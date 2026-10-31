from raiseradar.db import DB
from raiseradar.models import Article
from raiseradar.profile import Profile, company_score
from raiseradar.sources import news, yc
from raiseradar.util import now_iso


# ----------------------------------------------------------------------------- DB
def test_upsert_company_merges_without_overwriting(db):
    cid, created = db.upsert_company("Acme Inc", origin="news", description="Makes anvils", country=None)
    assert created
    cid2, created2 = db.upsert_company("ACME", origin="yc", description="Other text", country="US", website="https://acme.com",
                                       extra={"yc_batch": "W20"})
    assert cid2 == cid and not created2
    c = db.get_company(cid)
    assert c["description"] == "Makes anvils"              # existing value kept
    assert c["country"] == "US" and c["website"] == "https://acme.com"   # empty values filled
    assert c["origins"] == ["news", "yc"] and c["extra"] == {"yc_batch": "W20"}


def test_funding_dedupes_per_source(db):
    cid, _ = db.upsert_company("Acme")
    kw = dict(round="Series B", amount_usd_m=30, amount_text="$30M", investors=["Accel"], lead_investors=[], announced_at=now_iso(),
              source_name="TC", headline="h")
    assert db.add_funding(cid, source_url="u1", **kw)
    assert not db.add_funding(cid, source_url="u1", **kw)
    assert db.add_funding(cid, source_url="u2", **kw)
    assert len(db.funding_for(cid)) == 2 and db.funding_for(cid)[0]["investors"] == ["Accel"]


def job(i, title="Engineer"):
    return {"id": f"j{i}", "title": title, "description": "d", "matches_profile": 1}


def test_sync_jobs_tracks_new_updated_and_closed(db):
    cid, _ = db.upsert_company("Acme")
    assert db.sync_jobs(cid, [job(1), job(2)], "2026-10-01T00:00:00+00:00") == (2, 0, 0)
    assert db.sync_jobs(cid, [job(2, "Senior Engineer"), job(3)], "2026-10-02T00:00:00+00:00") == (1, 1, 1)
    active = {j["id"]: j for j in db.jobs_for(cid)}
    assert set(active) == {"j2", "j3"} and active["j2"]["title"] == "Senior Engineer"
    assert active["j2"]["first_seen"].startswith("2026-10-01")       # first_seen survives updates
    assert len(db.jobs_for(cid, active_only=False)) == 3
    assert db.sync_jobs(cid, [job(1)], "2026-10-03T00:00:00+00:00")[0] == 0    # reopened job is not "new"
    assert {j["id"] for j in db.jobs_for(cid)} == {"j1"}


def test_articles_and_cache(db):
    a = Article("a1", "t", "", "u", "p", None, "f")
    assert db.known_article_ids(["a1"]) == set()
    db.mark_article(a, True)
    assert db.known_article_ids(["a1", "a2"]) == {"a1"}
    db.cache_put("k", "{}", "m")
    assert db.cache_get("k") == "{}" and db.cache_get("zz") is None


def test_runs(db):
    r1 = db.start_run(); db.finish_run(r1, {"x": 1})
    r2 = db.start_run()
    assert db.last_run_started(before_id=r2) is not None


# ----------------------------------------------------------------------------- profile
def test_profile_matching():
    p = Profile.from_config({"roles": ["backend", "full.?stack"], "exclude_titles": ["intern", "manager"], "max_experience_years": 3,
                             "locations": ["remote", "india"]})
    assert p.active
    ok = {"title": "Backend Engineer", "location": "Bengaluru, India", "exp_min": 2, "remote": None}
    assert p.matches(ok)
    assert not p.matches({**ok, "title": "Sales Executive"})
    assert not p.matches({**ok, "title": "Backend Intern"})
    assert not p.matches({**ok, "exp_min": 5})
    assert p.matches({**ok, "exp_min": None})                                   # unknown experience stays
    assert not p.matches({**ok, "location": "Berlin"})
    assert p.matches({**ok, "location": "Berlin", "remote": 1})                 # remote-flagged job
    assert Profile.from_config({}).active is False and Profile.from_config({}).matches({"title": "Anything"})


def test_bad_regex_in_profile_is_treated_literally():
    p = Profile.from_config({"roles": ["c++ (senior"]})
    assert p.matches({"title": "Hiring C++ (Senior dev"})


