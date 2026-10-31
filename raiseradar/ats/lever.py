"""Lever Postings API (public, no auth): api.lever.co (EU region: api.eu.lever.co)"""
from __future__ import annotations

from ..http import Fetcher
from ..models import RawJob
from ..util import parse_dt, strip_html

NAME = "lever"


def board_url(slug: str) -> str:
    return f"https://jobs.lever.co/{slug}"


def _salary(sr: dict | None) -> str:
    if not isinstance(sr, dict) or not sr.get("min"):
        return ""
    try:
        return f"{sr.get('currency', '')} {int(sr['min']):,} - {int(sr.get('max') or sr['min']):,} ({(sr.get('interval') or '').replace('per-', '').replace('-salary', '')})".strip()
    except (TypeError, ValueError):
        return ""


def parse(data: list) -> list[RawJob]:
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        parts = [j.get("descriptionPlain") or strip_html(j.get("description"))]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{strip_html(lst.get('content'))}")
        parts.append(j.get("additionalPlain") or strip_html(j.get("additional")))
        wp = (j.get("workplaceType") or "").lower()
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        jobs.append(RawJob(
            ext_id=str(j.get("id")),
            title=(j.get("text") or "").strip(),
            url=j.get("hostedUrl") or "",
            apply_url=j.get("applyUrl") or "",
            location=loc,
            remote=True if wp == "remote" else (False if wp in ("on-site", "hybrid") else ("remote" in loc.lower() or None)),
            workplace={"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}.get(wp, ""),
            department=cats.get("department") or cats.get("team") or "",
            employment_type=cats.get("commitment") or "",
            posted_at=parse_dt(j.get("createdAt")),
            description="\n\n".join(p.strip() for p in parts if p and p.strip()),
            salary_text=_salary(j.get("salaryRange")),
        ))
    return jobs


def fetch(fetcher: Fetcher, slug: str, max_jobs: int = 300) -> list[RawJob] | None:
    for host in ("api.lever.co", "api.eu.lever.co"):
        data = fetcher.get_json(f"https://{host}/v0/postings/{slug}?mode=json")
        if isinstance(data, list):
            return parse(data)[:max_jobs]
    return None
