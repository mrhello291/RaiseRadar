"""Your applications: which roles you saved, applied to, are interviewing for, or want to hide.

Kept in its own SQLite file (`data/applications.db` by default), never in the main database. The main database is a
cache that can be rebuilt or wiped and that merges and deletes rows; this file holds the only data in the project that
cannot be re-fetched. Each entry copies the company, title and link, so it still reads correctly after the role is
closed or the company disappears from the main database.

Nothing here is written into the CSV files, data.json or the digest, and public reports never contain it.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .util import days_since, now_iso

STATUSES = ["saved", "applied", "interviewing", "offer", "rejected", "ignored"]
LABELS = {"saved": "Saved", "applied": "Applied", "interviewing": "Interviewing", "offer": "Offer", "rejected": "Rejected", "ignored": "Not interested"}
ALIASES = {"save": "saved", "apply": "applied", "interview": "interviewing", "interviews": "interviewing", "reject": "rejected",
           "skip": "ignored", "hide": "ignored", "ignore": "ignored", "not-interested": "ignored", "notinterested": "ignored", "no": "ignored"}
MAX_NOTE = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key TEXT UNIQUE,
  company TEXT NOT NULL, title TEXT NOT NULL, location TEXT, url TEXT,
  status TEXT NOT NULL, applied_at TEXT, note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  status TEXT NOT NULL, at TEXT NOT NULL, note TEXT
);
"""


class TrackerError(ValueError):
    """A request that cannot be honoured (unknown status, no such entry, bad date). The message is meant for the user."""


def job_code(job_key: str) -> str:
    """Short, stable code for a job id, small enough to type: `raiseradar track set applied 3fa9c1d2`."""
    return hashlib.sha1(job_key.encode("utf-8")).hexdigest()[:8]


def parse_status(text: str) -> str:
    s = (text or "").strip().lower().replace(" ", "-")
    s = ALIASES.get(s, s)
    if s not in STATUSES:
        raise TrackerError(f"unknown status {text!r}; use one of: " + ", ".join(STATUSES))
    return s


def parse_date(text: str) -> str:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date().isoformat()
    except (ValueError, AttributeError):
        raise TrackerError(f"{text!r} is not a date; use YYYY-MM-DD") from None


def _backdate(applied_at: str | None, status: str, now: str) -> str:
    """When something happened. An application you add today but sent on an earlier date happened on that date, so the
    "no news for N days" reminder counts from then."""
    return f"{applied_at}T00:00:00+00:00" if applied_at and status == "applied" and applied_at < now[:10] else now


