"""Find where a company hires (its careers page / ATS board) and fetch its jobs from there."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import ats as ats_mod
from .ats import jsonld
from .http import Fetcher
from .models import RawJob
from .util import days_since, distinctive_tokens, domain_of, registrable_label, slug_candidates

log = logging.getLogger(__name__)

CAREER_TEXT = re.compile(r"\b(careers?|jobs?|join (?:us|our team|the team)|we(?:'re| are) hiring|hiring|open (?:roles|positions)|work (?:with|at) us|openings|vacancies)\b", re.I)
CAREER_HREF = re.compile(r"(careers?|/jobs?\b|join[-_]?us|work[-_]?with[-_]?us|hiring|openings|vacanc|/team/join|/company/jobs)", re.I)
COMMON_PATHS = ["/careers", "/jobs", "/careers/", "/company/careers", "/about/careers", "/join", "/join-us", "/work-with-us", "/company/jobs"]
JOB_LINK = re.compile(r"/(jobs?|careers?|positions?|openings?|opportunit\w*|vacanc\w*|roles?)/[^/?#]{4,}", re.I)
HIDDEN_ATS_HINT = re.compile(r"[?&](gh_jid|gh_src)=|[?&]ashby_jid=", re.I)


@dataclass
class CareersInfo:
    careers_url: str | None = None
    ats: str | None = None
    ats_slug: str | None = None
    ats_confidence: str | None = None      # "linked" (found on their own site) | "guessed" (probed + verified)
    supported: bool = False                # we can fetch jobs from it
    notes: str = ""
    retry_soon: bool = False               # homepage failed (maybe transient): look again tomorrow, not in 2 weeks


def _anchors(html: str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        out.append((a.get_text(" ", strip=True)[:80], urljoin(base, href)))
    return out


def career_candidates(html: str, base_url: str) -> list[str]:
    """Careers-page URLs linked from a homepage, best first (same-site links beat third-party ones)."""
    site = registrable_label(domain_of(base_url))
    scored: list[tuple[int, str]] = []
    for text, url in _anchors(html, base_url):
        by_text, by_href = bool(CAREER_TEXT.search(text)), bool(CAREER_HREF.search(urlparse(url).path + "?" + urlparse(url).query))
        if not (by_text or by_href):
            continue
        same = registrable_label(domain_of(url)) == site
        known_ats = bool(ats_mod.detect_ats(url))
        if not (same or known_ats):
            continue
        score = (2 if by_text else 0) + (1 if by_href else 0) + (2 if known_ats else 0) + (1 if same else 0)
        # prefer short, listing-like paths over a single posting
        score -= min(len(urlparse(url).path.strip("/").split("/")), 4) * 0.1
        scored.append((score, url.split("#")[0]))
    scored.sort(key=lambda x: -x[0])
    out: list[str] = []
    for _, u in scored:
        if u not in out:
            out.append(u)
    return out


def board_belongs(jobs: list[RawJob], name: str, domain: str | None) -> bool:
    """Is a guessed ATS board really this company's? Its postings must mention the company name or domain."""
    toks = "".join(distinctive_tokens(name))
    label = registrable_label(domain) if domain else ""
    for j in jobs[:15]:
        blob = re.sub(r"[^a-z0-9]", "", f"{j.title} {j.description[:6000]} {j.url}".lower())
        if toks and len(toks) >= 3 and toks in blob:
            return True
        if label and len(label) >= 4 and label in blob:
            return True
    return False


def probe_boards(fetcher: Fetcher, name: str, domain: str | None) -> tuple[str, str] | None:
    """Guess ATS board slugs. A board is accepted only when its postings mention the company; if several verify
    (companies migrate between ATSs and leave stale boards behind) the live, largest one wins."""
    best: tuple[tuple, str, str] | None = None   # (rank, ats, slug)
    for ats_name in ("greenhouse", "lever", "ashby"):
        fn = ats_mod.ADAPTERS[ats_name][0]
        for slug in slug_candidates(name, domain)[:3]:
            jobs = fn(fetcher, slug, 400)
            if jobs and board_belongs(jobs, name, domain):
                newest = max((j.posted_at or "" for j in jobs), default="")
                age = days_since(newest)
                rank = (age is not None and age < 120, len(jobs), newest)
                if best is None or rank > best[0]:
                    best = (rank, ats_name, slug)
                break   # first verifying slug for this ATS is enough
    return (best[1], best[2]) if best else None


