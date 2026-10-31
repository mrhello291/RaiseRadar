"""Your preferences: which jobs fit you, and how interesting a company is right now."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .util import days_since


@dataclass
class Profile:
    roles: list[re.Pattern] = field(default_factory=list)
    exclude: list[re.Pattern] = field(default_factory=list)
    max_experience: float | None = None
    locations: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, section: dict) -> "Profile":
        def rx(items):
            out = []
            for p in items or []:
                try:
                    out.append(re.compile(p, re.I))
                except re.error:
                    out.append(re.compile(re.escape(p), re.I))
            return out

        mx = section.get("max_experience_years")
        return cls(
            roles=rx(section.get("roles")),
            exclude=rx(section.get("exclude_titles")),
            max_experience=float(mx) if mx is not None else None,
            locations=[l.lower() for l in section.get("locations") or []],
        )

    @property
    def active(self) -> bool:
        """False when no preference is configured (then 'matches profile' would be true for everything)."""
        return bool(self.roles or self.exclude or self.locations or self.max_experience is not None)

    def matches(self, job: dict) -> bool:
        title = job.get("title") or ""
        if self.roles and not any(r.search(title) for r in self.roles):
            return False
        if any(r.search(title) for r in self.exclude):
            return False
        exp_min = job.get("exp_min")
        if self.max_experience is not None and exp_min is not None and exp_min > self.max_experience:
            return False
        if self.locations:
            loc = (job.get("location") or "").lower()
            ok = any(l in loc for l in self.locations)
            if not ok and "remote" in self.locations and job.get("remote"):
                ok = True
            if not ok:
                return False
        return True


ROUND_WEIGHT = {"Series B": 1.0, "Series C": 1.0, "Series D": 0.8, "Series E+": 0.6, "Growth": 0.7, "Series A": 0.7}


def company_score(funding: list[dict], active_jobs: int, matched_jobs: int, is_yc: bool = False) -> float:
    """0-100. Rewards: recent target-round funding, open roles at all, and roles that match you."""
    best = 0.0
    for f in funding:
        d = days_since(f.get("announced_at"))
        recency = max(0.0, 1.0 - (d if d is not None else 180) / 180.0)
        best = max(best, recency * ROUND_WEIGHT.get(f.get("round"), 0.5))
    if is_yc and not funding:
        best = 0.35
    hiring = min(1.0, math.log1p(active_jobs) / math.log1p(25)) if active_jobs else 0.0
    fit = min(1.0, math.log1p(matched_jobs) / math.log1p(8)) if matched_jobs else 0.0
    return round(40 * best + 20 * hiring + 40 * fit, 1)
