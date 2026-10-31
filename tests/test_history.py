"""Job history: when a role counts as closed, the open-roles trend, and what the report shows of it."""
import sqlite3

from raiseradar import report
from raiseradar.db import DB
from raiseradar.demo import build_demo_data
from raiseradar.report import open_change


def job(i, title="Engineer"):
    return {"id": f"j{i}", "title": title, "description": "a long description", "matches_profile": 1}


def day(n):
    return f"2026-10-{n:02d}T09:00:00+00:00"


def test_a_role_must_be_missing_twice_in_a_row_to_close(db):
    cid, _ = db.upsert_company("Acme")
    db.sync_jobs(cid, [job(1), job(2)], day(1), first_fetch=True)
    assert db.sync_jobs(cid, [job(2)], day(2))[2] == 0
    assert db.sync_jobs(cid, [job(1), job(2)], day(3))[2] == 0         # came back: the miss is forgotten
    assert db.sync_jobs(cid, [job(2)], day(4))[2] == 0
    assert db.sync_jobs(cid, [job(2)], day(5))[2] == 1                 # missing twice in a row
    gone = db.closed_jobs(cid)
    assert [j["id"] for j in gone] == ["j1"] and gone[0]["closed_at"] == day(5)
    assert {j["id"] for j in db.jobs_for(cid)} == {"j2"}


def test_a_role_that_comes_back_is_marked_as_listed_again(db):
    cid, _ = db.upsert_company("Acme")
    db.sync_jobs(cid, [job(1)], day(1), first_fetch=True)
    db.sync_jobs(cid, [job(2)], day(2)); db.sync_jobs(cid, [job(2)], day(3))
    assert db.closed_jobs(cid)[0]["id"] == "j1"
    new, _upd, closed = db.sync_jobs(cid, [job(1), job(2)], day(4))
    assert new == 0                                                    # not a new role, an old one reopened
    back = next(j for j in db.jobs_for(cid) if j["id"] == "j1")
    assert back["reopened"] == 1 and back["closed_at"] is None and back["active"] == 1


def test_most_roles_vanishing_at_once_is_not_believed_for_a_while(db):
    """An ATS migration or a broken parser looks like 'everything closed'. Wait longer, and keep it out of the history."""
    cid, _ = db.upsert_company("Acme")
    db.sync_jobs(cid, [job(i) for i in range(10)], day(1), first_fetch=True)
    for d in (2, 3, 4):                                                # three fetches that list nothing
        assert db.sync_jobs(cid, [], day(d))[2] == 0
    assert len(db.jobs_for(cid)) == 10
    assert db.open_counts(cid) == [("2026-10-01", 10)]                 # the bad fetches left no zero in the trend
    assert db.sync_jobs(cid, [], day(5))[2] == 10                      # still empty a fourth time: believed
    db.sync_jobs(cid, [], day(6))
    assert db.open_counts(cid)[-1] == ("2026-10-06", 0)


def test_small_companies_are_not_treated_as_suspicious(db):
    cid, _ = db.upsert_company("Tiny")
    db.sync_jobs(cid, [job(1), job(2), job(3)], day(1), first_fetch=True)
    db.sync_jobs(cid, [], day(2))
    assert db.sync_jobs(cid, [], day(3))[2] == 3                       # fewer than 5 roles: the normal two fetches


def test_one_history_point_per_day_the_last_fetch_wins(db):
    cid, _ = db.upsert_company("Acme")
    db.sync_jobs(cid, [job(1)], "2026-10-01T06:00:00+00:00", first_fetch=True)
    db.sync_jobs(cid, [job(1), job(2)], "2026-10-01T18:00:00+00:00")
    db.sync_jobs(cid, [job(1), job(2), job(3)], "2026-10-02T06:00:00+00:00")
    assert db.open_counts(cid) == [("2026-10-01", 2), ("2026-10-02", 3)]
    assert db.open_counts(cid, since_day="2026-10-02") == [("2026-10-02", 3)]


