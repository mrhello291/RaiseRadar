"""Sample report, key setup, guided start, and the "what counts as NEW" rules."""
import json
import os
import re
import sqlite3
import stat
from pathlib import Path

import httpx
import pytest

from companyfinder import cli, demo, onboarding
from companyfinder.db import DB
from companyfinder.experience import extract_experience, infer_from_title, seniority_of
from companyfinder.report import _job_is_new


# --------------------------------------------------------------------------- demo
def test_demo_is_complete_and_fictional(tmp_path):
    data = demo.build_demo_data()
    cs = data["companies"]
    assert data["demo"] and data["profile"]["active"]
    assert len(cs) >= 12 and sum(c["open"] for c in cs) >= 40
    jobs = [j for c in cs for j in c["jobs"]]
    # the sample must exercise every filter the report has
    assert {"remote", "hybrid", "onsite"} <= {j["workplace"] for j in jobs}
    assert {"intern", "junior", "mid", "senior", "lead/manager", "principal+"} <= {j["seniority"] for j in jobs}
    assert any(j["is_new"] for j in jobs) and any(j["matches"] for j in jobs) and any(j["salary"] for j in jobs)
    assert any(j["exp_source"] == "entry-phrase" for j in jobs) and any(j["exp_source"] == "jd" for j in jobs)
    assert any(c["is_new"] for c in cs) and any(c["yc"] for c in cs)
    assert any(c["website_confidence"] == "unsure" for c in cs)
    assert any(not c["open"] and c["jobs_status"] for c in cs)               # companies whose jobs we could not read
    assert any(len(c["funding"][0]["sources"]) > 1 for c in cs if c["funding"])
    assert any(j["posted_at"] is None and j["seen_at"] for j in jobs) and any(j["posted_at"] is None and not j["seen_at"] for j in jobs)
    # "Member of Technical Staff" is not a staff-level title
    assert next(j for j in jobs if j["title"].startswith("Member of Technical")) ["seniority"] == "mid"


def test_demo_links_never_point_at_real_companies():
    data = demo.build_demo_data()
    urls = []
    for c in data["companies"]:
        urls += [c["website"], c["board_url"], c["careers_url"], (c["yc"] or {}).get("url")]
        for f in c["funding"]:
            urls += [s["url"] for s in f["sources"]]
        for j in c["jobs"]:
            urls += [j["url"], j["apply_url"]]
    for u in filter(None, urls):
        host = re.sub(r"^https?://", "", u).split("/")[0]
        assert host == "example.com" or host.endswith(".example"), u


def test_write_demo_touches_only_its_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"
    info = demo.write_demo(out)
    assert info["demo"] and Path(info["report"]) == out / "index.html"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]               # no data/ folder, no .env, nothing else
    html = (out / "index.html").read_text()
    assert "Sample data." in html and 'id="demo" hidden' in html and '"demo": {"profile"' in html
    assert json.loads((out / "data.json").read_text())["demo"]
    assert (out / "jobs.csv").exists() and (out / "digest.md").exists()


def test_real_reports_are_not_marked_as_demo(tmp_path):
    from companyfinder.config import Config
    from companyfinder.pipeline import Pipeline
    from companyfinder.report import build_data
    p = Pipeline(Config(), DB(":memory:"), None, None)
    assert build_data(p)["demo"] is False


def _cfg_dir(tmp_path):
    (tmp_path / "config.toml").write_text("")
    return str(tmp_path / "config.toml")


def test_cli_demo_writes_to_report_dir_demo(tmp_path, capsys):
    cli.main(["-c", _cfg_dir(tmp_path), "demo"])
    assert (tmp_path / "reports" / "demo" / "index.html").is_file()
    assert not (tmp_path / "data").exists()
    assert "fictional" in capsys.readouterr().out


def test_start_without_a_terminal_shows_demo_and_instructions(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(onboarding, "interactive", lambda: False)
    monkeypatch.setattr(cli, "interactive", lambda: False)
    cli.main(["-c", _cfg_dir(tmp_path)])                                        # plain `companyfinder`
    out = capsys.readouterr().out
    assert "sample report" in out and "companyfinder setup" in out and "about 30 minutes" in out
    assert (tmp_path / "reports" / "demo" / "index.html").is_file()


def test_start_with_existing_data_does_not_build_a_demo(tmp_path, capsys, monkeypatch):
    cfgp = _cfg_dir(tmp_path)
    db = DB(tmp_path / "data" / "companyfinder.db")
    db.upsert_company("Real Co", origin="manual")
    db.close()
    cli.main(["-c", cfgp, "start"])
    assert "already have data" in capsys.readouterr().out
    assert not (tmp_path / "reports").exists()


def test_start_interactive_key_then_run(tmp_path, capsys, monkeypatch):
    """Terminal user pastes a key and agrees to the real run: the key is saved and a run starts without the news skipped."""
    monkeypatch.setattr(cli, "interactive", lambda: True)
    monkeypatch.setattr(onboarding, "check_api_key", lambda key, transport=None: ("ok", "free account"))
    monkeypatch.setattr(cli, "setup_key", lambda *a, **k: onboarding.setup_key(*a, **k, key_reader=lambda prompt: "sk-or-v1-abcdefghijklmnop1234"))
    prompts = []
    monkeypatch.setattr(cli, "ask", lambda prompt: prompts.append(prompt) or "y")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    seen = {}
    monkeypatch.setattr(cli, "cmd_run", lambda args, cfg: seen.update(skip_news=args.skip_news))
    try:
        cli.main(["-c", _cfg_dir(tmp_path), "start", "--no-open"])
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)
    assert seen == {"skip_news": False}
    assert "OPENROUTER_API_KEY=sk-or-v1-abcdefghijklmnop1234" in (tmp_path / ".env").read_text()
    out = capsys.readouterr().out
    assert "about 30 minutes" in prompts[0] and "Ctrl-C is safe" in prompts[0]        # the wait is announced before anything starts
    assert "sk-or-v1-abcdefghijklmnop1234" not in out                                  # the key itself is never echoed


