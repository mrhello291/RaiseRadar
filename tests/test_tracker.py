"""The application tracker: its own file, the `track` command, what reaches the report, and the web API."""
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from raiseradar import cli, report, serve as serve_mod
from raiseradar import tracker as tr
from raiseradar.config import Config
from raiseradar.db import DB
from raiseradar.demo import build_demo_data, build_demo_db
from raiseradar.report import write_report

KEY = "greenhouse:lumenfield:lumenfield-0"
META = {"company": "Lumenfield Robotics", "title": "Senior Engineer", "location": "SF", "url": "https://example.com/j/0"}


@pytest.fixture
def t(tmp_path):
    return tr.Tracker(tmp_path / "data" / "applications.db")


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- the store
def test_reading_a_missing_file_creates_nothing(t):
    assert t.list() == [] and t.get(1) is None and not t.exists() and t.delete(app_id=1) is False
    assert not t.path.parent.exists()


def test_tracking_a_role_records_its_history(t):
    a = t.set(job_key=KEY, status="saved", meta=META)
    assert a["status"] == "saved" and a["applied_at"] is None and a["company"] == "Lumenfield Robotics" and a["code"] == tr.job_code(KEY)
    a = t.set(job_key=KEY, status="applied")
    assert a["applied_at"] == datetime.now(timezone.utc).date().isoformat()          # filled in when you apply
    a = t.set(job_key=KEY, status="applied", note="  sent my CV  ")
    assert a["note"] == "sent my CV" and [e["status"] for e in a["events"]] == ["saved", "applied"]     # same status: no new event
    a = t.set(job_key=KEY, status="interviewing")
    assert [e["status"] for e in a["events"]] == ["saved", "applied", "interviewing"]
    assert a["applied_at"] is not None and len(t.list()) == 1


def test_an_application_added_late_counts_its_quiet_time_from_the_day_you_applied(t):
    a = t.add_manual("Acme", "Data Engineer", applied_at=_iso(30)[:10])
    assert tr.needs_follow_up(a, 14)
    b = t.add_manual("Beta", "Data Engineer")                              # applied today
    assert not tr.needs_follow_up(b, 14)
    c = t.set(job_key=KEY, status="applied", applied_at=_iso(20)[:10], meta=META)
    assert tr.needs_follow_up(c, 14) and c["events"][0]["at"].startswith(_iso(20)[:10])


def test_an_explicit_date_wins_and_is_checked(t):
    a = t.set(job_key=KEY, status="applied", applied_at="2026-09-01", meta=META)
    assert a["applied_at"] == "2026-09-01"
    with pytest.raises(tr.TrackerError, match="YYYY-MM-DD"):
        t.set(job_key=KEY, applied_at="1st of Sep")


def test_status_words_and_aliases():
    assert tr.parse_status("Applied") == "applied" and tr.parse_status("skip") == "ignored" and tr.parse_status("not interested") == "ignored"
    assert tr.parse_status("interview") == "interviewing"
    with pytest.raises(tr.TrackerError, match="unknown status"):
        tr.parse_status("hired!!")


def test_a_new_entry_needs_a_status_and_the_role_details(t):
    with pytest.raises(tr.TrackerError):
        t.set(job_key=KEY, status="applied")                          # nothing known about the role
    with pytest.raises(tr.TrackerError, match="say what to do"):
        t.set(job_key=KEY, meta=META)
    with pytest.raises(tr.TrackerError, match="no tracked entry"):
        t.set(app_id=99, status="applied")
    assert t.list() == []


def test_notes_are_capped_and_manual_entries_work(t):
    a = t.add_manual("Acme", "Backend Engineer", url="https://acme.example/j", note="x" * 5000, applied_at="2026-10-01")
    assert a["job_key"] is None and a["code"] == "" and len(a["note"]) == tr.MAX_NOTE and a["applied_at"] == "2026-10-01"
    b = t.set(app_id=a["id"], status="rejected")
    assert b["status"] == "rejected" and [e["status"] for e in b["events"]] == ["applied", "rejected"]
    with pytest.raises(tr.TrackerError):
        t.add_manual("", "x")


