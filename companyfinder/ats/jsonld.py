"""Fallback for custom career sites: schema.org JobPosting JSON-LD embedded in pages."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import RawJob
from ..util import parse_dt, sha, strip_html


def _iter_nodes(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _iter_nodes(x)
    elif isinstance(obj, dict):
        yield obj
        for key in ("@graph", "itemListElement", "item"):
            if key in obj:
                yield from _iter_nodes(obj[key])


def _location(node: dict) -> str:
    loc = node.get("jobLocation")
    locs = loc if isinstance(loc, list) else [loc] if loc else []
    out = []
    for l in locs:
        addr = (l or {}).get("address") if isinstance(l, dict) else None
        if isinstance(addr, dict):
            out.append(", ".join(str(addr.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry") if addr.get(k) and not isinstance(addr.get(k), dict)))
        elif isinstance(addr, str):
            out.append(addr)
    if node.get("jobLocationType") == "TELECOMMUTE":
        out.append("Remote")
    return "; ".join(o for o in out if o)


def _salary(node: dict) -> str:
    bs = node.get("baseSalary")
    if not isinstance(bs, dict):
        return ""
    val = bs.get("value") or {}
    if isinstance(val, dict) and (val.get("minValue") or val.get("value")):
        lo, hi = val.get("minValue") or val.get("value"), val.get("maxValue")
        return f"{bs.get('currency', '')} {lo}{' - ' + str(hi) if hi else ''} {val.get('unitText', '')}".strip()
    return ""


def parse_html(html: str, page_url: str) -> list[RawJob]:
    soup = BeautifulSoup(html or "", "html.parser")
    jobs: list[RawJob] = []
    for tag in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except ValueError:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]+", " ", raw))
            except ValueError:
                continue
        for node in _iter_nodes(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" not in types or not node.get("title"):
                continue
            url = urljoin(page_url, node.get("url") or "") if node.get("url") else page_url
            ident = node.get("identifier")
            ident = ident.get("value") if isinstance(ident, dict) else ident
            et = node.get("employmentType")
            jobs.append(RawJob(
                ext_id=str(ident or sha(url + node["title"], 12)),
                title=str(node["title"]).strip(),
                url=url,
                apply_url=url,
                location=_location(node),
                remote=True if node.get("jobLocationType") == "TELECOMMUTE" else None,
                workplace="remote" if node.get("jobLocationType") == "TELECOMMUTE" else "",
                department=str(node.get("occupationalCategory") or ""),
                employment_type=", ".join(et) if isinstance(et, list) else str(et or ""),
                posted_at=parse_dt(node.get("datePosted")),
                description=strip_html(node.get("description")),
                salary_text=_salary(node),
            ))
    return jobs