def test_company_score_orders_sensibly():
    recent = [{"round": "Series B", "announced_at": now_iso()}]
    old = [{"round": "Series B", "announced_at": "2025-01-01T00:00:00+00:00"}]
    assert company_score(recent, 10, 5) > company_score(old, 10, 5) > company_score([], 0, 0)
    assert company_score(recent, 10, 5) > company_score(recent, 10, 0)
    assert company_score([], 10, 3, is_yc=True) > company_score([], 10, 3, is_yc=False)


# ----------------------------------------------------------------------------- news / yc
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>Acme raises $30M Series B led by Accel - TechCrunch</title><link>https://news.google.com/rss/articles/AAA</link><guid>AAA</guid>
<pubDate>Wed, 28 Oct 2026 13:00:00 GMT</pubDate><description>&lt;a href="x"&gt;Acme raises $30M Series B led by Accel&lt;/a&gt;</description><source url="https://techcrunch.com">TechCrunch</source></item>
<item><title>Weather is nice today - BBC</title><link>https://news.google.com/rss/articles/BBB</link><guid>BBB</guid><pubDate>Wed, 28 Oct 2026 13:00:00 GMT</pubDate></item>
<item><title>Acme raises $30M Series B led by Accel - Yahoo Finance</title><link>https://news.google.com/rss/articles/CCC</link><guid>CCC</guid><pubDate>Wed, 28 Oct 2026 14:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_parse_feed_splits_publisher_and_dates():
    arts = news.parse_feed(RSS, "https://news.google.com/rss/search?q=x")
    assert arts[0].title == "Acme raises $30M Series B led by Accel" and arts[0].publisher == "TechCrunch"
    assert arts[0].published_at == "2026-10-28T13:00:00+00:00" and arts[0].summary == ""
    assert len({a.id for a in arts}) == 3


def test_prefilter_and_google_url():
    arts = news.parse_feed(RSS, "https://news.google.com/rss/search?q=x")
    kept = news.prefilter(arts, ["Series B", "Series C"], True)
    assert [a.publisher for a in kept] == ["TechCrunch", "Yahoo Finance"]
    assert len(news.prefilter(arts, ["Series B"], False)) == 2
    url = news.google_news_url('"Series B" raises', "IN:IN:en", 7)
    assert "gl=IN" in url and "ceid=IN:en" in url and "hl=en-IN" in url and "when%3A7d" in url


def test_direct_feed_publisher_uses_feed_title():
    arts = news.parse_feed(RSS.replace("<title>Feed</title>", "<title>Sifted - Funding</title>"), "https://sifted.eu/feed")
    assert arts[0].publisher == "Sifted" and arts[0].title.endswith("TechCrunch")   # direct feeds keep the headline intact


def test_yc_selection_and_conversion():
    rows = [
        {"name": "Big", "website": "https://big.com", "team_size": 300, "batch": "Winter 2016", "status": "Active", "one_liner": "Does big", "all_locations": "San Francisco, CA, USA", "tags": ["B2B"], "url": "https://yc/big"},
        {"name": "Tiny", "website": "https://tiny.com", "team_size": 5, "batch": "Winter 2016", "status": "Active"},
        {"name": "Young", "website": "https://young.com", "team_size": 60, "batch": "Summer 2025", "status": "Active"},
        {"name": "Dead", "website": "https://dead.com", "team_size": 60, "batch": "Winter 2016", "status": "Inactive"},
        {"name": "NoSite", "team_size": 60, "batch": "Winter 2016", "status": "Active"},
        {"name": "Mid", "website": "https://mid.com", "team_size": 80, "batch": "Summer 2019", "status": "Active"},
    ]
    picked = yc.select_companies(rows, min_team=25, max_team=800, max_batch_year=2023, limit=10)
    assert [r["name"] for r in picked] == ["Big", "Mid"]
    c = yc.to_company(picked[0])
    assert c["country"] == "USA" and c["city"] == "San Francisco" and c["extra"]["yc_batch"] == "Winter 2016"


def test_env_model_overrides_config(tmp_path, monkeypatch):
    from raiseradar.config import load_config
    (tmp_path / "config.toml").write_text('[llm]\nmodels = ["a/x:free"]\n')
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    assert load_config(tmp_path / "config.toml")["llm"]["models"] == ["a/x:free"]
    monkeypatch.setenv("OPENROUTER_MODEL", " b/y:free , c/z:free ,")
    assert load_config(tmp_path / "config.toml")["llm"]["models"] == ["b/y:free", "c/z:free"]