def test_delete_removes_the_entry_and_its_history(t):
    a = t.set(job_key=KEY, status="applied", meta=META)
    assert t.delete(job_key=KEY) is True and t.list() == [] and t.delete(job_key=KEY) is False
    conn = sqlite3.connect(t.path)
    assert conn.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 0 and a["id"]
    conn.close()


def test_follow_up_means_applied_and_quiet_for_long_enough():
    app = {"status": "applied", "updated_at": _iso(20)}
    assert tr.needs_follow_up(app, 14) and not tr.needs_follow_up(app, 30) and not tr.needs_follow_up(app, 0)
    assert not tr.needs_follow_up({**app, "status": "interviewing"}, 14)
    assert not tr.needs_follow_up({**app, "updated_at": _iso(3)}, 14)


def test_the_applications_survive_wiping_the_job_database(tmp_path):
    cfg = Config(root=tmp_path)
    p, mem = build_demo_db()
    t = tr.Tracker(cfg.path("tracker", "path"))
    t.set(job_key=KEY, status="applied", meta=tr.job_meta(mem, KEY))
    (tmp_path / "data").mkdir(exist_ok=True)
    db_file = cfg.path("general", "db_path")
    db_file.write_text("")                                             # the cache is thrown away...
    db_file.unlink()
    again = tr.Tracker(cfg.path("tracker", "path")).list()
    assert [a["status"] for a in again] == ["applied"] and again[0]["title"]


def test_finding_jobs_by_words_code_or_id():
    p, db = build_demo_db()
    by_words = tr.find_jobs(db, "lumenfield robotics engineer")
    assert by_words and all("Lumenfield" in j["company"] for j in by_words)
    one = by_words[0]
    assert tr.find_jobs(db, one["code"])[0]["key"] == one["key"] and tr.find_jobs(db, one["key"])[0]["key"] == one["key"]
    assert tr.find_jobs(db, "zzzz nothing") == [] and tr.find_jobs(db, "  ") == []
    closed = [j for j in tr.find_jobs(db, "warehouse controls") if not j["active"]]
    assert closed and tr.listing_state(db, closed[0]["key"]) == "closed" and tr.listing_state(db, one["key"]) == "open"
    assert tr.listing_state(db, None) == "unknown" and tr.listing_state(db, "nope") == "unknown"


# --------------------------------------------------------------------------- what reaches the report
def _real_setup(tmp_path):
    """A scratch config + job database made from the sample companies (nothing real)."""
    p, mem = build_demo_db()
    (tmp_path / "data").mkdir()
    dst = sqlite3.connect(tmp_path / "data" / "companyfinder.db")
    mem.conn.backup(dst)
    dst.close()
    (tmp_path / "config.toml").write_text("")
    cfg = Config(root=tmp_path)
    tr.Tracker(cfg.path("tracker", "path")).set(job_key=KEY, status="applied", note="private note", meta=tr.job_meta(mem, KEY))
    return cfg


def _pipeline(cfg):
    p, db, _ = cli.build(cfg, need_llm=False)
    return p, db


def test_applications_are_in_the_page_but_not_in_data_json_csv_or_digest(tmp_path):
    cfg = _real_setup(tmp_path)
    p, db = _pipeline(cfg)
    write_report(p)
    db.close()
    out = tmp_path / "reports"
    assert "private note" in (out / "index.html").read_text()
    for name in ("data.json", "companies.csv", "jobs.csv", "digest.md"):
        assert "private note" not in (out / name).read_text() and '"tracker"' not in (out / name).read_text()


def test_public_reports_never_contain_applications(tmp_path):
    cfg = _real_setup(tmp_path)
    p, db = _pipeline(cfg)
    write_report(p, public=True)
    db.close()
    assert "private note" not in "".join(f.read_text() for f in (tmp_path / "reports").iterdir() if f.is_file())
    cfg.data["report"]["public"] = True                                   # also when public comes from config.toml
    p, db = _pipeline(cfg)
    write_report(p, tracker=True)                                         # even if asked to embed
    db.close()
    assert "private note" not in (tmp_path / "reports" / "index.html").read_text()


