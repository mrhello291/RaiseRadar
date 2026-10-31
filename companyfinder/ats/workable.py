"""Workable public widget API: apply.workable.com/api/v1/widget/accounts/{slug}?details=true"""
from __future__ import annotations

from ..http import Fetcher
from ..models import RawJob
from ..util import parse_dt, strip_html

NAME = "workable"


def board_url(slug: str) -> str:
    return f"https://apply.workable.com/{slug}/"


def parse(data: dict, slug: str) -> list[RawJob]:
    jobs = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        jobs.append(RawJob(
            ext_id=str(j.get("shortcode") or j.get("code") or j.get("title")),
            title=(j.get("title") or "").strip(),
            url=j.get("url") or j.get("shortlink") or board_url(slug),
            apply_url=j.get("application_url") or j.get("url") or "",
            location=loc,
            remote=True if j.get("telecommuting") else None,
            workplace="remote" if j.get("telecommuting") else "",
            department=j.get("department") or "",
            employment_type=j.get("employment_type") or "",
            posted_at=parse_dt(j.get("published_on") or j.get("created_at")),
            description=strip_html(j.get("description")),
            experience_hint=j.get("experience") or "",
        ))
    return jobs


def fetch(fetcher: Fetcher, slug: str, max_jobs: int = 300) -> list[RawJob] | None:
    data = fetcher.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return parse(data, slug)[:max_jobs]
