"""Ashby public job board API: api.ashbyhq.com/posting-api/job-board/{board}"""
from __future__ import annotations

from ..http import Fetcher
from ..models import RawJob
from ..util import parse_dt, strip_html

NAME = "ashby"


def board_url(slug: str) -> str:
    return f"https://jobs.ashbyhq.com/{slug}"


def parse(data: dict) -> list[RawJob]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in (j.get("secondaryLocations") or [])]
        loc = "; ".join(dict.fromkeys(l for l in locs if l))
        comp = j.get("compensation") or {}
        wt = (j.get("workplaceType") or "").lower()
        jobs.append(RawJob(
            ext_id=str(j.get("id")),
            title=(j.get("title") or "").strip(),
            url=j.get("jobUrl") or "",
            apply_url=j.get("applyUrl") or j.get("jobUrl") or "",
            location=loc,
            remote=True if (j.get("isRemote") or wt == "remote") else (False if wt else None),
            workplace={"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}.get(wt, "remote" if j.get("isRemote") else ""),
            department=j.get("department") or j.get("team") or "",
            employment_type=j.get("employmentType") or "",
            posted_at=parse_dt(j.get("publishedAt")),
            description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
            salary_text=comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary") or "",
        ))
    return jobs


def fetch(fetcher: Fetcher, slug: str, max_jobs: int = 300) -> list[RawJob] | None:
    data = fetcher.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return parse(data)[:max_jobs]