def test_embedding_can_be_switched_off(tmp_path):
    cfg = _real_setup(tmp_path)
    cfg.data["tracker"]["embed_in_report"] = False
    p, db = _pipeline(cfg)
    write_report(p)
    assert "private note" not in (tmp_path / "reports" / "index.html").read_text()
    write_report(p, tracker=True)                                         # an explicit request overrides the config
    db.close()
    assert "private note" in (tmp_path / "reports" / "index.html").read_text()


def test_the_page_data_lists_each_application_with_its_listing_state(tmp_path):
    cfg = _real_setup(tmp_path)
    p, db = _pipeline(cfg)
    data = report.build_data(p, tracker=True)
    (a,) = data["tracker"]["apps"]
    assert a["status"] == "applied" and a["listing"] == "open" and a["key"] == KEY
    assert data["tracker"]["follow_up_days"] == 14 and ["applied", "Applied"] in data["tracker"]["statuses"]
    assert "tracker" not in report.build_data(p)                          # off unless asked
    db.close()


# --------------------------------------------------------------------------- the `track` command
@pytest.fixture
def cfgfile(tmp_path):
    _real_setup(tmp_path)
    (tmp_path / "data" / "applications.db").unlink()                      # start with nothing tracked
    return tmp_path / "config.toml"


def run(cfgfile, *args):
    cli.main(["-c", str(cfgfile), "track", *args])


def test_track_set_by_words_then_list_note_show_and_remove(cfgfile, capsys, tmp_path):
    run(cfgfile, "set", "applied", "lumenfield", "mechanical", "intern", "--note", "via the careers page")
    out = capsys.readouterr().out
    assert out.startswith("Applied: Lumenfield Robotics - Mechanical Engineering Intern")
    run(cfgfile, "list")
    assert "Lumenfield Robotics" in capsys.readouterr().out
    run(cfgfile, "set", "interviewing", "#1")                              # a tracked entry is found by its number
    run(cfgfile, "note", "--text", "phone screen on Monday", "lumenfield")
    run(cfgfile, "show", "#1")
    shown = capsys.readouterr().out
    assert "Interviewing" in shown and "phone screen on Monday" in shown and "Applied" in shown and "->" in shown
    run(cfgfile, "rm", "#1")
    run(cfgfile, "list")
    assert "Nothing tracked yet" in capsys.readouterr().out


def test_an_ambiguous_search_lists_the_candidates_and_changes_nothing(cfgfile, capsys):
    with pytest.raises(SystemExit) as exc:
        run(cfgfile, "set", "applied", "lumenfield")
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "Several roles match" in out and "Lumenfield Robotics" in out
    run(cfgfile, "list")
    assert "Nothing tracked yet" in capsys.readouterr().out


def test_unknown_role_or_status_gives_a_clear_error(cfgfile):
    with pytest.raises(SystemExit, match="nothing matches"):
        run(cfgfile, "set", "applied", "no such company anywhere")
    with pytest.raises(SystemExit, match="unknown status"):
        run(cfgfile, "set", "hired", "lumenfield")
    with pytest.raises(SystemExit, match="among your tracked roles"):
        run(cfgfile, "rm", "lumenfield")


def test_track_add_for_a_role_found_elsewhere_and_list_filters(cfgfile, capsys):
    run(cfgfile, "add", "--company", "Acme", "--title", "Data Engineer", "--url", "https://acme.example/j", "--date", "2026-09-01")
    run(cfgfile, "set", "ignored", "lumenfield", "mechanical")
    capsys.readouterr()
    run(cfgfile, "list")
    shown = capsys.readouterr().out
    assert "Acme" in shown and "applied 2026-09-01" in shown and "Mechanical Engineering Intern" not in shown      # ignored ones are hidden...
    assert "follow up" in shown                                                                                   # ...old applications are flagged
    run(cfgfile, "list", "--all")
    assert "Mechanical Engineering Intern" in capsys.readouterr().out
    run(cfgfile, "list", "--status", "ignored")
    only = capsys.readouterr().out
    assert "Mechanical" in only and "Acme" not in only


def test_a_closed_role_is_flagged_in_the_list(cfgfile, capsys):
    run(cfgfile, "set", "applied", "warehouse", "controls")
    capsys.readouterr()
    run(cfgfile, "list")
    assert "role no longer listed" in capsys.readouterr().out


