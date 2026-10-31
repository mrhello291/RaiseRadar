"""Whole pipeline against a mocked internet: news -> LLM -> website -> careers -> ATS jobs -> report."""
import csv
import html as htmllib
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from raiseradar import resolve
from raiseradar.config import Config
from raiseradar.llm import LLMClient
from raiseradar.pipeline import Pipeline
from raiseradar.report import write_report
from conftest import html_response, json_response, make_fetcher


def rfc822(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S GMT")


FEED = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Acme raises $30M Series B led by Accel - TechCrunch</title><link>https://news.google.com/rss/articles/A1</link><guid>A1</guid><pubDate>{rfc822(2)}</pubDate></item>
<item><title>Acme secures $30 million Series B - Sifted</title><link>https://news.google.com/rss/articles/A2</link><guid>A2</guid><pubDate>{rfc822(1)}</pubDate></item>
<item><title>Globex lands EUR 50M Series C from Index Ventures - Tech.eu</title><link>https://news.google.com/rss/articles/G1</link><guid>G1</guid><pubDate>{rfc822(3)}</pubDate></item>
<item><title>Sequoia closes $950M Series B-stage fund - Axios</title><link>https://news.google.com/rss/articles/F1</link><guid>F1</guid><pubDate>{rfc822(3)}</pubDate></item>
<item><title>Nice weather today - BBC</title><link>https://news.google.com/rss/articles/W1</link><guid>W1</guid><pubDate>{rfc822(1)}</pubDate></item>
</channel></rss>"""

LLM_REPLY = {
    "Acme raises": {"is_funding": True, "company": "Acme", "round": "Series B", "amount_usd_millions": 30, "amount_text": "$30M",
                    "investors": ["Accel", "Y Combinator"], "lead_investors": ["Accel"], "country": "United States", "description": "makes anvils"},
    "Acme secures": {"is_funding": True, "company": "Acme", "round": "Series B", "amount_usd_millions": 30, "amount_text": "$30 million", "investors": ["Accel"]},
    "Globex lands": {"is_funding": True, "company": "Globex", "round": "Series C", "amount_usd_millions": 55, "amount_text": "EUR 50M",
                     "investors": ["Index Ventures"], "lead_investors": ["Index Ventures"], "country": "Germany", "city": "Berlin"},
    "Sequoia closes": {"is_funding": False, "company": "Sequoia"},
}


def page(title):
    return f"<html><head><title>{title}</title></head><body><h1>{title}</h1>" + "<p>text</p>" * 30 + "</body></html>"


class World:
    """Mutable fake internet. Tests tweak it between pipeline runs."""

    def __init__(self):
        self.llm_prompts = []
        self.llm_status = 200
        self.poison = None                 # if this text is in the prompt the model answers with garbage (a headline that breaks it)
        self.llm_truncate_after = None     # keep only this many complete items, then cut the JSON mid-object
        self.lever_jobs = [
            {"id": "l1", "text": "Senior Backend Engineer", "hostedUrl": "https://jobs.lever.co/acme/l1", "createdAt": 1790000000000,
             "categories": {"location": "Remote - Europe", "department": "Engineering"}, "workplaceType": "remote",
             "descriptionPlain": "Build our API. 5+ years of experience with Python required."},
            {"id": "l2", "text": "Software Engineer", "hostedUrl": "https://jobs.lever.co/acme/l2", "createdAt": 1790000000000,
             "categories": {"location": "New York", "department": "Engineering"}, "descriptionPlain": "We are great. 1-3 years of experience."},
            {"id": "l3", "text": "Office Manager", "hostedUrl": "https://jobs.lever.co/acme/l3", "categories": {"location": "New York"}, "descriptionPlain": "Keep the lights on."},
        ]

    def web(self, req: httpx.Request) -> httpx.Response:
        h, u = req.url.host, str(req.url)
        if h == "news.google.com":
            return httpx.Response(200, text=FEED, headers={"content-type": "application/rss+xml"})
        if h == "acme.com":
            if req.url.path == "/careers":
                return html_response('<html><a href="https://jobs.lever.co/acme">Open roles</a></html>')
            return html_response(page("Acme - anvils").replace("</body>", '<a href="/careers">Careers</a></body>'))
        if h == "globex.com":
            return html_response(page("Globex").replace("</body>", '<a href="https://boards.greenhouse.io/globex">We are hiring</a></body>'))
        if h == "api.lever.co" and "/acme" in u:
            return json_response(self.lever_jobs)
        if h == "boards-api.greenhouse.io" and "/globex/" in u:
            return json_response({"jobs": [{"id": 77, "title": "Data Engineer", "absolute_url": "https://boards.greenhouse.io/globex/jobs/77",
                                            "location": {"name": "Berlin"}, "content": htmllib.escape("<p>No prior experience required.</p>")}]})
        return html_response("", 404)

    def llm(self, req: httpx.Request) -> httpx.Response:
        if self.llm_status != 200:
            return httpx.Response(self.llm_status, text='{"error": "Rate limit exceeded: free-models-per-day"}')
        prompt = json.loads(req.content)["messages"][1]["content"]
        self.llm_prompts.append(prompt)
        if self.poison and self.poison in prompt:
            return httpx.Response(200, json={"choices": [{"message": {"content": "I'm sorry, I cannot do that."}, "finish_reason": "stop"}]})
        items = []
        for m in re.finditer(r"^\[(\d+)\] (.*?) \(via", prompt, re.M):
            for key, val in LLM_REPLY.items():
                if m.group(2).startswith(key):
                    items.append({"id": int(m.group(1)), **val})
                    break
            else:
                items.append({"id": int(m.group(1)), "is_funding": False})
        content = json.dumps({"items": items})
        if self.llm_truncate_after is not None:
            content = json.dumps({"items": items[: self.llm_truncate_after]})[:-2] + ', {"id": 99, "is_fu'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "length" if self.llm_truncate_after is not None else "stop"}]})


@pytest.fixture
def env(db, tmp_path, monkeypatch):
    monkeypatch.setattr(resolve, "_dns_ok", lambda h: h in {"acme.com", "globex.com"})
    world = World()
    cfg = Config(root=tmp_path)
    cfg.data["general"]["report_dir"] = str(tmp_path / "reports")
    cfg.data["news"].update(editions=["US:US:en"], extra_feeds=[])
    cfg.data["funding"]["target_rounds"] = ["Series B", "Series C"]
    cfg.data["profile"].update(roles=["engineer"], exclude_titles=["intern"], max_experience_years=3, locations=["remote", "berlin"])
    cfg.data["http"]["workers"] = 1

    def make_pipeline():
        llm = LLMClient("k", ["m"], cache=db, requests_per_minute=6000, sleep=lambda s: None,
                        client=httpx.Client(transport=httpx.MockTransport(world.llm)))
        return Pipeline(cfg, db, make_fetcher(world.web), llm)

    return world, cfg, db, make_pipeline


def test_full_run(env):
    world, cfg, db, make = env
    p = make()
    stats = p.run_all(news=True, yc=False)

    # --- stage 1: funding extraction
    assert stats["candidates"] == 4 and stats["llm_calls"] == 1 and stats["funding_items"] == 3
    prompt = world.llm_prompts[0]
    assert "Nice weather" not in prompt                              # prefilter kept the LLM prompt small
    acme, globex = db.find_company("Acme"), db.find_company("Globex")
    assert db.find_company("Sequoia") is None
    f = db.funding_for(acme["id"])
    assert len(f) == 2 and {x["source_name"] for x in f} == {"TechCrunch", "Sifted"}      # one round, two sources
    assert f[0]["round"] == "Series B" and "Accel" in f[0]["investors"] + f[1]["investors"]
    assert globex["country"] == "Germany" and db.funding_for(globex["id"])[0]["amount_text"] == "EUR 50M"

    # --- stage 3/4: website + careers
    acme, globex = db.get_company(acme["id"]), db.get_company(globex["id"])
    assert acme["website"] == "https://acme.com" and acme["website_confidence"] == "unsure"     # auto-matched, 4-letter name
    assert (acme["ats"], acme["ats_slug"], acme["ats_confidence"]) == ("lever", "acme", "linked")
    assert acme["careers_url"] == "https://acme.com/careers"
    assert (globex["ats"], globex["ats_slug"]) == ("greenhouse", "globex")

    # --- stage 5: jobs + enrichment
    jobs = {j["title"]: j for j in db.jobs_for(acme["id"])}
    assert set(jobs) == {"Senior Backend Engineer", "Software Engineer", "Office Manager"}
    sr = jobs["Senior Backend Engineer"]
    assert (sr["exp_min"], sr["exp_source"], sr["seniority"], sr["role_family"], sr["remote"]) == (5.0, "jd", "senior", "engineering", 1)
    assert "5+ years of experience" in sr["description"] and sr["matches_profile"] == 0         # needs 5 yrs > max 3
    se = jobs["Software Engineer"]
    assert (se["exp_min"], se["exp_max"]) == (1.0, 3.0) and se["matches_profile"] == 0           # New York, not remote/berlin
    assert jobs["Office Manager"]["matches_profile"] == 0 and jobs["Office Manager"]["exp_min"] is None
    gj = db.jobs_for(globex["id"])[0]
    assert (gj["exp_min"], gj["exp_source"], gj["matches_profile"]) == (0.0, "entry-phrase", 1)    # Berlin, entry-level

    # --- report
    info = write_report(p)
    out = cfg.path("general", "report_dir")
    data = json.loads((out / "data.json").read_text())
    names = [c["name"] for c in data["companies"]]
    assert set(names) == {"Acme", "Globex"} and names[0] == "Globex"                              # Globex has a matching job -> ranks first
    g = data["companies"][0]
    assert g["funding"][0]["sources"][0]["name"] == "Tech.eu" and g["board_url"] == "https://boards.greenhouse.io/globex"
    assert g["jobs"][0]["exp_label"] == "0-1 yrs" and g["jobs"][0]["matches"] is True
    a = next(c for c in data["companies"] if c["name"] == "Acme")
    assert len(a["funding"]) == 1 and len(a["funding"][0]["sources"]) == 2 and a["funding"][0]["investors"] == ["Accel", "Y Combinator"]
    html = (out / "index.html").read_text()
    assert "Acme" in html and "</script><script" not in html.split('type="application/json">')[1].split("</script>")[0]
    rows = list(csv.DictReader(open(out / "jobs.csv")))
    assert len(rows) == 4 and any(r["title"] == "Data Engineer" and r["experience"] == "0-1 yrs" for r in rows)
    # roles that were already open when we first looked are the baseline, not "new"
    assert not any(j["is_new"] for c in data["companies"] for j in c["jobs"])
    assert "## New roles matching your profile (0)" in (out / "digest.md").read_text()
    assert info["jobs"] == 4 and info["hiring"] == 2


def test_second_run_is_incremental(env):
    world, cfg, db, make = env
    make().run_all(news=True, yc=False)
    world.lever_jobs = world.lever_jobs[:2] + [{"id": "l9", "text": "Frontend Engineer", "hostedUrl": "u", "categories": {"location": "Remote"},
                                                 "descriptionPlain": "2+ years of experience"}]
    p2 = make()
    stats = p2.run_all(news=True, yc=False)
    assert stats["candidates"] == 0 and len(world.llm_prompts) == 1                  # articles already handled: no new LLM call
    assert stats["jobs_new"] == 1 and stats["jobs_closed"] == 1
    acme = db.find_company("Acme")
    assert {j["title"] for j in db.jobs_for(acme["id"])} == {"Senior Backend Engineer", "Software Engineer", "Frontend Engineer"}
    assert len(db.jobs_for(acme["id"], active_only=False)) == 4
    # only the role that appeared after the first fetch is NEW
    out = cfg.path("general", "report_dir")
    write_report(p2)
    new = [j["title"] for c in json.loads((out / "data.json").read_text())["companies"] for j in c["jobs"] if j["is_new"]]
    assert new == ["Frontend Engineer"]


def test_board_outage_does_not_close_jobs(env):
    world, cfg, db, make = env
    make().run_all(news=True, yc=False)
    original = world.web
    world.web = lambda req: html_response("", 503) if req.url.host == "api.lever.co" else original(req)
    p = make()
    p.fetcher.request  # fetcher bound to world.web through the closure below
    p.fetcher = make_fetcher(world.web)
    stats = p.fetch_jobs()
    acme = db.find_company("Acme")
    assert stats["boards_unreachable"] >= 1 and len(db.jobs_for(acme["id"])) == 3         # nothing was closed
    assert db.get_company(acme["id"])["jobs_status"] == "unreachable"


def test_llm_quota_exhaustion_keeps_articles_queued_and_pipeline_alive(env):
    world, cfg, db, make = env
    world.llm_status = 429
    p = make()
    stats = p.run_all(news=True, yc=False)
    assert stats["funding_items"] == 0 and db.find_company("Acme") is None
    assert db.known_article_ids(["x"]) == set()
    # candidates were NOT marked as processed, so a later run with a healthy LLM picks them up
    world.llm_status = 200
    stats = make().run_all(news=True, yc=False)
    assert stats["funding_items"] == 3 and db.find_company("Acme") is not None


def test_no_api_key_skips_llm_but_runs_other_stages(env):
    world, cfg, db, make = env
    p = make()
    p.llm = None
    db.upsert_company("Acme", origin="manual", website="https://acme.com")
    stats = p.run_all(news=True, yc=False)
    assert stats["candidates"] == 4 and stats["funding_items"] == 0 and stats["llm_calls"] == 0
    assert db.jobs_for(db.find_company("Acme")["id"])          # manual company still processed end-to-end


def test_manual_company_with_forced_ats(env):
    world, cfg, db, make = env
    p = make()
    cid, _ = db.upsert_company("Globex", origin="manual")
    db.update_company(cid, website="https://globex.com", ats="greenhouse", ats_slug="globex", ats_confidence="manual", careers_checked_at="2026-10-31T00:00:00+00:00")
    p.fetch_jobs()
    assert [j["title"] for j in db.jobs_for(cid)] == ["Data Engineer"]


def test_out_of_scope_companies_are_ignored(env):
    world, cfg, db, make = env
    p = make()
    cid, _ = db.upsert_company("Oldco", origin="news")
    db.add_funding(cid, round="Series B", amount_usd_m=10, amount_text=None, investors=[], lead_investors=[],
                   announced_at="2025-01-01T00:00:00+00:00", source_name="x", source_url="u", headline="h")
    cid2, _ = db.upsert_company("Seedco", origin="news")
    db.add_funding(cid2, round="Seed", amount_usd_m=1, amount_text=None, investors=[], lead_investors=[],
                   announced_at=datetime.now(timezone.utc).isoformat(), source_name="x", source_url="u2", headline="h")
    assert {c["name"] for c in p.scoped()} == set()


def test_transient_homepage_failure_is_rechecked_tomorrow_not_in_two_weeks(env):
    from raiseradar.util import days_since
    world, cfg, db, make = env
    p = make()
    cid, _ = db.upsert_company("Flaky", origin="manual", website="https://flaky.example")
    p.discover_careers()
    d = days_since(db.get_company(cid)["careers_checked_at"])
    assert 12.5 < d < 13.5          # rediscover_days (14) - 1
    assert p.discover_careers()["careers_tried"] == 0       # not today ...
    db.update_company(cid, careers_checked_at="2026-01-01T00:00:00+00:00")
    assert p.discover_careers()["careers_tried"] == 1       # ... but retried once the interval has passed


def test_truncated_llm_reply_keeps_finished_items_and_requeues_the_rest(env):
    """The real-world failure: a reasoning model runs out of tokens mid-JSON."""
    world, cfg, db, make = env
    world.llm_truncate_after = 2                      # answers for articles 0 and 1 (both Acme) arrive, 2 and 3 are cut off
    stats = make().run_all(news=True, yc=False)
    assert db.find_company("Acme") is not None and db.find_company("Globex") is None
    assert stats["funding_items"] == 2
    # the two unanswered articles were NOT marked as handled ...
    world.llm_truncate_after = None
    stats2 = make().run_all(news=True, yc=False)
    assert stats2["candidates"] == 2 and stats2["funding_items"] == 1             # ... so the next run reads them: Globex appears
    assert db.find_company("Globex") is not None and db.find_company("Sequoia") is None


def test_failed_batch_is_split_to_isolate_the_poison_article_and_eventually_dropped(env):
    world, cfg, db, make = env
    world.poison = "Globex"                              # any batch containing the Globex headline gets garbage back
    stats = make().run_all(news=True, yc=False)
    assert db.find_company("Acme") is not None           # the healthy half of the failed batch still got processed
    assert db.find_company("Globex") is None and stats["funding_items"] == 2
    arts = {r["title"][:6]: r for r in db.conn.execute("SELECT title, processed_at, attempts FROM articles")}
    assert arts["Sequoi"]["processed_at"] is not None    # innocent article in the same failing batch: answered after the split
    assert arts["Globex"]["processed_at"] is None and arts["Globex"]["attempts"] == 1
    for _ in range(2):                                   # two more failing runs -> 3 attempts
        make().run_all(news=True, yc=False)
    assert db.conn.execute("SELECT attempts FROM articles WHERE title LIKE 'Globex%'").fetchone()[0] == 3
    stats = make().run_all(news=True, yc=False)          # now it is given up on and no call is spent on it
    assert stats["given_up"] == 1 and stats["candidates"] == 0
    assert db.conn.execute("SELECT processed_at FROM articles WHERE title LIKE 'Globex%'").fetchone()[0] is not None


def test_job_workplace_is_recorded(env):
    world, cfg, db, make = env
    make().run_all(news=True, yc=False)
    acme = db.find_company("Acme")
    wp = {j["title"]: (j["workplace"], j["remote"]) for j in db.jobs_for(acme["id"])}
    assert wp["Senior Backend Engineer"] == ("remote", 1)       # ATS says workplaceType=remote
    assert wp["Software Engineer"] == ("", None)                # nothing stated


def test_recheck_regrades_and_replaces_auto_matched_websites(env):
    world, cfg, db, make = env
    p = make()
    # an old auto-match that is simply wrong (different domain), with careers/jobs that belong to the wrong company
    cid, _ = db.upsert_company("Acme", origin="news", website="https://old.example", domain="old.example", website_confidence="verified")
    db.add_funding(cid, round="Series B", amount_usd_m=30, amount_text=None, investors=[], lead_investors=[], announced_at=now_iso_(),
                   source_name="x", source_url="u", headline="h")
    db.update_company(cid, ats="lever", ats_slug="wrong", careers_checked_at="2026-10-30T00:00:00+00:00")
    manual, _ = db.upsert_company("Globex", origin="manual", website="https://globex.com", domain="globex.com", website_confidence="manual")
    stats = p.resolve_websites(recheck=True)
    c = db.get_company(cid)
    assert (c["website"], c["domain"]) == ("https://acme.com", "acme.com") and stats["websites_replaced"] == 1
    assert c["ats"] is None and c["careers_checked_at"] is None           # re-discovered from scratch next stage
    assert db.get_company(manual)["website_confidence"] == "manual"        # trusted sources are never touched
    # same site again but it can no longer be confirmed -> downgraded, not dropped
    world.web_backup = world.web
    world.web = lambda req: html_response("<html><body>nothing</body></html>") if req.url.host == "acme.com" else world.web_backup(req)
    p.fetcher = make_fetcher(world.web)
    db.update_company(cid, website_confidence="verified")
    stats = p.resolve_websites(recheck=True)
    assert stats["websites_downgraded"] == 1 and db.get_company(cid)["website_confidence"] == "unsure" and db.get_company(cid)["website"] == "https://acme.com"


def now_iso_():
    from raiseradar.util import now_iso
    return now_iso()


def test_first_run_notice_only_on_an_empty_database(env, caplog):
    import logging
    world, cfg, db, make = env
    caplog.set_level(logging.INFO)
    make().run_all(news=True, yc=False)
    assert "First run on an empty database" in caplog.text and "30 minutes" in caplog.text
    assert "[1/5] reading funding news" in caplog.text and "[5/5] fetching open roles" in caplog.text
    caplog.clear()
    make().run_all(news=True, yc=False)                     # companies exist now: no first-run warning
    assert "First run on an empty database" not in caplog.text
