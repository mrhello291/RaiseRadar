"""The outreach bot: find roles that fit your portfolio, draft a note, ask you on your phone, send what you approve.

Hard rules (all tested):
  * Nothing is sent without an approval for that exact text and recipient (a signed phone tap or a local command).
  * At most one email per company, ever; companies you applied to are left alone; blocked domains/addresses never get mail.
  * Recipients are only role addresses published on the company's own site, or ones you added yourself.
  * Only companies whose website was verified (or added by hand) are contacted, so a namesake never gets your email.
  * A daily cap and a gap between sends. No automatic follow-ups.
  * Never twice: with Gmail on, a copy of each draft goes to your Gmail Drafts (edit it there and send it yourself),
    and before sending the bot checks Sent. If you already emailed that address, or edited the draft, it does not send.

State lives in its own file (`data/outreach.db`), apart from the job cache, like the application tracker.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from . import mailer, notify
from .contacts import find_contacts
from .drafting import FOOTER, draft_email, linkedin_note
from .matching import score_job
from .me import Me
from .util import days_since, now_iso, registrable_domain

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key TEXT, company TEXT NOT NULL, domain TEXT NOT NULL UNIQUE, role_title TEXT NOT NULL, job_url TEXT,
  to_email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, score REAL, evidence TEXT, how TEXT,
  status TEXT NOT NULL, created_at TEXT NOT NULL, notified_at TEXT, decided_at TEXT, decided_via TEXT,
  sent_at TEXT, message_id TEXT, error TEXT, gmail_marker TEXT, gmail_cleaned_at TEXT
);
CREATE TABLE IF NOT EXISTS blocked(pattern TEXT PRIMARY KEY, reason TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS contacts(domain TEXT NOT NULL, email TEXT NOT NULL, source_url TEXT, rank INTEGER, found_at TEXT,
  manual INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(domain, email));
CREATE TABLE IF NOT EXISTS contact_checks(domain TEXT PRIMARY KEY, checked_at TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL UNIQUE, company TEXT NOT NULL, role_title TEXT NOT NULL,
  job_url TEXT, apply_url TEXT, score REAL, project TEXT, linkedin_note TEXT, message TEXT,
  status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL, notified_at TEXT
);
"""
STATUSES = ("pending", "approved", "sent", "sent_by_you", "in_gmail", "skipped", "expired", "failed")
# statuses where you may still send the draft yourself from Gmail, so Sent is checked for them
OPEN = ("pending", "approved", "in_gmail")
ADDED_COLUMNS = {"drafts": ("gmail_marker TEXT", "gmail_cleaned_at TEXT")}
REQ_HEAD = re.compile(r"(?im)^[\s#*-]*(requirements|qualifications|minimum qualifications|who you are|about you|you have|you(?:'ll| will) (?:have|bring)|"
                      r"what you(?:'ll)? bring|what we(?:'re| are) looking for|must[- ]haves?|skills|experience)\b[^\n]{0,40}$")


def jd_excerpt(description: str, chars: int = 900) -> str:
    """The part of a job post that says what it asks for (from a 'Requirements' style heading), else its start."""
    text = (description or "").strip()
    m = REQ_HEAD.search(text)
    part = text[m.start():] if m else text
    part = re.sub(r"\n{2,}", "\n", part).strip()
    return part if len(part) <= chars else part[:chars].rsplit(" ", 1)[0] + " ..."
TRUSTED_SITES = ("verified", "yc", "manual")


class OutreachError(ValueError):
    pass


