"""SmartRecruiters public Posting API: api.smartrecruiters.com/v1/companies/{id}/postings"""
from __future__ import annotations

from ..http import Fetcher
from ..models import RawJob
from ..util import parse_dt, strip_html

NAME = "smartrecruiters"
DETAIL_LIMIT = 40   # one extra request per job for the full JD, so only the first N get one


def board_url(slug: str) -> str:
    return f"https://careers.smartrecruiters.com/{slug}"


def parse_listing(item: dict, slug: str) -> RawJob:
    loc = item.get("location") or {}
    return RawJob(
        ext_id=str(item.get("id")),
        title=(item.get("name") or "").strip(),
        url=f"https://jobs.smartrecruiters.com/{slug}/{item.get('id')}",
        location=loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if x),
        remote=True if loc.get("remote") else None,
        workplace="remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else ""),
        department=(item.get("department") or {}).get("label") or (item.get("function") or {}).get("label") or "",
        employment_type=(item.get("typeOfEmployment") or {}).get("label") or "",
        posted_at=parse_dt(item.get("releasedDate")),
        experience_hint=(item.get("experienceLevel") or {}).get("label") or "",
    )


def apply_detail(job: RawJob, detail: dict) -> None:
    ad = (detail.get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        sec = ad.get(key) or {}
        text = strip_html(sec.get("text"))
        if text:
            parts.append(f"{sec.get('title') or key}\n{text}")
    job.description = "\n\n".join(parts)
    job.url = detail.get("postingUrl") or job.url
    job.apply_url = detail.get("applyUrl") or job.url


def fetch(fetcher: Fetcher, slug: str, max_jobs: int = 300) -> list[RawJob] | None:
    jobs: list[RawJob] = []
    offset, total = 0, None
    while len(jobs) < max_jobs:
        data = fetcher.get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}")
        if not isinstance(data, dict) or "content" not in data:
            return None if offset == 0 else jobs
        total = data.get("totalFound", 0)
        if offset == 0 and total == 0:
            return None  # SmartRecruiters answers 200/empty for unknown company ids: treat as not found
        jobs.extend(parse_listing(i, slug) for i in data["content"])
        offset += 100
        if offset >= total or not data["content"]:
            break
    jobs = jobs[:max_jobs]
    for job in jobs[:DETAIL_LIMIT]:
        detail = fetcher.get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job.ext_id}")
        if isinstance(detail, dict):
            apply_detail(job, detail)
    return jobs
