"""ATS adapters + detection of which applicant-tracking system a careers page uses."""
from __future__ import annotations

import re
from typing import Callable

from ..http import Fetcher
from ..models import RawJob
from . import ashby, greenhouse, lever, recruitee, smartrecruiters, workable

# name -> (fetch function, board URL builder)
ADAPTERS: dict[str, tuple[Callable[[Fetcher, str, int], "list[RawJob] | None"], Callable[[str], str]]] = {
    "greenhouse": (greenhouse.fetch, greenhouse.board_url),
    "lever": (lever.fetch, lever.board_url),
    "ashby": (ashby.fetch, ashby.board_url),
    "workable": (workable.fetch, workable.board_url),
    "smartrecruiters": (smartrecruiters.fetch, smartrecruiters.board_url),
    "recruitee": (recruitee.fetch, recruitee.board_url),
}

_SLUG = r"([A-Za-z0-9][A-Za-z0-9_.%-]{0,60})"
# Patterns that reveal a supported ATS board slug anywhere in a page (links, iframes, scripts).
SUPPORTED_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/embed/job_board(?:/js)?\?for=" + _SLUG, re.I)),
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?!embed\b)" + _SLUG, re.I)),
    ("lever", re.compile(r"jobs(?:\.eu)?\.lever\.co/" + _SLUG, re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/" + _SLUG, re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?!api\b|j\b)" + _SLUG, re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/" + _SLUG, re.I)),
    ("recruitee", re.compile(r"(?<![\w-])((?!www\b|api\b|careers\b|app\b)[a-z0-9][a-z0-9-]{1,40})\.recruitee\.com", re.I)),
]
# Platforms we recognise (so the report can link to them) but cannot parse yet.
UNSUPPORTED_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("workday", re.compile(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", re.I)),
    ("bamboohr", re.compile(r"([a-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)", re.I)),
    ("personio", re.compile(r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.I)),
    ("teamtailor", re.compile(r"([a-z0-9-]+)\.teamtailor\.com", re.I)),
    ("rippling", re.compile(r"ats\.rippling\.com/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?" + _SLUG, re.I)),   # optional locale: /en-GB/<company>
    ("breezy", re.compile(r"([a-z0-9-]+)\.breezy\.hr", re.I)),
    ("jazzhr", re.compile(r"([a-z0-9-]+)\.applytojob\.com", re.I)),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/" + _SLUG, re.I)),
    ("icims", re.compile(r"([a-z0-9-]+)\.icims\.com", re.I)),
    ("oracle", re.compile(r"([a-z0-9-]+)\.fa\.[a-z0-9]+\.oraclecloud\.com", re.I)),
]
_BAD_SLUGS = {"embed", "api", "www", "static", "assets", "js", "css", "img", "images", "v1", "careers", "jobs", "undefined", "null"}


def board_url(ats: str, slug: str) -> str:
    return ADAPTERS[ats][1](slug) if ats in ADAPTERS else ""


def detect_ats(html: str) -> list[tuple[str, str, bool]]:
    """All ATS boards referenced in `html` as (ats, slug, supported), supported ones first, in page order."""
    found: list[tuple[int, str, str, bool]] = []
    for supported, patterns in ((True, SUPPORTED_PATTERNS), (False, UNSUPPORTED_PATTERNS)):
        for name, rx in patterns:
            for m in rx.finditer(html or ""):
                slug = m.group(1).strip(".-_").rstrip("/")
                if slug.lower() in _BAD_SLUGS or len(slug) < 2:
                    continue
                found.append((m.start(), name, slug, supported))
    found.sort(key=lambda x: (not x[3], x[0]))
    seen, out = set(), []
    for _, name, slug, supported in found:
        if (name, slug.lower()) not in seen:
            seen.add((name, slug.lower()))
            out.append((name, slug, supported))
    return out


def board_link(html: str, name: str, slug: str) -> str | None:
    """The full URL of an (unsupported) ATS board as written in the page, so the report can link straight to it."""
    for n, rx in UNSUPPORTED_PATTERNS + SUPPORTED_PATTERNS:
        if n != name:
            continue
        for m in rx.finditer(html or ""):
            if m.group(1).strip(".-_/").lower() == slug.lower():
                tail = re.match(r"[^\s\"'<>)\\]*", html[m.start():]).group(0)
                return "https://" + tail.rstrip(".,;")
    return None
