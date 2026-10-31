"""SQLite persistence. Only the main thread writes; worker threads return data."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .util import now_iso, norm_name, registrable_domain

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  id TEXT PRIMARY KEY, url TEXT, title TEXT, publisher TEXT, published_at TEXT, feed TEXT,
  processed_at TEXT, is_funding INTEGER, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS companies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, norm_name TEXT UNIQUE NOT NULL,
  website TEXT, domain TEXT, website_confidence TEXT,
  description TEXT, country TEXT, city TEXT,
  origins TEXT NOT NULL DEFAULT '[]', extra TEXT NOT NULL DEFAULT '{}',
  careers_url TEXT, ats TEXT, ats_slug TEXT, ats_confidence TEXT,
  resolve_checked_at TEXT, careers_checked_at TEXT, jobs_fetched_at TEXT, jobs_status TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS funding(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  round TEXT, amount_usd_m REAL, amount_text TEXT,
  investors TEXT, lead_investors TEXT,
  announced_at TEXT, source_name TEXT, source_url TEXT, headline TEXT,
  UNIQUE(company_id, round, source_url)
);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
  title TEXT, department TEXT, location TEXT, remote INTEGER, employment_type TEXT,
  url TEXT, apply_url TEXT, posted_at TEXT, description TEXT, salary_text TEXT,
  exp_min REAL, exp_max REAL, exp_source TEXT, exp_mentions TEXT,
  seniority TEXT, role_family TEXT, workplace TEXT, matches_profile INTEGER,
  first_seen TEXT, last_seen TEXT, active INTEGER NOT NULL DEFAULT 1,
  baseline INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id, active);
CREATE TABLE IF NOT EXISTS job_counts(
  company_id INTEGER NOT NULL REFERENCES companies(id), day TEXT NOT NULL, at TEXT, open_count INTEGER NOT NULL,
  PRIMARY KEY(company_id, day)
);
CREATE TABLE IF NOT EXISTS llm_cache(key TEXT PRIMARY KEY, response TEXT, model TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, stats TEXT);
"""

_COMPANY_JSON = ("origins", "extra")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in _COMPANY_JSON:
        if k in d and isinstance(d[k], str):
            d[k] = json.loads(d[k] or ("[]" if k == "origins" else "{}"))
    return d


