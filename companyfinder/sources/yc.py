"""Y Combinator companies that are currently hiring (via the unofficial yc-oss static API)."""
from __future__ import annotations

import logging
import re

from ..http import Fetcher

log = logging.getLogger(__name__)
YC_HIRING_URL = "https://yc-oss.github.io/api/companies/hiring.json"


def batch_year(batch: str | None) -> int | None:
    m = re.search(r"(\d{4})", batch or "")
    return int(m.group(1)) if m else None


def select_companies(rows: list[dict], *, min_team: int, max_team: int, max_batch_year: int, limit: int) -> list[dict]:
    picked = []
    for r in rows:
        if not r.get("website") or r.get("status", "Active") != "Active" or r.get("nonprofit"):
            continue
        team = r.get("team_size") or 0
        if team < min_team or (max_team and team > max_team):
            continue
        by = batch_year(r.get("batch"))
        if max_batch_year and by and by > max_batch_year:
            continue
        picked.append(r)
    picked.sort(key=lambda r: -(r.get("team_size") or 0))
    return picked[:limit] if limit else picked


def to_company(r: dict) -> dict:
    parts = [p.strip() for p in (r.get("all_locations") or "").split(";")[0].split(",") if p.strip()]
    return {
        "name": r["name"],
        "website": r["website"],
        "description": r.get("one_liner") or (r.get("long_description") or "")[:200],
        "country": parts[-1] if parts else None,
        "city": parts[0] if len(parts) > 1 else None,
        "extra": {
            "yc_batch": r.get("batch"),
            "yc_url": r.get("url"),
            "team_size": r.get("team_size"),
            "tags": (r.get("tags") or [])[:8],
        },
    }


def fetch_yc(fetcher: Fetcher, cfg: dict) -> list[dict]:
    rows = fetcher.get_json(YC_HIRING_URL)
    if not isinstance(rows, list):
        log.warning("could not load YC hiring list")
        return []
    picked = select_companies(
        rows,
        min_team=cfg["min_team_size"], max_team=cfg["max_team_size"],
        max_batch_year=cfg["max_batch_year"], limit=cfg["max_companies"],
    )
    log.info("YC: %d hiring companies, %d match filters", len(rows), len(picked))
    return [to_company(r) for r in picked]