def test_start_interactive_without_key_offers_keyless_run(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "interactive", lambda: True)
    monkeypatch.setattr(cli, "setup_key", lambda *a, **k: False)
    monkeypatch.setattr(cli, "ask", lambda prompt: "y")
    seen = {}
    monkeypatch.setattr(cli, "cmd_run", lambda args, cfg: seen.update(skip_news=args.skip_news))
    cli.main(["-c", _cfg_dir(tmp_path), "start", "--no-open"])
    assert seen == {"skip_news": True}                                          # YC-only run: no key, no news


# --------------------------------------------------------------------------- key handling
def _transport(status=200, body=None, boom=None):
    def handler(req: httpx.Request):
        if boom:
            raise boom
        assert req.url.host == "openrouter.ai" and req.headers["authorization"] == "Bearer KEY"
        return httpx.Response(status, json=body or {})
    return httpx.MockTransport(handler)


def test_check_api_key_outcomes():
    assert onboarding.check_api_key("KEY", _transport(200, {"data": {"is_free_tier": True}})) == ("ok", "free account")
    assert onboarding.check_api_key("KEY", _transport(200, {"data": {"is_free_tier": False}}))[0] == "ok"
    assert onboarding.check_api_key("KEY", _transport(401))[0] == "invalid"
    assert onboarding.check_api_key("KEY", _transport(500))[0] == "unreachable"
    assert onboarding.check_api_key("KEY", _transport(boom=httpx.ConnectError("down")))[0] == "unreachable"


def test_save_env_key_keeps_other_lines_and_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "old")
    env = tmp_path / ".env"
    env.write_text("# my settings\nOPENROUTER_MODEL=a,b\nOPENROUTER_API_KEY=\nOPENROUTER_API_KEY=dup\nOTHER=1\n")
    onboarding.save_env_key(env, "sk-new")
    lines = env.read_text().splitlines()
    assert lines == ["# my settings", "OPENROUTER_MODEL=a,b", "OPENROUTER_API_KEY=sk-new", "OTHER=1"]
    assert stat.S_IMODE(env.stat().st_mode) == 0o600 and os.environ["OPENROUTER_API_KEY"] == "sk-new"
    fresh = tmp_path / "sub.env"
    onboarding.save_env_key(fresh, "k")
    assert fresh.read_text() == "OPENROUTER_API_KEY=k\n"


def test_setup_key_retries_invalid_then_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    replies = iter(["bad-key", "KEY"])
    seen = []
    t = httpx.MockTransport(lambda req: httpx.Response(200 if req.headers["authorization"] == "Bearer KEY" else 401, json={"data": {}}))
    ok = onboarding.setup_key(tmp_path / ".env", transport=t, say=seen.append, key_reader=lambda p: next(replies))
    assert ok and "OPENROUTER_API_KEY=KEY" in (tmp_path / ".env").read_text()
    assert any("rejected" in s for s in seen) and any("accepted" in s for s in seen)
    assert not any("KEY" == s for s in seen)


def test_setup_key_skip_unreachable_and_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    say = []
    assert onboarding.setup_key(tmp_path / ".env", say=say.append, key_reader=lambda p: "") is False
    assert not (tmp_path / ".env").exists()
    down = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert onboarding.setup_key(tmp_path / ".env", transport=down, say=say.append, key_reader=lambda p: "KEY") is True   # saved anyway
    assert "OPENROUTER_API_KEY=KEY" in (tmp_path / ".env").read_text()
    # a key that already works: no prompt at all
    good = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {}}))
    assert onboarding.setup_key(tmp_path / "x.env", current="KEY", transport=good, say=say.append,
                                key_reader=lambda p: pytest.fail("must not ask")) is True


def test_setup_command_refuses_without_a_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "interactive", lambda: False)
    with pytest.raises(SystemExit) as e:
        cli.main(["-c", _cfg_dir(tmp_path), "setup"])
    assert "terminal" in str(e.value)