class DB:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created (keeps existing data)."""
        for table, col, decl in (("jobs", "workplace", "TEXT"), ("articles", "attempts", "INTEGER NOT NULL DEFAULT 0"),
                                 ("jobs", "baseline", "INTEGER NOT NULL DEFAULT 0"), ("jobs", "closed_at", "TEXT"),
                                 ("jobs", "missed", "INTEGER NOT NULL DEFAULT 0"), ("jobs", "reopened", "INTEGER NOT NULL DEFAULT 0")):
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                if col == "baseline":   # jobs stored before this existed were all found by a first fetch: none of them is "new"
                    self.conn.execute("UPDATE jobs SET baseline=1")
                if col == "closed_at":  # roles closed before history existed: the last time we saw them is the best date we have
                    self.conn.execute("UPDATE jobs SET closed_at=last_seen WHERE active=0")

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # -- articles -------------------------------------------------------------------
    def known_article_ids(self, ids: Iterable[str]) -> set[str]:
        ids = list(ids)
        found: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            q = ",".join("?" * len(chunk))
            found |= {r[0] for r in self.conn.execute(f"SELECT id FROM articles WHERE processed_at IS NOT NULL AND id IN ({q})", chunk)}
        return found

    def article_attempts(self, ids: Iterable[str]) -> dict[str, int]:
        ids = list(ids)
        out: dict[str, int] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            q = ",".join("?" * len(chunk))
            out.update({r[0]: r[1] for r in self.conn.execute(f"SELECT id, attempts FROM articles WHERE processed_at IS NULL AND id IN ({q})", chunk)})
        return out

    def bump_attempts(self, art) -> None:
        """An article the LLM could not answer for. After a few tries the pipeline gives up on it."""
        self.conn.execute(
            "INSERT INTO articles(id,url,title,publisher,published_at,feed,attempts) VALUES(?,?,?,?,?,?,1) "
            "ON CONFLICT(id) DO UPDATE SET attempts=attempts+1",
            (art.id, art.url, art.title, art.publisher, art.published_at, art.feed),
        )
        self.conn.commit()

    def mark_article(self, art, is_funding: bool | None) -> None:
        """Record an article as handled. is_funding=None means 'skipped by prefilter'."""
        self.conn.execute(
            "INSERT OR REPLACE INTO articles(id,url,title,publisher,published_at,feed,processed_at,is_funding) VALUES(?,?,?,?,?,?,?,?)",
            (art.id, art.url, art.title, art.publisher, art.published_at, art.feed, now_iso(),
             None if is_funding is None else int(is_funding)),
        )
        self.conn.commit()

    # -- companies ------------------------------------------------------------------
    def get_company(self, company_id: int) -> dict | None:
        return _row(self.conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone())

    def find_company(self, name: str) -> dict | None:
        return _row(self.conn.execute("SELECT * FROM companies WHERE norm_name=?", (norm_name(name),)).fetchone())

    def upsert_company(self, name: str, *, origin: str | None = None, extra: dict | None = None, **fields: Any) -> tuple[int, bool]:
        """Insert or merge. Existing non-empty values are kept; empty ones are filled. Returns (id, created)."""
        key = norm_name(name)
        if not key:
            raise ValueError("empty company name")
        cur = self.conn.execute("SELECT * FROM companies WHERE norm_name=?", (key,)).fetchone()
        if cur is None and fields.get("domain"):
            twin = self.find_by_domain(fields["domain"])
            if twin:
                key = twin["norm_name"]
                cur = self.conn.execute("SELECT * FROM companies WHERE norm_name=?", (key,)).fetchone()
        if cur is None:
            origins = [origin] if origin else []
            self.conn.execute(
                "INSERT INTO companies(name,norm_name,origins,extra,created_at) VALUES(?,?,?,?,?)",
                (name.strip(), key, json.dumps(origins), json.dumps(extra or {}), now_iso()),
            )
            cid = self.conn.execute("SELECT id FROM companies WHERE norm_name=?", (key,)).fetchone()[0]
            created = True
            cur = self.conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        else:
            cid, created = cur["id"], False
        cur = dict(cur)
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if v not in (None, "") and cur.get(k) in (None, ""):
                updates[k] = v
        origins = json.loads(cur["origins"] or "[]")
        if origin and origin not in origins:
            updates["origins"] = json.dumps(origins + [origin])
        if extra:
            merged = {**extra, **json.loads(cur["extra"] or "{}")}
            updates["extra"] = json.dumps(merged)
        if updates:
            self.update_company(cid, **updates)
        self.conn.commit()
        return cid, created

    def update_company(self, company_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if k in _COMPANY_JSON and not isinstance(v, str) else v for k, v in fields.items()]
        self.conn.execute(f"UPDATE companies SET {cols} WHERE id=?", (*vals, company_id))
        self.conn.commit()

    def find_by_domain(self, domain: str, exclude_id: int | None = None) -> dict | None:
        want = registrable_domain(domain)
        for r in self.conn.execute("SELECT * FROM companies WHERE domain IS NOT NULL AND domain != '' ORDER BY id"):
            if r["id"] != exclude_id and registrable_domain(r["domain"]) == want:
                return _row(r)
        return None

    def merge_companies(self, keep_id: int, drop_id: int) -> None:
        """Fold company `drop_id` into `keep_id`: funding events, origins, missing fields. Its jobs are dropped (the kept
        company fetches the same board itself)."""
        keep, drop = self.get_company(keep_id), self.get_company(drop_id)
        if not keep or not drop:
            return
        self.conn.execute("UPDATE OR IGNORE funding SET company_id=? WHERE company_id=?", (keep_id, drop_id))
        self.conn.execute("DELETE FROM funding WHERE company_id=?", (drop_id,))
        self.conn.execute("DELETE FROM jobs WHERE company_id=?", (drop_id,))
        self.conn.execute("DELETE FROM job_counts WHERE company_id=?", (drop_id,))
        fill = {k: drop[k] for k in ("description", "country", "city", "website", "domain", "website_confidence") if not keep.get(k) and drop.get(k)}
        fill["origins"] = json.dumps(list(dict.fromkeys(keep["origins"] + drop["origins"])))
        fill["extra"] = json.dumps({**drop["extra"], **keep["extra"]})
        self.conn.execute("DELETE FROM companies WHERE id=?", (drop_id,))
        self.update_company(keep_id, **fill)

    def merge_duplicates_by_domain(self) -> list[tuple[str, str]]:
        """Companies that resolved to the same website are one company ("Upwind" / "Upwind Security")."""
        merged: list[tuple[str, str]] = []
        seen: dict[str, dict] = {}
        for c in self.all_companies():
            if not c["domain"]:
                continue
            key = registrable_domain(c["domain"])
            if key in seen:
                keep = seen[key]
                self.merge_companies(keep["id"], c["id"])
                merged.append((c["name"], keep["name"]))
            else:
                seen[key] = c
        return merged

    def all_companies(self) -> list[dict]:
        return [_row(r) for r in self.conn.execute("SELECT * FROM companies ORDER BY id")]

    # -- funding --------------------------------------------------------------------
    def add_funding(self, company_id: int, *, round: str, amount_usd_m, amount_text, investors, lead_investors,
                    announced_at, source_name, source_url, headline) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO funding(company_id,round,amount_usd_m,amount_text,investors,lead_investors,announced_at,source_name,source_url,headline)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (company_id, round, amount_usd_m, amount_text, json.dumps(investors or []), json.dumps(lead_investors or []),
             announced_at, source_name, source_url, headline),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def funding_for(self, company_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM funding WHERE company_id=? ORDER BY announced_at DESC", (company_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["investors"] = json.loads(d["investors"] or "[]")
            d["lead_investors"] = json.loads(d["lead_investors"] or "[]")
            out.append(d)
        return out

    # -- jobs -----------------------------------------------------------------------
    def sync_jobs(self, company_id: int, jobs: list[dict], seen_at: str, first_fetch: bool = False,
                  close_after: int = 2, mass_close_after: int = 4) -> tuple[int, int, int]:
        """Upsert the current job list. Returns (new, updated, closed).

        first_fetch=True marks the jobs as the starting snapshot: they were open before we looked, so they are not
        reported as "new" (only roles that appear in later fetches are).

        A role that is missing from a fetch is not closed straight away: it must be missing `close_after` fetches in a
        row (a board can glitch for one request). When most of a company's roles vanish at once (an ATS migration or a
        parser that broke looks exactly like that) the bar is `mass_close_after` fetches, and that fetch is not added to
        the open-roles history, so one bad fetch never shows up as "the company stopped hiring"."""
        new = updated = 0
        rows = self.conn.execute("SELECT id, active, missed FROM jobs WHERE company_id=?", (company_id,)).fetchall()
        existing = {r["id"]: r for r in rows}
        listed = {j["id"] for j in jobs}
        was_open = [r["id"] for r in rows if r["active"]]
        still = sum(1 for i in was_open if i in listed)
        # "suspicious" = at least 5 roles were open and fewer than a quarter of them are still listed
        suspicious = len(was_open) >= 5 and still * 4 < len(was_open)
        cols = ["title", "department", "location", "remote", "workplace", "employment_type", "url", "apply_url", "posted_at", "description",
                "salary_text", "exp_min", "exp_max", "exp_source", "exp_mentions", "seniority", "role_family", "matches_profile"]
        for j in jobs:
            vals = [j.get(c) for c in cols]
            prev = existing.get(j["id"])
            if prev is not None:
                relisted = 0 if prev["active"] else 1       # closed earlier and listed again: the board re-opened it
                self.conn.execute(
                    f"UPDATE jobs SET {', '.join(c + '=?' for c in cols)}, last_seen=?, active=1, missed=0, closed_at=NULL, "
                    "reopened=reopened+? WHERE id=?",
                    (*vals, seen_at, relisted, j["id"]),
                )
                updated += 1
            else:
                self.conn.execute(
                    f"INSERT INTO jobs(id, company_id, {', '.join(cols)}, first_seen, last_seen, active, baseline) VALUES(?,?,{','.join('?' * len(cols))},?,?,1,?)",
                    (j["id"], company_id, *vals, seen_at, seen_at, int(first_fetch)),
                )
                new += 1
        closed = 0
        limit = mass_close_after if suspicious else close_after
        for jid in was_open:
            if jid in listed:
                continue
            missed = existing[jid]["missed"] + 1
            if missed >= limit:
                self.conn.execute("UPDATE jobs SET active=0, missed=?, closed_at=? WHERE id=?", (missed, seen_at, jid))
                closed += 1
            else:
                self.conn.execute("UPDATE jobs SET missed=? WHERE id=?", (missed, jid))
        if not suspicious:
            self.conn.execute("INSERT OR REPLACE INTO job_counts(company_id, day, at, open_count) VALUES(?,?,?,?)",
                              (company_id, seen_at[:10], seen_at, len(listed)))
        self.conn.commit()
        return new, updated, closed

    def open_counts(self, company_id: int, since_day: str = "") -> list[tuple[str, int]]:
        """(day, number of open roles) for one company, oldest first: one point per day, the day's last fetch."""
        q = self.conn.execute("SELECT day, open_count FROM job_counts WHERE company_id=? AND day>=? ORDER BY day", (company_id, since_day))
        return [(r[0], r[1]) for r in q]

    def closed_jobs(self, company_id: int, since: str = "") -> list[dict]:
        """Roles that are no longer listed, most recently closed first (closed_at >= since)."""
        q = self.conn.execute("SELECT * FROM jobs WHERE company_id=? AND active=0 AND COALESCE(closed_at, last_seen, '') >= ? "
                              "ORDER BY COALESCE(closed_at, last_seen) DESC, title", (company_id, since))
        return [dict(r) for r in q]

    def trim_history(self, before: str) -> int:
        """Drop the description of roles that closed before `before` (an ISO date). The row stays: title, dates and link are
        small and still useful, while the description is most of the size of the database. Returns the roles trimmed."""
        cur = self.conn.execute("UPDATE jobs SET description='', exp_mentions='[]' WHERE active=0 AND COALESCE(closed_at, last_seen, '') < ? "
                                "AND (description != '' OR exp_mentions != '[]')", (before,))
        self.conn.commit()
        return cur.rowcount

    def jobs_for(self, company_id: int, active_only: bool = True) -> list[dict]:
        q = "SELECT * FROM jobs WHERE company_id=?" + (" AND active=1" if active_only else "") + " ORDER BY first_seen DESC, title"
        return [dict(r) for r in self.conn.execute(q, (company_id,))]

    # -- LLM cache ------------------------------------------------------------------
    def cache_get(self, key: str) -> str | None:
        r = self.conn.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def cache_put(self, key: str, response: str, model: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO llm_cache(key,response,model,created_at) VALUES(?,?,?,?)", (key, response, model, now_iso()))
        self.conn.commit()

    # -- runs -----------------------------------------------------------------------
    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs(started_at) VALUES(?)", (now_iso(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, stats: dict) -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, stats=? WHERE id=?", (now_iso(), json.dumps(stats), run_id))
        self.conn.commit()

    def last_finished_run_started(self) -> str | None:
        """When the most recent run that completed (or was interrupted cleanly) started; a run that was killed does not count."""
        r = self.conn.execute("SELECT started_at FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return r[0] if r else None

    def last_run_started(self, before_id: int | None = None) -> str | None:
        q = "SELECT started_at FROM runs" + (" WHERE id<?" if before_id else "") + " ORDER BY id DESC LIMIT 1"
        r = self.conn.execute(q, (before_id,) if before_id else ()).fetchone()
        return r[0] if r else None
