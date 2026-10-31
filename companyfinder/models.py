"""Plain data containers passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Article:
    id: str
    title: str
    summary: str
    url: str
    publisher: str
    published_at: str | None
    feed: str


@dataclass
class FundingItem:
    """One funding announcement extracted from an article."""
    company: str
    round: str
    amount_usd_m: float | None
    amount_text: str | None
    investors: list[str]
    lead_investors: list[str]
    country: str | None
    city: str | None
    website: str | None
    description: str | None
    article: Article


@dataclass
class RawJob:
    """A job as returned by an ATS adapter, before enrichment."""
    ext_id: str
    title: str
    url: str
    location: str = ""
    remote: bool | None = None
    workplace: str = ""         # "remote" | "hybrid" | "onsite" | "" (unknown), as stated by the ATS
    department: str = ""
    employment_type: str = ""
    apply_url: str = ""
    posted_at: str | None = None
    description: str = ""
    salary_text: str = ""
    experience_hint: str = ""   # e.g. SmartRecruiters "Mid-Senior Level"
    extra: dict = field(default_factory=dict)