def test_duplicate_companies_with_the_same_website_are_merged(db):
    a, _ = db.upsert_company("Upwind", origin="yc", website="https://www.upwind.io", domain="www.upwind.io", description="cloud security")
    b, _ = db.upsert_company("Upwind Security", origin="news", country="Israel")
    db.update_company(b, website="https://upwind.io", domain="upwind.io")
    db.add_funding(b, round="Series B", amount_usd_m=250, amount_text="$250M", investors=["Bessemer"], lead_investors=[], announced_at=now_iso(),
                   source_name="TC", source_url="u", headline="h")
    db.sync_jobs(b, [job(1)], now_iso())
    unrelated, _ = db.upsert_company("Other", website="https://other.com", domain="other.com")
    assert db.merge_duplicates_by_domain() == [("Upwind Security", "Upwind")]
    assert db.find_company("Upwind Security") is None
    keep = db.get_company(a)
    assert keep["origins"] == ["yc", "news"] and keep["country"] == "Israel" and keep["description"] == "cloud security"
    assert [f["round"] for f in db.funding_for(a)] == ["Series B"] and db.jobs_for(b) == []
    assert db.get_company(unrelated) is not None and db.merge_duplicates_by_domain() == []     # idempotent


def test_upsert_with_a_known_domain_joins_the_existing_company(db):
    a, _ = db.upsert_company("Upwind Security", origin="news", website="https://upwind.io", domain="upwind.io")
    b, created = db.upsert_company("Upwind", origin="yc", website="https://www.upwind.io", domain="www.upwind.io")
    assert b == a and not created and db.get_company(a)["origins"] == ["news", "yc"]


def test_old_database_is_migrated_in_place(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE articles(id TEXT PRIMARY KEY, url TEXT, title TEXT, publisher TEXT, published_at TEXT, feed TEXT, processed_at TEXT, is_funding INTEGER);
      CREATE TABLE jobs(id TEXT PRIMARY KEY, company_id INTEGER NOT NULL, title TEXT, department TEXT, location TEXT, remote INTEGER, employment_type TEXT,
        url TEXT, apply_url TEXT, posted_at TEXT, description TEXT, salary_text TEXT, exp_min REAL, exp_max REAL, exp_source TEXT, exp_mentions TEXT,
        seniority TEXT, role_family TEXT, matches_profile INTEGER, first_seen TEXT, last_seen TEXT, active INTEGER NOT NULL DEFAULT 1);
      INSERT INTO articles(id,title,processed_at) VALUES('a1','old article','2026-10-01T00:00:00+00:00');
      INSERT INTO jobs(id,company_id,title,active) VALUES('j1',1,'Engineer',1);
    """)
    con.commit(); con.close()
    d = DB(path)
    assert {r[1] for r in d.conn.execute("PRAGMA table_info(jobs)")} >= {"workplace"}
    assert {r[1] for r in d.conn.execute("PRAGMA table_info(articles)")} >= {"attempts"}
    assert d.known_article_ids(["a1"]) == {"a1"}                       # existing rows survive and still count as processed
    assert d.conn.execute("SELECT title FROM jobs").fetchone()[0] == "Engineer"
    DB(path).close()                                                   # opening twice is harmless


def test_failed_articles_are_not_treated_as_known(db):
    a = Article("x1", "t", "", "u", "p", None, "f")
    db.bump_attempts(a); db.bump_attempts(a)
    assert db.known_article_ids(["x1"]) == set() and db.article_attempts(["x1"]) == {"x1": 2}
    db.mark_article(a, False)
    assert db.known_article_ids(["x1"]) == {"x1"}


def test_user_agent_contact(monkeypatch):
    from raiseradar.config import Config
    monkeypatch.delenv("RAISERADAR_CONTACT", raising=False)
    cfg = Config()
    assert "+personal-use" in cfg.user_agent and "@" not in cfg.user_agent      # blank contact sends no address
    cfg.data["general"]["contact"] = "me@example.com"
    assert "+me@example.com" in cfg.user_agent
    monkeypatch.setenv("RAISERADAR_CONTACT", "env@example.com")                 # .env wins, so the address can stay out of git
    assert "+env@example.com" in cfg.user_agent