def test_open_change_needs_enough_history_and_uses_the_window():
    assert open_change([]) is None and open_change([["2026-10-01", 4]]) is None
    assert open_change([["2026-10-01", 4], ["2026-10-03", 9]]) is None             # only 2 days of history
    assert open_change([["2026-10-01", 4], ["2026-10-04", 9]]) == {"change": 5, "days": 3}
    long = [["2026-08-01", 2], ["2026-09-25", 10], ["2026-10-20", 12], ["2026-10-25", 8]]
    assert open_change(long, window=30) == {"change": -2, "days": 30}              # compared with 30 days ago, not with August
    assert open_change([["2026-10-01", 5], ["2026-10-10", 5]]) == {"change": 0, "days": 9}


def test_descriptions_of_old_closed_roles_are_trimmed_but_the_rows_stay(db):
    cid, _ = db.upsert_company("Acme")
    db.sync_jobs(cid, [job(1), job(2)], day(1), first_fetch=True)
    db.sync_jobs(cid, [job(2)], day(2)); db.sync_jobs(cid, [job(2)], day(3))       # j1 closes on day 3
    assert db.trim_history("2026-10-02T00:00:00+00:00") == 0                       # closed after the cut-off: kept
    assert db.trim_history("2026-11-01T00:00:00+00:00") == 1
    rows = {j["id"]: j for j in db.jobs_for(cid, active_only=False)}
    assert rows["j1"]["description"] == "" and rows["j1"]["title"] == "Engineer"
    assert rows["j2"]["description"] == "a long description"                        # open roles are never trimmed
    assert db.trim_history("2026-11-01T00:00:00+00:00") == 0


def test_an_old_database_gets_the_new_columns_and_keeps_its_closed_dates(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE companies(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, norm_name TEXT UNIQUE, origins TEXT DEFAULT '[]', extra TEXT DEFAULT '{}', created_at TEXT);
        CREATE TABLE jobs(id TEXT PRIMARY KEY, company_id INTEGER NOT NULL, title TEXT, first_seen TEXT, last_seen TEXT, active INTEGER NOT NULL DEFAULT 1);
        INSERT INTO companies(name, norm_name) VALUES('Acme', 'acme');
        INSERT INTO jobs VALUES('old-open', 1, 'A', '2026-09-01', '2026-10-01', 1), ('old-closed', 1, 'B', '2026-09-01', '2026-09-20', 0);
    """)
    raw.commit(); raw.close()
    db = DB(path)
    rows = {j["id"]: j for j in db.jobs_for(1, active_only=False)}
    assert rows["old-closed"]["closed_at"] == "2026-09-20" and rows["old-open"]["closed_at"] is None
    assert rows["old-open"]["missed"] == 0 and rows["old-open"]["reopened"] == 0
    db.close()


def test_merging_two_companies_drops_the_history_of_the_merged_one(db):
    a, _ = db.upsert_company("Upwind")
    b, _ = db.upsert_company("Upwind Security")
    db.sync_jobs(b, [job(1)], day(1), first_fetch=True)
    db.merge_companies(a, b)
    assert db.open_counts(b) == [] and db.get_company(b) is None


def test_the_report_carries_trends_and_closed_roles():
    data = build_demo_data()
    by = {c["name"]: c for c in data["companies"]}
    q = by["Quillstack"]
    assert q["open_change"] == {"change": 3, "days": 12} and len(q["trend"]) == 5
    assert {c["title"] for c in by["Tessera Health"]["closed"]} == {"Clinical Data Analyst", "Platform Engineer"}
    gone = by["Parallax Pay"]["closed"][0]
    assert gone["title"] == "Backend Engineer, Payments" and gone["closed_at"] and gone["url"].startswith("https://example.com/")
    assert by["Foxglove Security"]["closed"] == [] and by["Foxglove Security"]["open_change"] == {"change": 0, "days": 12}


def test_digest_and_csv_mention_hiring_growth(tmp_path):
    info = report.write_files(build_demo_data(), tmp_path)
    digest = (tmp_path / "digest.md").read_text()
    assert "## Hiring up (" in digest and "Quillstack" in digest and "+3 open roles in 12 days" in digest
    header = (tmp_path / "companies.csv").read_text().splitlines()[0]
    assert "open_change,open_change_days" in header
    assert info["companies"] == 15
