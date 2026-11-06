"""Public contact addresses for a company: only role addresses published on the company's own website.

Named people's addresses are never collected and never guessed (first@company.com). If you know a founder's address,
add it yourself with `raiseradar outreach contact add`. robots.txt is honoured because pages go through `Fetcher.get_html`.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .util import registrable_domain

PATHS = ["", "/contact", "/contact-us", "/careers", "/jobs", "/about", "/company", "/team"]
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")

# Lower number = better place to send a role pitch. Anything not listed is not collected.
ROLE_RANK = {"founders": 0, "founder": 0, "cofounders": 0, "careers": 1, "jobs": 1, "hiring": 1, "talent": 1, "recruiting": 1,
             "recruitment": 1, "people": 2, "team": 3, "hello": 4, "hi": 4, "contact": 4, "info": 5}


def _clean(addr: str) -> str:
    return addr.strip().strip(".,;:<>()[]\"'").lower()


def extract_emails(html: str, domain: str) -> list[str]:
    """Role addresses on `domain` found in a page (mailto links and visible text), best first, without duplicates."""
    soup = BeautifulSoup(html or "", "html.parser")
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            found.append(_clean(a["href"][7:].split("?")[0]))
    found += [_clean(m) for m in EMAIL_RX.findall(soup.get_text(" "))]
    want = registrable_domain(domain)
    out: dict[str, int] = {}
    for e in found:
        if "@" not in e:
            continue
        local, _, host = e.partition("@")
        if registrable_domain(host) != want:
            continue
        rank = ROLE_RANK.get(local)
        if rank is None:
            continue
        out[e] = rank
    return sorted(out, key=lambda e: (out[e], e))


def find_contacts(fetcher, website: str, domain: str, max_pages: int = 6) -> list[dict]:
    """[{email, source_url, rank}] best first. Fetches at most `max_pages` pages of the company's own site."""
    base = website if "//" in website else "https://" + website
    seen: dict[str, dict] = {}
    for path in PATHS[:max_pages]:
        got = fetcher.get_html(urljoin(base if base.endswith("/") else base + "/", path.lstrip("/")))
        if not got:
            continue
        url, html = got
        for e in extract_emails(html, domain):
            seen.setdefault(e, {"email": e, "source_url": url, "rank": ROLE_RANK[e.split("@")[0]]})
    return sorted(seen.values(), key=lambda c: (c["rank"], c["email"]))