class Tracker:
    """The applications file. Every call opens and closes its own connection, so the web server's threads and the CLI
    can use it at the same time. A missing file is simply 'nothing tracked yet' and is not created by reading."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _conn(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def exists(self) -> bool:
        return self.path.is_file()

    # ------------------------------------------------------------------ reading
    def list(self, status: str | None = None) -> list[dict]:
        if not self.exists():
            return []
        with self._conn() as c:
            rows = c.execute("SELECT * FROM applications" + (" WHERE status=?" if status else "") + " ORDER BY updated_at DESC, id DESC",
                             (status,) if status else ()).fetchall()
            events: dict[int, list[dict]] = {}
            for e in c.execute("SELECT app_id, status, at, note FROM app_events ORDER BY id"):
                events.setdefault(e["app_id"], []).append({"status": e["status"], "at": e["at"], "note": e["note"] or ""})
        return [{**dict(r), "code": job_code(r["job_key"]) if r["job_key"] else "", "events": events.get(r["id"], [])} for r in rows]

    def get(self, app_id: int) -> dict | None:
        return next((a for a in self.list() if a["id"] == app_id), None)

    # ------------------------------------------------------------------ writing
    def set(self, *, job_key: str | None = None, app_id: int | None = None, status: str | None = None, note: str | None = None,
            applied_at: str | None = None, meta: dict | None = None) -> dict:
        """Create or update one entry, found by `app_id` or `job_key`. A new entry needs a status and `meta`
        (company, title, and optionally location and url, taken from the job). Returns the entry."""
        if status is not None:
            status = parse_status(status)
        if applied_at:
            applied_at = parse_date(applied_at)
        if note is not None:
            note = note.strip()[:MAX_NOTE]
        now = now_iso()
        with self._conn(write=True) as c:
            row = None
            if app_id is not None:
                row = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
            elif job_key:
                row = c.execute("SELECT * FROM applications WHERE job_key=?", (job_key,)).fetchone()
            if row is None:
                if app_id is not None:
                    raise TrackerError(f"no tracked entry #{app_id}")
                if not job_key or not meta or not meta.get("title") or not meta.get("company"):
                    raise TrackerError("cannot track a role without its company and title")
                if status is None:
                    raise TrackerError("say what to do with it: " + ", ".join(STATUSES))
                stamp = _backdate(applied_at, status, now)
                cur = c.execute(
                    "INSERT INTO applications(job_key, company, title, location, url, status, applied_at, note, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (job_key, meta["company"], meta["title"], meta.get("location") or "", meta.get("url") or "", status,
                     applied_at or (now[:10] if status == "applied" else None), note or "", now, stamp))
                c.execute("INSERT INTO app_events(app_id, status, at, note) VALUES(?,?,?,?)", (cur.lastrowid, status, stamp, note or ""))
                new_id = cur.lastrowid
            else:
                new_id = row["id"]
                sets, vals = [], []
                if status is not None and status != row["status"]:
                    sets.append("status=?"); vals.append(status)
                    c.execute("INSERT INTO app_events(app_id, status, at, note) VALUES(?,?,?,?)", (new_id, status, now, note or ""))
                    if status == "applied" and not row["applied_at"] and not applied_at:
                        sets.append("applied_at=?"); vals.append(now[:10])
                if applied_at:
                    sets.append("applied_at=?"); vals.append(applied_at)
                if note is not None and note != row["note"]:
                    sets.append("note=?"); vals.append(note)
                if sets:
                    c.execute(f"UPDATE applications SET {', '.join(sets)}, updated_at=? WHERE id=?", (*vals, now, new_id))
        return self.get(new_id)

    def add_manual(self, company: str, title: str, *, url: str = "", location: str = "", status: str = "applied",
                   note: str = "", applied_at: str | None = None) -> dict:
        """A role found somewhere other than the report (a referral, a job board, an email)."""
        company, title = company.strip(), title.strip()
        if not company or not title:
            raise TrackerError("a manual entry needs a company and a title")
        status = parse_status(status)
        applied = parse_date(applied_at) if applied_at else None
        now = now_iso()
        stamp = _backdate(applied, status, now)
        with self._conn(write=True) as c:
            cur = c.execute(
                "INSERT INTO applications(job_key, company, title, location, url, status, applied_at, note, created_at, updated_at) "
                "VALUES(NULL,?,?,?,?,?,?,?,?,?)",
                (company, title, location.strip(), url.strip(), status, applied or (now[:10] if status == "applied" else None),
                 note.strip()[:MAX_NOTE], now, stamp))
            c.execute("INSERT INTO app_events(app_id, status, at, note) VALUES(?,?,?,?)", (cur.lastrowid, status, stamp, note.strip()[:MAX_NOTE]))
            new_id = cur.lastrowid
        return self.get(new_id)

    def delete(self, *, job_key: str | None = None, app_id: int | None = None) -> bool:
        if not self.exists():
            return False
        with self._conn(write=True) as c:
            cur = c.execute("DELETE FROM applications WHERE " + ("id=?" if app_id is not None else "job_key=?"),
                            (app_id if app_id is not None else job_key,))
            return cur.rowcount > 0


# --------------------------------------------------------------------------- joining with the jobs database
def listing_state(db, job_key: str | None) -> str:
    """Is the role still on the company's board? 'open', 'closed' (no longer listed) or 'unknown' (manual entry, or
    the role is not in the database any more)."""
    if not job_key:
        return "unknown"
    r = db.conn.execute("SELECT active FROM jobs WHERE id=?", (job_key,)).fetchone()
    if r is None:
        return "unknown"
    return "open" if r["active"] else "closed"


def app_views(tracker: Tracker, db) -> list[dict]:
    """Every tracked entry with whether its role is still listed. This is what the report and the web page show."""
    out = []
    for a in tracker.list():
        out.append({k: a[k] for k in ("id", "code", "company", "title", "location", "url", "status", "applied_at", "note", "created_at", "updated_at", "events")}
                   | {"key": a["job_key"], "listing": listing_state(db, a["job_key"])})
    return out


def find_jobs(db, query: str, limit: int = 20) -> list[dict]:
    """Jobs matching `query`: a full job id, an 8-character code, or words that all appear in the company name or title.
    Open roles first. Each result: key, code, company, title, location, url, active."""
    q = query.strip()
    if not q:
        return []
    rows = db.conn.execute("SELECT j.id, j.title, j.location, j.url, j.active, c.name AS company FROM jobs j JOIN companies c ON c.id=j.company_id").fetchall()
    exact = [r for r in rows if r["id"] == q]
    if not exact and re.fullmatch(r"[0-9a-f]{8}", q.lower()):
        exact = [r for r in rows if job_code(r["id"]) == q.lower()]
    if exact:
        hits = exact
    else:
        words = [w for w in norm_name_words(q) if w]
        hits = [r for r in rows if words and all(w in (r["company"] + " " + r["title"]).lower() for w in words)]
    hits.sort(key=lambda r: (not r["active"], r["company"].lower(), r["title"].lower()))
    return [{"key": r["id"], "code": job_code(r["id"]), "company": r["company"], "title": r["title"], "location": r["location"] or "",
             "url": r["url"] or "", "active": bool(r["active"])} for r in hits[:limit]]


def norm_name_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+#.]+", text.lower())


def job_meta(db, job_key: str) -> dict | None:
    """Company, title, location and link of a job, taken from the database (never trusted from a web request)."""
    r = db.conn.execute("SELECT j.title, j.location, j.url, c.name AS company FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=?", (job_key,)).fetchone()
    return None if r is None else {"company": r["company"], "title": r["title"], "location": r["location"] or "", "url": r["url"] or ""}


def needs_follow_up(app: dict, days: float) -> bool:
    """Applied, and nothing has changed for `days` days."""
    if app["status"] != "applied" or days <= 0:
        return False
    age = days_since(app["updated_at"])
    return age is not None and age >= days
