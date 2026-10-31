"""Greenhouse Job Board API (public, no auth): boards-api.greenhouse.io"""
from __future__ import annotations

from ..http import Fetcher
from ..models import RawJob
from ..util import parse_dt, strip_html

NAME = "greenhouse"


def board_url(slug: str) -> str:
    return f"https://boards.greenhouse.io/{slug}"


def parse(data: dict) -> list[RawJob]:
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name") or ""
        pay = ""
        ranges = j.get("pay_input_ranges") or []
        if ranges:
            r = ranges[0]
            try:
                pay = f"{r.get('currency_type', '')} {int(r['min_cents']) // 100:,} - {int(r['max_cents']) // 100:,}".strip()
            except (KeyError, TypeError, ValueError):
                pay = ""
        jobs.append(RawJob(
            ext_id=str(j.get("id")),
            title=(j.get("title") or "").strip(),
            url=j.get("absolute_url") or "",
            apply_url=j.get("absolute_url") or "",
            location=loc,
            remote=True if "remote" in loc.lower() else None,
            department=", ".join(d.get("name", "") for d in (j.get("departments") or []) if d.get("name")),
            posted_at=parse_dt(j.get("first_published") or j.get("updated_at")),
            description=strip_html(j.get("content")),
            salary_text=pay,
        ))
    return jobs


def fetch(fetcher: Fetcher, slug: str, max_jobs: int = 300) -> list[RawJob] | None:
    """None = board does not exist / unreachable; [] = board exists but has no openings."""
    data = fetcher.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return parse(data)[:max_jobs]
