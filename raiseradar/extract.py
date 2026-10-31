"""LLM step: turn news headlines into structured funding facts."""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable

from .llm import LLMClient
from .models import Article, FundingItem

log = logging.getLogger(__name__)

SYSTEM = (
    "You extract structured data about startup funding announcements from news headlines and snippets. "
    "You reply with a single JSON object and nothing else."
)

USER_TEMPLATE = """For each numbered item decide whether it reports that ONE specific company raised a funding round.
Exclude (is_funding=false): venture/PE funds raising their own fund, acquisitions, IPOs, grants, layoffs,
opinion pieces and roundups covering several companies, and items you cannot attribute to a single company.

Return exactly this JSON shape:
{{"items":[{{"id":<int>,"is_funding":<bool>,"company":<string|null>,"round":<"Pre-Seed"|"Seed"|"Series A"|"Series B"|"Series C"|"Series D"|"Series E+"|"Growth"|"Other"|"Unknown">,
"amount_usd_millions":<number|null>,"amount_text":<string|null>,"investors":[<string>],"lead_investors":[<string>],
"country":<string|null>,"description":<string|null>}}]}}

Rules:
- "company" is the startup that raised the money (not the publisher, not the investor). Use its common name without "Inc"/"Ltd".
- "round" must be stated in the text; if it is not stated use "Unknown". Do not guess.
- "amount_usd_millions": convert to USD millions (approx. 1 EUR=1.1, 1 GBP=1.3, 1 INR=0.012, 1 SGD=0.75, 1 AUD=0.65, 1 CAD=0.73 USD). null if no amount. Keep the original wording in "amount_text" (e.g. "Rs 200 crore").
- "investors" only names that appear in the text. Put the leading investor(s) also in "lead_investors". Never invent investors.
- "country": HQ country only if stated or obvious from the text, else null. "description": 6-12 words on what the company does, only if the text says.
- Output one entry for every id given, in order. This is simple extraction: do not explain, do not think at length, output only the JSON.

Items:
{items}
"""

_ROUND_NORMALISE = [
    (re.compile(r"pre[\s-]*seed", re.I), "Pre-Seed"),
    (re.compile(r"series[\s-]*a\b", re.I), "Series A"),
    (re.compile(r"series[\s-]*b\b", re.I), "Series B"),
    (re.compile(r"series[\s-]*c\b", re.I), "Series C"),
    (re.compile(r"series[\s-]*d\b", re.I), "Series D"),
    (re.compile(r"series[\s-]*[e-z]\b|series[\s-]*e\+", re.I), "Series E+"),
    (re.compile(r"\bseed\b", re.I), "Seed"),
    (re.compile(r"growth|late[\s-]*stage|pre[\s-]*ipo", re.I), "Growth"),
    (re.compile(r"unknown|undisclosed|not stated", re.I), "Unknown"),
]


def normalise_round(value: str | None) -> str:
    v = (value or "").strip()
    if not v:
        return "Unknown"
    for rx, name in _ROUND_NORMALISE:
        if rx.search(v):
            return name
    return "Other"


def round_keyword_regex(target_rounds: Iterable[str]) -> re.Pattern:
    """Regex that matches mentions of any target round, tolerant to 'Series-B', 'series b', 'B round'."""
    parts = []
    for r in target_rounds:
        m = re.fullmatch(r"series\s+([a-z])", r.strip(), re.I)
        if m:
            letter = m.group(1)
            parts.append(rf"series[\s-]*{letter}\b|\b{letter}[\s-]+round\b")
        else:
            parts.append(re.escape(r.strip()).replace(r"\ ", r"[\s-]*"))
    return re.compile("|".join(parts) or r"$^", re.I)


def _to_float(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        m = re.search(r"\d+(?:\.\d+)?", str(v).replace(",", ""))
        return float(m.group()) if m else None


def _str_list(v) -> list[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r",| and ", v) if x.strip()]
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if isinstance(x, (str, int)) and str(x).strip()][:12]


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in ("", "null", "none", "n/a", "unknown") else s


def format_items(articles: list[Article]) -> str:
    lines = []
    for i, a in enumerate(articles):
        snippet = f" -- {a.summary[:300]}" if a.summary and a.summary.strip() != a.title.strip() else ""
        lines.append(f"[{i}] {a.title} (via {a.publisher or 'unknown'}, {(a.published_at or '')[:10]}){snippet}")
    return "\n".join(lines)


def _find_items(parsed, depth: int = 0):
    """Locate the list of per-article answers, whatever wrapper the model put around it."""
    if isinstance(parsed, list):
        return parsed
    if not isinstance(parsed, dict) or depth > 2:
        return None
    if isinstance(parsed.get("items"), list):
        return parsed["items"]
    for v in parsed.values():                                   # {"results": [...]} / {"data": {"items": [...]}}
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v
        if isinstance(v, dict):
            inner = _find_items(v, depth + 1)
            if inner is not None:
                return inner
    if parsed and all(str(k).isdigit() for k in parsed) and all(isinstance(v, dict) for v in parsed.values()):
        return [{"id": int(k), **v} for k, v in parsed.items()]     # {"0": {...}, "1": {...}}
    if "id" in parsed and ("company" in parsed or "is_funding" in parsed):
        return [parsed]                                          # a single bare item
    return None


def salvage_items(text: str):
    """Recover the complete answer objects from a reply that was cut off mid-JSON (token limit)."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    dec = json.JSONDecoder()
    items, i = [], 0
    while True:
        i = text.find("{", i)
        if i == -1:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict) and "id" in obj and ("is_funding" in obj or "company" in obj):
            items.append(obj)
            i = end
        else:
            i += 1      # step inside wrappers like {"items": [...]} to reach the objects within
    return {"items": items} if items else None


def parse_response(parsed, articles: list[Article]) -> tuple[list[FundingItem], set[int]]:
    """Returns (funding items, ids that the model answered for). Tolerates sloppy model output."""
    items = _find_items(parsed)
    if not isinstance(items, list):
        keys = list(parsed)[:6] if isinstance(parsed, dict) else type(parsed).__name__
        raise ValueError(f"reply has no list of answers (keys: {keys})")
    answered: set[int] = set()
    out: list[FundingItem] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(articles):
            continue
        answered.add(idx)
        company = _clean(it.get("company"))
        funding = it.get("is_funding")
        if isinstance(funding, str):
            funding = funding.strip().lower() in ("true", "yes", "1")
        if not funding or not company or len(company) > 80:
            continue
        out.append(
            FundingItem(
                company=company,
                round=normalise_round(it.get("round")),
                amount_usd_m=_to_float(it.get("amount_usd_millions")),
                amount_text=_clean(it.get("amount_text")),
                investors=_str_list(it.get("investors")),
                lead_investors=_str_list(it.get("lead_investors")),
                country=_clean(it.get("country")),
                city=_clean(it.get("city")),
                website=_clean(it.get("website")),
                description=_clean(it.get("description")),
                article=articles[idx],
            )
        )
    if not answered:
        raise ValueError("reply answered none of the requested ids")
    return out, answered


def extract_funding(llm: LLMClient, articles: list[Article]) -> tuple[list[FundingItem], set[int]]:
    """One LLM call for a batch of articles."""
    holder: dict = {}

    def validate(parsed):
        holder["result"] = parse_response(parsed, articles)
        return True

    llm.complete_json(SYSTEM, USER_TEMPLATE.format(items=format_items(articles)), validate=validate, salvage=salvage_items)
    return holder["result"]
