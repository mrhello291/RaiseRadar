"""How well a role fits your portfolio, and which projects and know-how are the best evidence for it. No AI involved."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .me import Expertise, Me, Project


def term_rx(term: str) -> re.Pattern:
    """Whole-word match that also works for terms like c++, c#, node.js."""
    return re.compile(r"(?<![\w+#])" + re.escape(term.strip().lower()) + r"(?![\w+#])", re.I)


@dataclass
class Match:
    score: float
    title_skills: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    project: Project | None = None                      # the best evidence for this role
    project_hits: list[str] = field(default_factory=list)
    also: list[tuple[Project, list[str]]] = field(default_factory=list)       # next best projects (up to 3)
    expertise: list[tuple[Expertise, list[str]]] = field(default_factory=list)  # relevant know-how (up to 3)


def rank_projects(me: Me, text: str) -> list[tuple[Project, list[str]]]:
    """Projects that match the role text, best evidence first. Relevance (tag hits) is weighted by how strong the
    project is, so a flagship project beats a weak one that happens to share generic words with the job post.
    Strength-1 projects are only used when no stronger project matches at all."""
    ranked = []
    for p in me.projects:
        if p.name == "Example project":
            continue
        hits = [t for t in p.tags if term_rx(t).search(text)]
        if hits:
            ranked.append((len(hits) * max(1, p.strength), p.strength, p, hits))
    if any(r[1] >= 2 for r in ranked):
        ranked = [r for r in ranked if r[1] >= 2]
    ranked.sort(key=lambda r: (-r[0], -r[1]))
    return [(p, hits) for _w, _s, p, hits in ranked]


def rank_expertise(me: Me, text: str) -> list[tuple[Expertise, list[str]]]:
    """Know-how whose tags appear in the role text, most matches first."""
    ranked = [(e, [t for t in e.tags if term_rx(t).search(text)]) for e in me.expertise]
    return sorted([r for r in ranked if r[1]], key=lambda r: -len(r[1]))


def score_job(me: Me, title: str, description: str, exp_min: float | None = None) -> Match | None:
    """None when the role is not one you want (title) or asks for far more experience than you have."""
    title = title or ""
    if me.excluded(title):
        return None
    if me.role_patterns() and not any(r.search(title) for r in me.role_patterns()):
        return None
    if me.years_experience and exp_min is not None and exp_min > me.years_experience + 1:
        return None
    text = f"{title}\n{description or ''}"
    in_title = [s for s in me.skills if term_rx(s).search(title)]
    in_text = [s for s in me.skills if term_rx(s).search(text)]
    ranked = rank_projects(me, text)
    best, best_hits = ranked[0] if ranked else (None, [])
    score = 0.3 * min(2, len(in_title)) + 0.1 * min(5, len(in_text)) + (0.2 if best else 0.0)
    return Match(round(min(1.0, score), 2), in_title, in_text, best, best_hits, ranked[1:4], rank_expertise(me, text)[:3])