# --------------------------------------------------------------------------- Ctrl-C / first-run notice
def test_ctrl_c_still_writes_a_report(tmp_path, capsys, monkeypatch):
    from companyfinder.pipeline import Pipeline
    monkeypatch.setattr(Pipeline, "run_all", lambda self, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cli.main(["-c", _cfg_dir(tmp_path), "run"])
    out = capsys.readouterr().out
    assert "Interrupted" in out and (tmp_path / "reports" / "index.html").is_file()
    assert "no OPENROUTER_API_KEY" in out                                       # told up front that news is skipped


def test_open_flag_opens_only_from_a_terminal(tmp_path, capsys, monkeypatch):
    from companyfinder.pipeline import Pipeline
    monkeypatch.setattr(Pipeline, "run_all", lambda self, **k: {})
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    opened = []
    monkeypatch.setattr(cli, "_open", lambda path: opened.append(path))
    cfgp = _cfg_dir(tmp_path)

    monkeypatch.setattr(cli, "interactive", lambda: False)                      # cron / container
    cli.main(["-c", cfgp, "run", "--open"])
    cli.main(["-c", cfgp, "report", "--open"])
    assert opened == [] and "--open ignored" in capsys.readouterr().out

    monkeypatch.setattr(cli, "interactive", lambda: True)                       # a person at a terminal
    cli.main(["-c", cfgp, "run", "--open"])
    cli.main(["-c", cfgp, "report", "--open"])
    assert [p.name for p in opened] == ["index.html", "index.html"]

    opened.clear()
    cli.main(["-c", cfgp, "run"])                                               # no flag: never opens
    cli.main(["-c", cfgp, "report"])
    assert opened == []


# --------------------------------------------------------------------------- what is NEW
def _job(**kw):
    base = dict(posted_at=None, first_seen=None, baseline=0)
    base.update(kw)
    return base


def test_new_uses_the_board_date_when_there_is_one():
    from companyfinder.util import now_iso
    from datetime import datetime, timedelta, timezone
    ago = lambda d: (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()
    assert _job_is_new(_job(posted_at=ago(1), first_seen=ago(1), baseline=1), 3)          # posted yesterday: new even on the first fetch
    assert not _job_is_new(_job(posted_at=ago(30), first_seen=ago(0), baseline=0), 3)     # old posting we only just noticed: not new
    assert not _job_is_new(_job(first_seen=now_iso(), baseline=1), 3)                     # no date, open at our first look: not new
    assert _job_is_new(_job(first_seen=now_iso(), baseline=0), 3)                         # no date, appeared in a later fetch: new
    assert not _job_is_new(_job(first_seen=ago(10), baseline=0), 3)


def test_first_fetch_is_a_baseline_and_later_fetches_are_not():
    db = DB(":memory:")
    cid, _ = db.upsert_company("Acme", origin="manual")
    mk = lambda i: {"id": f"j{i}", "title": f"T{i}", "matches_profile": 0}
    db.sync_jobs(cid, [mk(1), mk(2)], "2026-10-01T00:00:00+00:00", first_fetch=True)
    db.sync_jobs(cid, [mk(1), mk(2), mk(3)], "2026-10-05T00:00:00+00:00", first_fetch=False)
    rows = {r["id"]: r["baseline"] for r in db.jobs_for(cid)}
    assert rows == {"j1": 1, "j2": 1, "j3": 0}


def test_old_database_jobs_become_baseline_on_upgrade(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE jobs(id TEXT PRIMARY KEY, company_id INTEGER, title TEXT, department TEXT, location TEXT, remote INTEGER,
        employment_type TEXT, url TEXT, apply_url TEXT, posted_at TEXT, description TEXT, salary_text TEXT, exp_min REAL, exp_max REAL,
        exp_source TEXT, exp_mentions TEXT, seniority TEXT, role_family TEXT, workplace TEXT, matches_profile INTEGER,
        first_seen TEXT, last_seen TEXT, active INTEGER NOT NULL DEFAULT 1);
        INSERT INTO jobs(id, company_id, title, first_seen, active) VALUES('a', 1, 'Old role', '2026-10-30T00:00:00+00:00', 1);""")
    c.commit(); c.close()
    db = DB(path)
    assert [r[0] for r in db.conn.execute("SELECT baseline FROM jobs")] == [1]


# --------------------------------------------------------------------------- extractor fixes found while building the demo
def test_member_of_technical_staff_is_not_staff_level():
    for t in ("Member of Technical Staff, Voice", "Chief of Staff", "Staff Accountant"):
        assert seniority_of(t) not in ("principal+",), t
    assert infer_from_title("Member of Technical Staff") is None
    for t in ("Staff Software Engineer", "Senior Staff Engineer"):
        assert seniority_of(t) == "principal+"


def test_experience_snippet_is_the_whole_bullet_not_a_cut_window():
    jd = "About us\nWe build things.\n\nWhat we are looking for\n- 2-4 years of experience building backend services\n- Comfortable with Python\n"
    assert extract_experience(jd, "Engineer").mentions == ["2-4 years of experience building backend services"]
    long = "You have 5+ years of experience shipping things; " + "lots of words here " * 30 + "and then more."
    m = extract_experience(long, "Engineer").mentions[0]
    assert m.startswith("You have 5+ years") and len(m) < 260 and m.endswith("...")