def discover(fetcher: Fetcher, website: str, name: str, *, probe: bool = True) -> CareersInfo:
    """Locate the hiring page / ATS for a company website."""
    home = fetcher.get_html(website)
    if home is None:
        info = CareersInfo(notes="homepage unreachable or blocked (robots.txt / bot protection)", retry_soon=True)
        hit = probe_boards(fetcher, name, domain_of(website)) if probe else None
        if hit:   # the site is walled off but its job board is not
            info.ats, info.ats_slug, info.ats_confidence, info.supported = hit[0], hit[1], "guessed", True
            info.careers_url, info.notes, info.retry_soon = ats_mod.board_url(*hit), "", False
        return info
    base, html = home
    domain = domain_of(base)
    info = CareersInfo()
    unsupported: tuple[str, str, str] | None = None   # (ats, slug, careers_url)
    jsonld_page: str | None = None
    hidden_hint = bool(HIDDEN_ATS_HINT.search(html))

    def consider(page_html: str, page_url: str) -> bool:
        nonlocal unsupported
        for name_, slug, supported in ats_mod.detect_ats(page_html):
            if supported:
                info.ats, info.ats_slug, info.ats_confidence, info.supported = name_, slug, "linked", True
                info.careers_url = ats_mod.board_url(name_, slug) if page_url == base else page_url
                return True
            unsupported = unsupported or (name_, slug, ats_mod.board_link(page_html, name_, slug) or page_url)
        return False

    if consider(html, base):
        return info

    candidates = career_candidates(html, base)
    if not candidates:
        origin = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
        candidates = [origin + p for p in COMMON_PATHS] + [f"https://careers.{domain}", f"https://jobs.{domain}"]
    seen_pages = set()
    page_html_for_careers: dict[str, str] = {}
    first_ok: str | None = None
    for url in candidates[:6]:
        if url in seen_pages:
            continue
        seen_pages.add(url)
        got = fetcher.get_html(url)
        if got is None:
            continue
        final, page = got
        if urlparse(final).path.strip("/") == "" and url != base and registrable_label(domain_of(final)) == registrable_label(domain):
            continue   # redirected back to the homepage: that candidate does not exist
        first_ok = first_ok or final
        page_html_for_careers[final] = page
        if HIDDEN_ATS_HINT.search(page):
            hidden_hint = True
        if consider(page, final):
            info.careers_url = final
            return info
        if not jsonld_page and jsonld.parse_html(page, final):
            jsonld_page = final
        if len(page_html_for_careers) >= 3:
            break

    info.careers_url = first_ok
    if probe:
        hit = probe_boards(fetcher, name, domain)
        if hit:
            info.ats, info.ats_slug, info.ats_confidence, info.supported = hit[0], hit[1], "guessed", True
            info.careers_url = info.careers_url or ats_mod.board_url(hit[0], hit[1])
            return info
    if jsonld_page:
        info.ats, info.ats_slug, info.ats_confidence, info.supported = "jsonld", jsonld_page, "linked", True
        info.careers_url = jsonld_page
        return info
    if unsupported:
        info.ats, info.ats_slug, info.ats_confidence, info.supported = unsupported[0], unsupported[1], "linked", False
        info.careers_url = unsupported[2]
        info.notes = f"uses {unsupported[0]} (not supported yet)"
        return info
    info.notes = ("jobs embedded via a script (ATS slug not visible)" if hidden_hint
                  else "careers page found, no machine-readable job list" if first_ok else "no careers page found")
    return info


def jsonld_site_jobs(fetcher: Fetcher, careers_url: str, max_pages: int = 25) -> list[RawJob] | None:
    """Jobs from schema.org JSON-LD on the careers page, or on job-detail pages it links to."""
    got = fetcher.get_html(careers_url)
    if got is None:
        return None
    final, html = got
    jobs = jsonld.parse_html(html, final)
    if jobs:
        return jobs
    site = registrable_label(domain_of(final))
    seen, links = set(), []
    for _, url in _anchors(html, final):
        if registrable_label(domain_of(url)) == site and JOB_LINK.search(urlparse(url).path) and url not in seen:
            seen.add(url)
            links.append(url)
    out: list[RawJob] = []
    for url in links[:max_pages]:
        page = fetcher.get_html(url)
        if page:
            out.extend(jsonld.parse_html(page[1], page[0]))
    return out


def fetch_jobs(fetcher: Fetcher, ats: str, slug: str, max_jobs: int = 300) -> list[RawJob] | None:
    """None = board gone/unreachable; [] = no openings."""
    if ats == "jsonld":
        jobs = jsonld_site_jobs(fetcher, slug)
        return jobs[:max_jobs] if jobs is not None else None
    if ats in ats_mod.ADAPTERS:
        return ats_mod.ADAPTERS[ats][0](fetcher, slug, max_jobs)
    return None
