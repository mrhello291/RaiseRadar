"""Recruitee public offers API: {slug}.recruitee.com/api/offers/"""
from __future__ import annotations

from ..http import Fetcher
from ..models import RawJob
from ..util import parse_dt, strip_html

NAME = "recruitee"


def board_url(slug: str) -> str:
    return f"https://{slug}.recruitee.com"


def parse(data: dict) -> list[RawJob]:
    jobs = []
    for j in data.get("offers", []):
        if j.get("status") not in (None, "published"):
            continue
        sal = j.get("salary") or {}
        salary = ""
        if isinstance(sal, dict) and sal.get("min"):
            salary = f"{sal.get('currency', '')} {sal.get('min')} - {sal.get('max')} {sal.get('period', '')}".strip()
        desc = "\n\n".join(x for x in (strip_html(j.get("description")), strip_html(j.get("requirements"))) if x)
        jobs.append(RawJob(
            ext_id=str(j.get("id")),
            title=(j.get("title") or "").strip(),
            url=j.get("careers_url") or "",
            apply_url=j.get("careers_apply_url") or j.get("careers_url") or "",
            location=j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x),
            remote=True if (j.get("remote") or j.get("workplace") == "remote") else None,
            workplace="remote" if (j.get("remote") or j.get("workplace") == "remote") else ("hybrid" if j.get("hybrid") else ("onsite" if j.get("on_site") else "")),
            department=j.get("department") or j.get("category_code") or "",
            employment_type=(j.get("employment_type_code") or "").replace("_", " "),
            posted_at=parse_dt(j.get("published_at") or j.get("created_at")),
            description=desc,
            salary_text=salary,
            experience_hint=(j.get("experience_code") or "").replace("_", " "),
        ))
    return jobs


def fetch(fetcher: Fetcher, slug: str, max_jobs: int = 300) -> list[RawJob] | None:
    data = fetcher.get_json(f"https://{slug}.recruitee.com/api/offers/")
    if not isinstance(data, dict) or "offers" not in data:
        return None
    return parse(data)[:max_jobs]
