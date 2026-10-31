"""The pipeline stages. Network work runs in worker threads; all DB writes happen on the calling thread."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import careers as careers_mod
from .config import Config
from .db import DB
from .experience import Experience, extract_experience, find_salary, infer_workplace, level_to_years, role_family, seniority_of
from .extract import extract_funding
from .http import Fetcher
from .llm import LLMClient, LLMError, LLMQuotaExceeded
from .profile import Profile
from .resolve import resolve_website
from .sources import news as news_src
from .sources import yc as yc_src
from .util import days_since, domain_of, now_iso, registrable_domain

log = logging.getLogger(__name__)

MAX_ARTICLE_ATTEMPTS = 3     # after this many total LLM failures an article is skipped for good
MAX_SPLIT_DEPTH = 2          # a failed batch of 10 is retried as 5+5, then 2-3 each


class Pipeline:
    def __init__(self, cfg: Config, db: DB, fetcher: Fetcher, llm: LLMClient | None = None):
        self.cfg, self.db, self.fetcher, self.llm = cfg, db, fetcher, llm
        self.profile = Profile.from_config(cfg["profile"])
        self.workers = int(cfg["http"]["workers"])

    # ------------------------------------------------------------------ helpers
    def _parallel(self, fn: Callable, items: list):
        if not items:
            return []
        ex = ThreadPoolExecutor(max_workers=self.workers)
        try:
            return list(ex.map(fn, items))
        except BaseException:           # e.g. Ctrl-C: drop the queued work instead of waiting for all of it
            ex.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            ex.shutdown(wait=True)

    def in_scope(self, company: dict) -> bool:
        """Companies worth spending requests on: recent target-round funding, or YC, or added by hand."""
        origins = company.get("origins") or []
        if "manual" in origins or "yc" in origins:
            return True
        fcfg = self.cfg["funding"]
        lo, hi = fcfg.get("min_amount_usd_m") or 0, fcfg.get("max_amount_usd_m") or 0
        for f in self.db.funding_for(company["id"]):
            if f["round"] not in fcfg["target_rounds"]:
                continue
            d = days_since(f["announced_at"])
            if d is not None and d > self.cfg["general"]["recent_days"]:
                continue
            amt = f["amount_usd_m"]
            if amt is not None and ((lo and amt < lo) or (hi and amt > hi)):
                continue
            return True
        return False

    def scoped(self) -> list[dict]:
        return [c for c in self.db.all_companies() if self.in_scope(c)]

    # ------------------------------------------------------------------ stage 1: news
    def discover_news(self) -> dict:
        stats = {"articles": 0, "new": 0, "candidates": 0, "llm_calls": 0, "funding_items": 0, "new_companies": 0, "given_up": 0}
        ncfg, fcfg = self.cfg["news"], self.cfg["funding"]
        urls = news_src.build_feed_urls(fcfg["target_rounds"], ncfg["editions"], int(ncfg["lookback_days"]), ncfg["extra_feeds"])
        arts = news_src.collect_articles(self.fetcher, urls)
        stats["articles"] = len(arts)
        known = self.db.known_article_ids(a.id for a in arts)
        new = [a for a in arts if a.id not in known]
        stats["new"] = len(new)
        cands = news_src.prefilter(new, fcfg["target_rounds"], ncfg["require_round_keyword"])
        cand_ids = {a.id for a in cands}
        for a in new:
            if a.id not in cand_ids:
                self.db.mark_article(a, None)       # irrelevant: never look at it again
        # articles the LLM has repeatedly failed on are dropped instead of burning calls on them every run
        tries = self.db.article_attempts(a.id for a in cands)
        for a in [a for a in cands if tries.get(a.id, 0) >= MAX_ARTICLE_ATTEMPTS]:
            self.db.mark_article(a, None)
            stats["given_up"] += 1
        cands = [a for a in cands if tries.get(a.id, 0) < MAX_ARTICLE_ATTEMPTS]
        stats["candidates"] = len(cands)
        if not cands:
            return stats
        if self.llm is None:
            log.warning("OPENROUTER_API_KEY not set: %d funding articles waiting. Add it to .env and re-run.", len(cands))
            return stats

        self._fail_streak = 0
        size = int(self.cfg["llm"]["batch_size"])
        batches = -(-len(cands) // size)
        log.info("%d headlines to read in %d LLM batches (free models take 1-3 minutes per batch, so this can take a while)", len(cands), batches)
        for i in range(0, len(cands), size):
            if not self._extract_batch(cands[i : i + size], stats):
                break
        stats["llm_calls"] = self.llm.calls
        return stats

    def _extract_batch(self, batch: list, stats: dict, depth: int = 0) -> bool:
        """Run one LLM batch and store the results. A failed batch is split in two and retried, which isolates an
        article that makes the model misbehave. Returns False when the stage should stop (quota / LLM unavailable)."""
        fcfg = self.cfg["funding"]
        try:
            items, answered = extract_funding(self.llm, batch)
        except LLMQuotaExceeded as exc:
            log.warning("LLM stopped: %s. Remaining articles stay queued for the next run.", exc)
            return False
        except LLMError as exc:
            self._fail_streak += 1
            log.warning("LLM batch of %d failed (%s)", len(batch), str(exc)[:400])
            if self._fail_streak >= 6:
                log.warning("too many LLM failures in a row, giving up for this run")
                return False
            if len(batch) > 1 and depth < MAX_SPLIT_DEPTH:
                mid = (len(batch) + 1) // 2
                return self._extract_batch(batch[:mid], stats, depth + 1) and self._extract_batch(batch[mid:], stats, depth + 1)
            for art in batch:               # smallest piece still failing: count the failure against each article
                self.db.bump_attempts(art)
            return True
        self._fail_streak = 0
        funded = {id(it.article) for it in items}
        for idx, art in enumerate(batch):
            if idx in answered:                 # unanswered ones (reply cut off) stay queued for a later run
                self.db.mark_article(art, id(art) in funded)
        for it in items:
            if it.round not in fcfg["target_rounds"]:
                continue
            extra = {"website_hint": it.website} if it.website else None
            cid, created = self.db.upsert_company(it.company, origin="news", extra=extra,
                                                  description=it.description, country=it.country, city=it.city)
            stats["new_companies"] += int(created)
            if self.db.add_funding(
                cid, round=it.round, amount_usd_m=it.amount_usd_m, amount_text=it.amount_text,
                investors=it.investors, lead_investors=it.lead_investors, announced_at=it.article.published_at,
                source_name=it.article.publisher, source_url=it.article.url, headline=it.article.title,
            ):
                stats["funding_items"] += 1
        return True

    def merge_duplicates(self) -> int:
        merged = self.db.merge_duplicates_by_domain()
        for dropped, kept in merged:
            log.info("merged duplicate company %r into %r (same website)", dropped, kept)
        return len(merged)

    # ------------------------------------------------------------------ stage 2: YC
    def discover_yc(self) -> dict:
        if not self.cfg["yc"].get("enabled", True):
            return {"yc_companies": 0}
        rows = yc_src.fetch_yc(self.fetcher, self.cfg["yc"])
        added = 0
        for r in rows:
            cid, created = self.db.upsert_company(
                r["name"], origin="yc", extra=r["extra"], website=r["website"], website_confidence="yc",
                domain=domain_of(r["website"]), description=r["description"], country=r["country"], city=r["city"])
            added += int(created)
        return {"yc_companies": len(rows), "yc_new": added, "yc_merged": self.merge_duplicates()}

    # ------------------------------------------------------------------ stage 3: websites
    def resolve_websites(self, limit: int = 0, force: bool = False, recheck: bool = False) -> dict:
        """Find websites for companies that have none. With recheck=True, also re-grade websites that were auto-matched
        earlier (never YC / manual ones): a match that no longer holds up is downgraded to "unsure", a different one replaces it."""
        todo = []
        for c in self.scoped():
            auto = bool(c["website"]) and c["website_confidence"] in ("verified", "unsure")
            if c["website"] and not (recheck and auto):
                continue
            d = days_since(c["resolve_checked_at"])
            if force or recheck or d is None or d > 7:
                todo.append(c)
        if limit:
            todo = todo[:limit]

        def work(c):
            hint = None if c["website"] else (c.get("extra") or {}).get("website_hint")
            try:
                return c, resolve_website(self.fetcher, c["name"], country=c.get("country"), hint=hint, description=c.get("description"))
            except Exception as exc:   # one bad company must not kill the stage
                log.warning("resolve failed for %s: %s", c["name"], exc)
                return c, None

        ok = changed = downgraded = 0
        for c, res in self._parallel(work, todo):
            if res:
                same = bool(c["domain"]) and registrable_domain(res.domain) == registrable_domain(c["domain"])
                fields = dict(website_confidence=res.confidence, resolve_checked_at=now_iso())
                if not same:
                    fields.update(website=res.url, domain=res.domain)
                    if c["website"]:    # a different site than before: the old careers/ATS/jobs belong to the wrong company
                        fields.update(careers_url=None, ats=None, ats_slug=None, ats_confidence=None, careers_checked_at=None, jobs_status=None)
                        changed += 1
                self.db.update_company(c["id"], **fields)
                ok += 1
                log.info("website  %-28s -> %s (%s)", c["name"], res.url, res.confidence)
            elif c["website"]:
                self.db.update_company(c["id"], website_confidence="unsure", resolve_checked_at=now_iso())
                downgraded += 1
                log.info("website  %-28s -> %s can no longer be confirmed: marked unverified", c["name"], c["website"])
            else:
                self.db.update_company(c["id"], resolve_checked_at=now_iso())
                log.info("website  %-28s -> not found", c["name"])
        return {"websites_tried": len(todo), "websites_found": ok, "websites_replaced": changed, "websites_downgraded": downgraded,
                "merged_duplicates": self.merge_duplicates()}

    # ------------------------------------------------------------------ stage 4: careers / ATS
    def discover_careers(self, limit: int = 0, force: bool = False) -> dict:
        redo = float(self.cfg["jobs"]["rediscover_days"])
        todo = []
        for c in self.scoped():
            if not c["website"]:
                continue
            d = days_since(c["careers_checked_at"])
            if force or d is None or (d > redo and not (c["ats"] and c["jobs_status"] in ("ok", "empty"))):
                todo.append(c)
        if limit:
            todo = todo[:limit]

        def work(c):
            try:
                return c, careers_mod.discover(self.fetcher, c["website"], c["name"])
            except Exception as exc:
                log.warning("careers discovery failed for %s: %s", c["name"], exc)
                return c, None

        found = 0
        for c, info in self._parallel(work, todo):
            if info is None:
                continue
            checked = now_iso()
            if info.retry_soon:   # pretend it was checked (rediscover_days - 1) days ago, so it is retried tomorrow
                checked = (datetime.now(timezone.utc) - timedelta(days=max(float(self.cfg["jobs"]["rediscover_days"]) - 1, 0))).replace(microsecond=0).isoformat()
            self.db.update_company(c["id"], careers_url=info.careers_url, ats=info.ats, ats_slug=info.ats_slug,
                                   ats_confidence=info.ats_confidence, careers_checked_at=checked,
                                   jobs_status=None if info.supported else (info.notes or "no job feed"))
            found += int(info.supported)
            log.info("careers  %-28s -> %s %s", c["name"], f"{info.ats}/{info.ats_slug} ({info.ats_confidence})" if info.ats else "-", info.careers_url or info.notes)
        return {"careers_tried": len(todo), "ats_supported": found}

    # ------------------------------------------------------------------ stage 5: jobs
    def enrich_job(self, raw, ats: str, slug: str) -> dict:
        exp = extract_experience(raw.description, raw.title)
        if exp.source is None and raw.experience_hint:
            lv = level_to_years(raw.experience_hint)
            if lv:
                exp = Experience(min=lv[0], max=lv[1], source="level", mentions=[f"ATS level: {raw.experience_hint}"])
        workplace = infer_workplace(raw.location, raw.title, raw.workplace, raw.remote)
        job = {
            "id": f"{ats}:{slug}:{raw.ext_id}",
            "title": raw.title, "department": raw.department, "location": raw.location,
            "workplace": workplace,
            "remote": 1 if workplace == "remote" else (0 if workplace in ("hybrid", "onsite") else None),
            "employment_type": raw.employment_type, "url": raw.url, "apply_url": raw.apply_url or raw.url,
            "posted_at": raw.posted_at, "description": raw.description,
            "salary_text": raw.salary_text or find_salary(raw.description),
            "exp_min": exp.min, "exp_max": exp.max, "exp_source": exp.source,
            "exp_mentions": json.dumps(exp.mentions),
            "seniority": seniority_of(raw.title, exp), "role_family": role_family(raw.title, raw.department),
        }
        job["matches_profile"] = int(self.profile.matches(job))
        return job

    def fetch_jobs(self, limit: int = 0) -> dict:
        todo = [c for c in self.scoped() if c["ats"] and c["ats_slug"] and (c["ats"] in careers_mod.ats_mod.ADAPTERS or c["ats"] == "jsonld")]
        if limit:
            todo = todo[:limit]
        cap = int(self.cfg["jobs"]["max_jobs_per_company"])

        def work(c):
            try:
                return c, careers_mod.fetch_jobs(self.fetcher, c["ats"], c["ats_slug"], cap)
            except Exception as exc:
                log.warning("job fetch failed for %s: %s", c["name"], exc)
                return c, None

        totals = {"companies_with_jobs": 0, "jobs_active": 0, "jobs_new": 0, "jobs_closed": 0, "boards_unreachable": 0}
        seen_at = now_iso()
        for c, raws in self._parallel(work, todo):
            if raws is None:   # transient failure or board removed: keep what we know, do NOT close jobs
                self.db.update_company(c["id"], jobs_status="unreachable")
                totals["boards_unreachable"] += 1
                continue
            jobs = [self.enrich_job(r, c["ats"], c["ats_slug"]) for r in raws if r.title]
            new, _upd, closed = self.db.sync_jobs(c["id"], jobs, seen_at, first_fetch=c["jobs_fetched_at"] is None)
            self.db.update_company(c["id"], jobs_fetched_at=seen_at, jobs_status="ok" if jobs else "empty")
            totals["companies_with_jobs"] += int(bool(jobs))
            totals["jobs_active"] += len(jobs)
            totals["jobs_new"] += new
            totals["jobs_closed"] += closed
            log.info("jobs     %-28s %3d open (%d new, %d closed)", c["name"], len(jobs), new, closed)
        return totals

    # ------------------------------------------------------------------ everything
    def run_all(self, *, news: bool = True, yc: bool = True, limit: int = 0) -> dict:
        stats: dict = {}
        if not self.db.all_companies():
            log.info("First run on an empty database: expect roughly 30 minutes (free LLM models are slow and every company "
                     "site is checked once). Later runs only look at what is new and take a few minutes. "
                     "Safe to interrupt with Ctrl-C: the next run continues where this one stopped.")
        if news:
            log.info("[1/5] reading funding news")
            stats.update(self.discover_news())
        if yc:
            log.info("[2/5] Y Combinator list")
            stats.update(self.discover_yc())
        log.info("[3/5] finding company websites")
        stats.update(self.resolve_websites(limit))
        log.info("[4/5] finding careers pages / job boards")
        stats.update(self.discover_careers(limit))
        log.info("[5/5] fetching open roles")
        stats.update(self.fetch_jobs(limit))
        return stats