def _norm_block(pattern: str) -> str:
    p = pattern.strip().lower()
    if "@" in p:
        return p
    return registrable_domain(re.sub(r"^https?://", "", p).split("/")[0].removeprefix("www."))


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _c(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA)
            for table, cols in ADDED_COLUMNS.items():     # databases made by an older version
                have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                for col in cols:
                    if col.split()[0] not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- blocking -------------------------------------------------------------------
    def block(self, pattern: str, reason: str = "") -> str:
        p = _norm_block(pattern)
        if not p:
            raise OutreachError("nothing to block")
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO blocked(pattern, reason, at) VALUES(?,?,?)", (p, reason, now_iso()))
            c.execute("UPDATE drafts SET status='skipped', decided_at=?, decided_via='blocked' WHERE status IN ('pending','approved') "
                      "AND (domain=? OR lower(to_email)=?)", (now_iso(), p, p))
        return p

    def is_blocked(self, email: str, domain: str) -> bool:
        with self._c() as c:
            return c.execute("SELECT 1 FROM blocked WHERE pattern IN (?,?,?)", (email.lower(), registrable_domain(domain), registrable_domain(email.split("@")[-1]))).fetchone() is not None

    def blocked(self) -> list[dict]:
        with self._c() as c:
            return [dict(r) for r in c.execute("SELECT * FROM blocked ORDER BY at DESC")]

    # -- contacts -------------------------------------------------------------------
    def add_contact(self, domain: str, email: str) -> None:
        email = email.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise OutreachError(f"{email!r} is not an email address")
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO contacts(domain,email,source_url,rank,found_at,manual) VALUES(?,?,?,?,?,1)",
                      (registrable_domain(domain), email, "added by you", -1, now_iso()))

    def contacts_for(self, domain: str) -> list[dict]:
        with self._c() as c:
            return [dict(r) for r in c.execute("SELECT * FROM contacts WHERE domain=? ORDER BY rank, email", (registrable_domain(domain),))]

    def save_contacts(self, domain: str, found: list[dict]) -> None:
        d = registrable_domain(domain)
        with self._c() as c:
            for f in found:
                c.execute("INSERT OR IGNORE INTO contacts(domain,email,source_url,rank,found_at,manual) VALUES(?,?,?,?,?,0)",
                          (d, f["email"], f["source_url"], f["rank"], now_iso()))
            c.execute("INSERT OR REPLACE INTO contact_checks(domain, checked_at) VALUES(?,?)", (d, now_iso()))

    def contact_checked_days(self, domain: str) -> float | None:
        with self._c() as c:
            r = c.execute("SELECT checked_at FROM contact_checks WHERE domain=?", (registrable_domain(domain),)).fetchone()
        return days_since(r["checked_at"]) if r else None

    # -- drafts ---------------------------------------------------------------------
    def has_domain(self, domain: str) -> bool:
        with self._c() as c:
            return c.execute("SELECT 1 FROM drafts WHERE domain=?", (registrable_domain(domain),)).fetchone() is not None

    def add_draft(self, **f) -> int:
        with self._c() as c:
            cur = c.execute(
                "INSERT INTO drafts(job_key,company,domain,role_title,job_url,to_email,subject,body,score,evidence,how,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                (f["job_key"], f["company"], registrable_domain(f["domain"]), f["role_title"], f.get("job_url", ""), f["to_email"].lower(),
                 f["subject"], f["body"], f["score"], json.dumps(f.get("evidence", {})), f.get("how", ""), now_iso()))
            return cur.lastrowid

    def get(self, draft_id: int) -> dict | None:
        with self._c() as c:
            r = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(r) if r else None

    def list(self, status: str | None = None) -> list[dict]:
        with self._c() as c:
            q = "SELECT * FROM drafts" + (" WHERE status=?" if status else "") + " ORDER BY id DESC"
            return [dict(r) for r in c.execute(q, (status,) if status else ())]

    def update(self, draft_id: int, **fields) -> None:
        with self._c() as c:
            c.execute(f"UPDATE drafts SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), draft_id))

    def decide(self, draft_id: int, action: str, via: str) -> bool:
        """pending -> approved/skipped. Only a pending draft can be decided, so a repeated tap does nothing."""
        new = {"approve": "approved", "skip": "skipped"}[action]
        with self._c() as c:
            cur = c.execute("UPDATE drafts SET status=?, decided_at=?, decided_via=? WHERE id=? AND status='pending'", (new, now_iso(), via, draft_id))
            return cur.rowcount > 0

    def open_drafts(self) -> list[dict]:
        """Drafts you might still send yourself from Gmail: open ones, and expired ones with a Gmail copy from the last 30 days."""
        cut = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
        with self._c() as c:
            return [dict(r) for r in c.execute(
                f"SELECT * FROM drafts WHERE status IN ({','.join('?' * len(OPEN))}) OR (status='expired' AND gmail_marker IS NOT NULL "
                "AND created_at>=?) ORDER BY id", (*OPEN, cut))]

    def gmail_copies_to_remove(self) -> list[dict]:
        with self._c() as c:
            return [dict(r) for r in c.execute("SELECT * FROM drafts WHERE gmail_marker IS NOT NULL AND gmail_cleaned_at IS NULL "
                                               "AND status IN ('sent','sent_by_you','skipped','expired','failed')")]

    def sent_since(self, hours: float) -> list[dict]:
        cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
        with self._c() as c:
            return [dict(r) for r in c.execute("SELECT * FROM drafts WHERE status='sent' AND sent_at>=? ORDER BY sent_at", (cut,))]

    # -- leads: companies with no public email, to apply to directly -------------------
    def has_lead(self, domain: str) -> bool:
        with self._c() as c:
            return c.execute("SELECT 1 FROM leads WHERE domain=?", (registrable_domain(domain),)).fetchone() is not None

    def add_lead(self, **f) -> int:
        with self._c() as c:
            cur = c.execute(
                "INSERT INTO leads(domain,company,role_title,job_url,apply_url,score,project,linkedin_note,message,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (registrable_domain(f["domain"]), f["company"], f["role_title"], f.get("job_url", ""), f.get("apply_url", ""), f["score"],
                 f.get("project", ""), f.get("linkedin_note", ""), f.get("message", ""), now_iso()))
            return cur.lastrowid

    def leads(self, status: str | None = None) -> list[dict]:
        with self._c() as c:
            q = "SELECT * FROM leads" + (" WHERE status=?" if status else "") + " ORDER BY score DESC, id"
            return [dict(r) for r in c.execute(q, (status,) if status else ())]

    def get_lead(self, lead_id: int) -> dict | None:
        with self._c() as c:
            r = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return dict(r) if r else None

    def set_lead(self, lead_id: int, **fields) -> None:
        with self._c() as c:
            c.execute(f"UPDATE leads SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), lead_id))

    def close_lead(self, domain: str, status: str) -> None:
        with self._c() as c:
            c.execute("UPDATE leads SET status=? WHERE domain=? AND status='new'", (status, registrable_domain(domain)))

    def meta(self, key: str, default: str = "") -> str:
        with self._c() as c:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))