# --------------------------------------------------------------------------- the web API
def _server(cfg, host="127.0.0.1", writable=True):
    api = serve_mod.TrackerAPI(cfg, host, writable)
    (cfg.root / "reports").mkdir(exist_ok=True)
    (cfg.root / "reports" / "index.html").write_text("<h1>page</h1>")
    server = serve_mod.make_server(cfg.root / "reports", "127.0.0.1", 0, api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, api, f"http://127.0.0.1:{server.server_address[1]}"


def call(url, method="GET", body=None, headers=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    def parse(raw):
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, parse(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, parse(e.read()), dict(e.headers)


def good(api, **extra):
    return {"Content-Type": "application/json", "X-RaiseRadar-Token": api.token, **extra}


@pytest.fixture
def web(tmp_path):
    cfg = _real_setup(tmp_path)
    (tmp_path / "data" / "applications.db").unlink()
    server, api, base = _server(cfg)
    yield cfg, api, base
    server.shutdown(); server.server_close()


def test_the_page_can_read_and_save_applications(web):
    cfg, api, base = web
    status, snap, headers = call(base + "/api/tracker")
    assert status == 200 and snap["writable"] and snap["token"] == api.token and snap["applications"] == []
    assert headers["Cache-Control"] == "no-store"
    status, snap, _ = call(base + "/api/tracker/set", "POST", {"key": KEY, "status": "applied"}, good(api))
    assert status == 200 and snap["applications"][0]["status"] == "applied" and snap["applications"][0]["listing"] == "open"
    assert snap["applications"][0]["company"] == "Lumenfield Robotics"                          # taken from our database, not from the request
    status, snap, _ = call(base + "/api/tracker/set", "POST", {"id": snap["applications"][0]["id"], "note": "hello"}, good(api))
    assert snap["applications"][0]["note"] == "hello"
    assert tr.Tracker(cfg.path("tracker", "path")).list()[0]["note"] == "hello"
    status, snap, _ = call(base + "/api/tracker/delete", "POST", {"key": KEY}, good(api))
    assert status == 200 and snap["applications"] == []
    assert call(base + "/api/tracker/delete", "POST", {"key": KEY}, good(api))[0] == 400


def test_the_page_cannot_invent_a_role_or_a_status(web):
    _cfg, api, base = web
    assert call(base + "/api/tracker/set", "POST", {"key": "made:up:role", "status": "applied", "company": "X", "title": "Y"}, good(api))[0] == 400
    assert call(base + "/api/tracker/set", "POST", {"key": KEY, "status": "hired!!"}, good(api))[0] == 400
    assert call(base + "/api/tracker/set", "POST", {"key": KEY, "status": 5}, good(api))[0] == 400
    assert call(base + "/api/tracker/set", "POST", {"status": "applied"}, good(api))[0] == 400
    assert call(base + "/api/tracker/set", "POST", {"key": KEY, "status": "applied", "date": "yesterday"}, good(api))[0] == 400
    assert call(base + "/api/tracker/set", "POST", [1, 2], good(api))[0] == 400
    assert call(base + "/api/tracker/set", "POST", b"not json", good(api))[0] == 400


def test_writes_are_refused_without_the_token_or_from_another_site(web):
    _cfg, api, base = web
    body = {"key": KEY, "status": "applied"}
    plain = {"Content-Type": "application/json"}
    assert call(base + "/api/tracker/set", "POST", body, plain)[0] == 403                                   # no token
    assert call(base + "/api/tracker/set", "POST", body, {**plain, "X-RaiseRadar-Token": "guess"})[0] == 403
    assert call(base + "/api/tracker/set", "POST", body, good(api, Origin="https://evil.example"))[0] == 403  # another website
    assert call(base + "/api/tracker/set", "POST", body, good(api, Host="evil.example:8080"))[0] == 403      # DNS rebinding
    assert call(base + "/api/tracker/set", "POST", body, {"Content-Type": "text/plain", "X-RaiseRadar-Token": api.token})[0] == 415
    assert call(base + "/api/tracker/set", "POST", b"x" * 30000, good(api))[0] == 413
    assert call(base + "/api/tracker/set", "OPTIONS", None, {"Origin": "https://evil.example"})[0] == 501    # no CORS preflight answer
    assert call(base + "/api/tracker")[0] == 200 and call(base + "/api/tracker", headers={"Host": "evil.example"})[0] == 403
    assert call(base + "/api/tracker/set", "POST", body, good(api, Origin=base))[0] == 200                  # the page itself is fine
    assert tr.Tracker(_cfg.path("tracker", "path")).list()[0]["status"] == "applied"


def test_a_read_only_server_never_hands_out_a_token_or_saves(tmp_path):
    cfg = _real_setup(tmp_path)
    server, api, base = _server(cfg, writable=False)
    try:
        status, snap, _ = call(base + "/api/tracker")
        assert status == 200 and snap["writable"] is False and "token" not in snap
        assert call(base + "/api/tracker/set", "POST", {"key": KEY, "status": "applied"}, good(api))[0] == 403
    finally:
        server.shutdown(); server.server_close()


def test_other_api_paths_and_methods_are_not_found(web):
    _cfg, api, base = web
    for path in ("/api/", "/api/other", "/api/tracker/../../etc/passwd", "/api/tracker/nope"):
        assert call(base + path)[0] in (403, 404)
    assert call(base + "/api/tracker/set")[0] == 405                                                         # GET on a write path
    assert call(base + "/api/tracker", "POST", {}, good(api))[0] == 405
    assert call(base + "/", "POST", {}, good(api))[0] == 405
    with urllib.request.urlopen(base + "/index.html", timeout=5) as r:                                         # ordinary files still work
        assert r.status == 200 and b"page" in r.read()


def test_without_an_api_the_paths_do_not_exist(tmp_path):
    (tmp_path / "reports").mkdir()
    server = serve_mod.make_server(tmp_path / "reports", "127.0.0.1", 0, None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert call(base + "/api/tracker")[0] == 404 and call(base + "/api/tracker/set", "POST", {}, {"Content-Type": "application/json"})[0] == 404
    finally:
        server.shutdown(); server.server_close()


def test_host_names_that_are_accepted(tmp_path, monkeypatch):
    cfg = Config(root=tmp_path)
    api = serve_mod.TrackerAPI(cfg, "0.0.0.0", True)
    assert all(api.host_ok(h) for h in ("localhost", "localhost:8080", "127.0.0.1:9000", "[::1]:8080", "LOCALHOST"))
    assert not any(api.host_ok(h) for h in (None, "", "evil.example", "evil.example:80", "localhost.evil.example", "127.0.0.1.evil.example"))
    monkeypatch.setenv("RAISERADAR_ALLOWED_HOSTS", "nas.lan, pi")
    api = serve_mod.TrackerAPI(cfg, "192.168.1.5", True)
    assert api.host_ok("nas.lan:8080") and api.host_ok("pi") and api.host_ok("192.168.1.5:8080") and not api.host_ok("192.168.1.6")


def test_when_saving_is_on(monkeypatch):
    m = serve_mod.tracker_mode
    monkeypatch.delenv("RAISERADAR_TRACKER", raising=False)
    assert m("127.0.0.1", False, None) and m("localhost", False, None)
    assert not m("0.0.0.0", False, None)                                  # reachable by others: off unless you say so
    assert m("0.0.0.0", False, True) and not m("127.0.0.1", False, False)
    assert not m("127.0.0.1", True, None) and not m("127.0.0.1", True, True)       # never for a public page
    monkeypatch.setenv("RAISERADAR_TRACKER", "1")
    assert m("0.0.0.0", False, None) and not m("0.0.0.0", True, None)


def test_serve_command_line_passes_the_tracker_choice(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(serve_mod, "serve", lambda cfg, host, port, full, jobs, public=None, ready=None, tracker=None: seen.append(tracker))
    cfgp = tmp_path / "config.toml"; cfgp.write_text("")
    cli.main(["-c", str(cfgp), "serve"]); cli.main(["-c", str(cfgp), "serve", "--tracker"]); cli.main(["-c", str(cfgp), "serve", "--no-tracker"])
    assert seen == [None, True, False]