class Outreach:
    def __init__(self, cfg, me: Me, store: Store, jobs_db, fetcher=None, llm=None, tracker=None, env=None, sleep=time.sleep,
                 smtp_factory=None, http_client=None, imap_factory=None):
        import os
        self.o = cfg["outreach"]
        self.me, self.store, self.db, self.fetcher, self.llm, self.tracker = me, store, jobs_db, fetcher, llm, tracker
        self.env = env if env is not None else os.environ
        self.sleep, self.smtp_factory, self.http, self.imap_factory = sleep, smtp_factory, http_client, imap_factory
        self._lookups = 0
        self.last_leads: list[dict] = []

    # -- settings from the environment ------------------------------------------------
    @property
    def topic(self) -> str: return self.env.get("OUTREACH_NTFY_TOPIC", "").strip()
    @property
    def reply_topic(self) -> str: return self.env.get("OUTREACH_NTFY_REPLY_TOPIC", "").strip()
    @property
    def secret(self) -> str: return self.env.get("OUTREACH_APPROVAL_SECRET", "").strip()

    def phone_ready(self) -> bool:
        return bool(self.topic and self.reply_topic and len(self.secret) >= 16)

    @property
    def smtp_user(self) -> str: return self.env.get("OUTREACH_SMTP_USER", "").strip()

    def gmail_on(self) -> bool:
        """Drafts copied to Gmail and Sent checked: needs `gmail_drafts` and the mail login."""
        return bool(self.o.get("gmail_drafts") and self.smtp_user and self.env.get("OUTREACH_SMTP_PASSWORD", ""))

    def gmail_edit_url(self) -> str:
        from urllib.parse import quote
        return f"https://mail.google.com/mail/?authuser={quote(self.smtp_user)}#drafts"

    @contextmanager
    def mailbox(self):
        """The Gmail Drafts/Sent connection, or None when Gmail is off or cannot be reached (the reason is logged)."""
        from .gmailbox import Gmailbox, MailboxError
        if not self.gmail_on():
            yield None
            return
        try:
            box = Gmailbox(self.o["imap_host"], int(self.o["imap_port"]), self.smtp_user,
                           self.env.get("OUTREACH_SMTP_PASSWORD", ""), imap_factory=self.imap_factory).__enter__()
        except MailboxError as exc:
            log.warning("Gmail: %s", exc)
            yield None
            return
        try:
            yield box
        finally:
            box.__exit__(None, None, None)

    def _message(self, d: dict):
        return mailer.build_message(self.me.name, self.smtp_user or self.me.email, self.me.email, d["to_email"], d["subject"], d["body"])

    def facts(self, d: dict) -> dict:
        """What the notification shows to check the email against: matched skills, project, years asked, the job post's words."""
        ev = json.loads(d.get("evidence") or "{}") if isinstance(d.get("evidence"), str) else dict(d.get("evidence") or {})
        if "jd" not in ev and d.get("job_key") and self.db is not None:     # drafts made before these were stored
            r = self.db.conn.execute("SELECT description, exp_min FROM jobs WHERE id=?", (d["job_key"],)).fetchone()
            if r:
                ev.setdefault("exp_min", r["exp_min"])
                ev["jd"] = jd_excerpt(r["description"])
        ev.setdefault("years", self.me.years_experience or None)
        return ev

    # -- 1. find and draft --------------------------------------------------------------
    def _applied_companies(self) -> set[str]:
        if not self.tracker:
            return set()
        from .util import norm_name
        return {norm_name(a["company"]) for a in self.tracker.list() if a["status"] in ("applied", "interviewing", "offer", "rejected")}

    def _funding(self, company_id: int) -> str:
        r = self.db.conn.execute("SELECT round, amount_text, announced_at FROM funding WHERE company_id=? ORDER BY announced_at DESC LIMIT 1", (company_id,)).fetchone()
        if not r or not r["round"]:
            return ""
        age = days_since(r["announced_at"])
        if age is None or age > float(self.o["max_funding_age_days"]):
            return ""
        return f"{r['round']}" + (f" ({r['amount_text']})" if r["amount_text"] else "")

    def candidates(self) -> list[dict]:
        """Best role per company, best fit first, before any contact lookup. Skips untrusted sites, applied and blocked companies."""
        from .util import norm_name
        applied = self._applied_companies()
        rows = self.db.conn.execute(
            "SELECT j.id AS job_key, j.title, j.location, j.url, j.apply_url, j.description, j.exp_min, c.id AS cid, c.name, c.website, c.domain "
            "FROM jobs j JOIN companies c ON c.id=j.company_id "
            f"WHERE j.active=1 AND c.domain IS NOT NULL AND c.domain!='' AND c.website_confidence IN ({','.join('?' * len(TRUSTED_SITES))})",
            TRUSTED_SITES).fetchall()
        best: dict[str, dict] = {}
        for r in rows:
            if norm_name(r["name"]) in applied or self.store.has_domain(r["domain"]):
                continue
            m = score_job(self.me, r["title"], r["description"], r["exp_min"])
            if m is None or m.score < float(self.o["min_match_score"]) or m.project is None:
                continue
            cur = best.get(r["domain"])
            if cur is None or m.score > cur["match"].score:
                best[r["domain"]] = {"row": dict(r), "match": m}
        return sorted(best.values(), key=lambda x: -x["match"].score)

    def _needs_lookup(self, row: dict) -> bool:
        """True if finding this company's contact address would cost website requests (nothing known, not checked lately)."""
        if self.fetcher is None or self.store.contacts_for(row["domain"]):
            return False
        checked = self.store.contact_checked_days(row["domain"])
        return checked is None or checked > 14

    def _recipient(self, row: dict) -> dict | None:
        known = self.store.contacts_for(row["domain"])
        if not known and self._needs_lookup(row):
            self._lookups += 1
            self.store.save_contacts(row["domain"], find_contacts(self.fetcher, row["website"], row["domain"]))
            known = self.store.contacts_for(row["domain"])
        for k in known:
            if not self.store.is_blocked(k["email"], row["domain"]):
                return k
        return None

    def plan(self, limit: int | None = None, dry_run: bool = False) -> list[dict]:
        """Create drafts for the best new candidates. With dry_run nothing is stored. Returns the drafts."""
        limit = limit if limit is not None else int(self.o["max_drafts_per_run"])
        if (miss := self.me.missing()):
            raise OutreachError("fill in your profile first (data/me.toml): " + ", ".join(miss))
        pending = len(self.store.list("pending"))
        room = max(0, int(self.o["max_pending"]) - pending)
        out: list[dict] = []
        self._lookups, self.last_leads = 0, []
        max_lookups, max_leads = int(self.o["max_lookups_per_run"]), int(self.o["max_leads_per_run"])
        for cand in self.candidates():
            if len(out) >= min(limit, room if not dry_run else limit):
                break
            row, m = cand["row"], cand["match"]
            if self.store.is_blocked("", row["domain"]):
                continue
            if self._lookups >= max_lookups and self._needs_lookup(row):
                log.info("stopping after %d website lookups; the rest continue next run (results are remembered)", self._lookups)
                break
            rcpt = self._recipient(row)
            if not rcpt:
                if not self.store.has_lead(row["domain"]) and len(self.last_leads) < max_leads:
                    lead = self._make_lead(row, m)
                    if not dry_run:
                        lead["id"] = self.store.add_lead(**lead)
                    self.last_leads.append(lead)
                    log.info("no public email for %s: kept as an apply-directly lead", row["name"])
                continue
            company = {"name": row["name"], "domain": row["domain"]}
            job = {"title": row["title"], "location": row["location"], "description": row["description"]}
            try:
                subject, body, how = draft_email(self.llm, self.me, company, job, m, self._funding(row["cid"]),
                                                 resume=self.me.resume_for(row["title"]))
            except ValueError as exc:
                log.warning("%s: %s", row["name"], exc)
                continue
            d = {"job_key": row["job_key"], "company": row["name"], "domain": row["domain"], "role_title": row["title"], "job_url": row["url"] or "",
                 "to_email": rcpt["email"], "subject": subject, "body": body, "score": m.score, "how": how,
                 "evidence": {"skills": m.skills, "project": m.project.name, "project_hits": m.project_hits, "contact_source": rcpt["source_url"],
                              "exp_min": row["exp_min"], "jd": jd_excerpt(row["description"])}}
            if not dry_run:
                d["id"] = self.store.add_draft(**d)
                self.store.close_lead(row["domain"], "emailed")   # an address turned up (or you added one): the email replaces the lead
            out.append(d)
        return out

    def _make_lead(self, row: dict, m) -> dict:
        """A company with no public email: the apply link, a LinkedIn note (300 characters) and a message to paste into an application."""
        company = {"name": row["name"], "domain": row["domain"]}
        job = {"title": row["title"], "location": row["location"], "description": row["description"]}
        try:
            _subject, body, _how = draft_email(self.llm, self.me, company, job, m, self._funding(row["cid"]), resume=self.me.resume_for(row["title"]))
            message = body.removesuffix(FOOTER)      # the "reply no" footer is for cold email, not for applications
        except ValueError:
            message = ""
        return {"company": row["name"], "domain": row["domain"], "role_title": row["title"], "job_url": row["url"] or "",
                "apply_url": row.get("apply_url") or row["url"] or "", "score": m.score, "project": m.project.name,
                "linkedin_note": linkedin_note(self.me, row["name"], row["title"], m.project), "message": message}

    def notify_leads(self) -> int:
        """One phone notification listing new apply-directly leads (company, role, link). The texts stay on this computer."""
        if not self.topic:
            return 0
        new = [l for l in self.store.leads("new") if not l["notified_at"]][:10]
        if not new:
            return 0
        lines = [f"- #{l['id']} {l['company']}: {l['role_title']}\n  {l['apply_url'] or l['job_url']}" for l in new]
        msg = "No public email found for these, so apply directly or message on LinkedIn:\n\n" + "\n".join(lines) + \
              "\n\nTexts to paste: raiseradar outreach leads --show ID"
        if notify.publish_text(self.o["ntfy_server"], self.topic, f"{len(new)} roles to apply to directly", msg, client=self.http):
            for l in new:
                self.store.set_lead(l["id"], notified_at=now_iso())
            return len(new)
        return 0

    # -- 2. ask ---------------------------------------------------------------------------
    def notify_pending(self) -> int:
        """Send the approval request for every pending draft that has not been announced. Returns how many went out."""
        if not self.phone_ready():
            return 0
        todo = [d for d in reversed(self.store.list("pending")) if not d["notified_at"]]
        if not todo:
            return 0
        n = 0
        with self.mailbox() as box:
            for d in todo:
                if box is not None and not d["gmail_marker"]:
                    d["gmail_marker"] = self._save_gmail_copy(box, d)
                edit = self.gmail_edit_url() if d["gmail_marker"] else None
                if notify.publish(self.o["ntfy_server"], self.topic, self.reply_topic, self.secret, d, client=self.http,
                                  facts=self.facts(d), edit_url=edit):
                    self.store.update(d["id"], notified_at=now_iso())
                    n += 1
        return n

    def _save_gmail_copy(self, box, d: dict) -> str | None:
        from .gmailbox import MailboxError, marker_value
        marker = marker_value(d["id"], notify.content_hash(d["to_email"], d["subject"], d["body"]))
        try:
            box.save_draft(self._message(d), marker)
        except (MailboxError, mailer.MailError, OSError) as exc:
            log.warning("could not put draft #%s in Gmail Drafts: %s", d["id"], exc)
            return None
        self.store.update(d["id"], gmail_marker=marker)
        return marker

    # -- 3b. what you did in Gmail ----------------------------------------------------------
    def sync_gmail(self) -> dict:
        """Drafts you sent yourself from Gmail become 'sent_by_you' (and go in the tracker); the bot's copies of
        skipped, sent or expired drafts are removed from Gmail Drafts if you did not change them."""
        from .gmailbox import MailboxError
        res = {"sent_by_you": 0, "cleaned": 0}
        if not self.gmail_on():
            return res
        with self.mailbox() as box:
            if box is None:
                return res
            for d in self.store.open_drafts():
                try:
                    if box.sent_to(d["to_email"], d["created_at"]):
                        self._mark_sent_by_you(d)
                        res["sent_by_you"] += 1
                except MailboxError as exc:
                    log.warning("Gmail Sent check for #%s: %s", d["id"], exc)
            for d in self.store.gmail_copies_to_remove():
                try:
                    res["cleaned"] += box.delete_draft(d["to_email"], d["gmail_marker"], d["body"])
                    self.store.update(d["id"], gmail_cleaned_at=now_iso())
                except MailboxError as exc:
                    log.warning("Gmail cleanup for #%s: %s", d["id"], exc)
        return res

    def _mark_sent_by_you(self, d: dict) -> None:
        self.store.update(d["id"], status="sent_by_you", sent_at=now_iso(), decided_at=d["decided_at"] or now_iso(),
                          decided_via=d["decided_via"] or "gmail")
        self._record_application(d, note=f"You emailed {d['to_email']} from Gmail (outreach #{d['id']})")

    # -- 3. collect taps ------------------------------------------------------------------
    def collect_decisions(self) -> dict:
        res = {"approved": 0, "skipped": 0, "ignored": 0}
        if not self.phone_ready():
            return res
        msgs, cursor = notify.poll(self.o["ntfy_server"], self.reply_topic, self.store.meta("ntfy_cursor", "all"), client=self.http)
        pend = {d["id"]: notify.content_hash(d["to_email"], d["subject"], d["body"]) for d in self.store.list("pending")}
        for m in msgs:
            dec = notify.parse_decision(m["message"], self.secret, pend)
            if dec is None:
                res["ignored"] += 1
                continue
            did, action = dec
            if self.store.decide(did, action, "phone"):
                res["approved" if action == "approve" else "skipped"] += 1
                pend.pop(did, None)
        if cursor != self.store.meta("ntfy_cursor", "all"):
            self.store.set_meta("ntfy_cursor", cursor)
        return res

    def expire(self) -> int:
        n, ttl = 0, float(self.o["approval_ttl_hours"]) / 24
        for status in ("pending", "approved", "in_gmail"):
            for d in self.store.list(status):
                age = days_since(d["created_at"] if status == "pending" else d["decided_at"])
                if age is not None and age > ttl:
                    self.store.update(d["id"], status="expired")
                    n += 1
        return n

    # -- 4. send --------------------------------------------------------------------------
    def send_approved(self, max_sends: int | None = None) -> dict:
        res = {"sent": 0, "failed": 0, "held": 0}
        if not self.o["enabled"]:
            res["held"] = len(self.store.list("approved"))
            return res
        cap = int(self.o["daily_cap"])
        todo = list(reversed(self.store.list("approved")))
        for d in todo:
            if max_sends is not None and res["sent"] >= max_sends:
                res["held"] += 1
                continue
            if len(self.store.sent_since(24)) >= cap:
                res["held"] += 1
                continue
            fresh = self.store.get(d["id"])
            if fresh["status"] != "approved" or self.store.is_blocked(fresh["to_email"], fresh["domain"]):
                continue
            verdict = self._gmail_verdict(fresh)
            if verdict == "hold":
                res["held"] += 1
                continue
            if verdict != "send":
                res[verdict] = res.get(verdict, 0) + 1
                continue
            if res["sent"]:
                self.sleep(float(self.o["min_gap_seconds"]) * random.uniform(1.0, 1.5))
            try:
                msg = self._message(fresh)
                mid = mailer.send(self.o["smtp_host"], int(self.o["smtp_port"]), self.env.get("OUTREACH_SMTP_USER", ""),
                                  self.env.get("OUTREACH_SMTP_PASSWORD", ""), msg, smtp_factory=self.smtp_factory)
            except mailer.MailError as exc:
                self.store.update(d["id"], status="failed", error=str(exc)[:300])
                res["failed"] += 1
                if "login" in str(exc).lower():     # no point trying the rest
                    break
                continue
            self.store.update(d["id"], status="sent", sent_at=now_iso(), message_id=mid)
            self._record_application(fresh)
            res["sent"] += 1
        if res["sent"] and self.gmail_on():
            self.sync_gmail()          # remove the bot's copies of what was just sent from Gmail Drafts
        return res

    def _gmail_verdict(self, d: dict) -> str:
        """Last look in Gmail before sending: 'send', 'hold' (Gmail unreachable while a copy is there), 'sent_by_you',
        'already_emailed' (you wrote to this address before this draft) or 'edited' (send your edit from Gmail)."""
        from .gmailbox import MailboxError
        if not self.gmail_on():
            return "send"
        with self.mailbox() as box:
            if box is None:
                return "hold" if d["gmail_marker"] else "send"
            try:
                if box.sent_to(d["to_email"], d["created_at"]):
                    self._mark_sent_by_you(d)
                    return "sent_by_you"
                if box.sent_to(d["to_email"]):
                    self.store.update(d["id"], status="skipped", decided_via="already emailed", error="you had emailed this address before")
                    return "already_emailed"
                if d["gmail_marker"] and box.draft_state(d["to_email"], d["body"]) == "edited":
                    self.store.update(d["id"], status="in_gmail", error="you edited it in Gmail: send it from there")
                    if self.topic:
                        notify.publish_text(self.o["ntfy_server"], self.topic, f"{d['company']}: send your edit from Gmail",
                                            f"You changed the draft to {d['to_email']} in Gmail, so the bot did not send its "
                                            "version. Send yours from Gmail; the bot will notice it in Sent.", client=self.http)
                    return "edited"
            except MailboxError as exc:
                log.warning("Gmail check before sending #%s: %s", d["id"], exc)
                return "hold" if d["gmail_marker"] else "send"
        return "send"

    def _record_application(self, d: dict, note: str | None = None) -> None:
        """Remember it in the tracker, so the report shows the follow-up reminder and the role is not pitched twice."""
        if not self.tracker:
            return
        try:
            self.tracker.add_manual(d["company"], d["role_title"], url=d["job_url"] or "", status="applied",
                                    note=note or f"Cold email sent to {d['to_email']} (outreach #{d['id']})")
        except Exception:
            log.warning("could not record outreach #%s in the tracker", d["id"])

    # -- everything ----------------------------------------------------------------------
    def run(self, dry_run: bool = False, limit: int | None = None) -> dict:
        if dry_run:
            return {"drafts": self.plan(limit, dry_run=True), "leads": self.last_leads}
        out = {"expired": self.expire()}
        out["collected"] = self.collect_decisions()      # first, so an approval is never lost to a later step
        out["gmail"] = self.sync_gmail()                 # what you sent or edited yourself in Gmail
        out["new_drafts"] = len(self.plan(limit))
        out["new_leads"] = len(self.last_leads)
        out["notified"] = self.notify_pending()
        out["leads_notified"] = self.notify_leads()
        out["send"] = self.send_approved()
        return out
