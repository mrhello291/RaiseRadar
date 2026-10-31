"""Funding-news discovery: Google News RSS searches (per round x edition) plus direct RSS feeds."""
from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus

import feedparser

from ..extract import round_keyword_regex
from ..http import Fetcher
from ..models import Article
from ..util import parse_dt, sha, strip_html

log = logging.getLogger(__name__)

_FUNDING_WORDS = re.compile(r"\b(raises?|raised|secures?|secured|closes?|lands?|bags?|funding|series[\s-]*[a-z]\b|round)\b", re.I)


def google_news_url(query: str, edition: str, days: int) -> str:
    """edition 'IN:IN:en' -> gl=IN, ceid=IN:en, hl=en-IN"""
    gl, country, lang = (edition.split(":") + ["US", "en"])[:3]
    q = quote_plus(f"{query} when:{days}d")
    return f"https://news.google.com/rss/search?q={q}&hl={lang}-{gl}&gl={gl}&ceid={country}:{lang}"


def build_feed_urls(target_rounds: list[str], editions: list[str], days: int, extra_feeds: list[str]) -> list[str]:
    urls = []
    for rnd in target_rounds:
        query = f'"{rnd}" (raises OR raised OR funding OR secures OR closes)'
        for ed in editions:
            urls.append(google_news_url(query, ed, days))
    urls.extend(extra_feeds)
    return urls


def _split_publisher(title: str, source: str | None) -> tuple[str, str]:
    if source and title.endswith(" - " + source):
        return title[: -len(source) - 3].strip(), source
    if " - " in title:
        head, _, pub = title.rpartition(" - ")
        if len(pub) <= 40:
            return head.strip(), pub.strip()
    return title.strip(), source or ""


def parse_feed(content: bytes | str, feed_url: str) -> list[Article]:
    parsed = feedparser.parse(content)
    feed_title = (parsed.feed.get("title") or "") if hasattr(parsed, "feed") else ""
    arts: list[Article] = []
    for e in parsed.entries:
        raw_title = (e.get("title") or "").strip()
        link = e.get("link") or ""
        if not raw_title or not link:
            continue
        src = (e.get("source") or {}).get("title") if isinstance(e.get("source"), dict) else None
        is_gnews = "news.google.com" in feed_url
        if is_gnews:
            title, publisher = _split_publisher(raw_title, src)
        else:
            title, publisher = raw_title, feed_title.split(" - ")[0].strip() or re.sub(r"^www\.", "", re.sub(r"^https?://", "", feed_url).split("/")[0])
        summary = strip_html(e.get("summary") or "")
        if summary.startswith(title) or is_gnews:  # Google News summaries just repeat the headline
            summary = ""
        published = parse_dt(e.get("published") or e.get("updated"))
        arts.append(Article(id=sha(e.get("id") or link), title=title, summary=summary[:600], url=link,
                            publisher=publisher, published_at=published, feed=feed_url))
    return arts


def collect_articles(fetcher: Fetcher, feed_urls: list[str]) -> list[Article]:
    """Fetch all feeds; duplicates (same id, or same normalised headline) are dropped."""
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    out: list[Article] = []
    for url in feed_urls:
        resp = fetcher.get(url)
        if resp is None or resp.status_code != 200:
            log.warning("feed failed (%s): %s", resp.status_code if resp is not None else "network", url[:110])
            continue
        n = 0
        for a in parse_feed(resp.content, url):
            tkey = re.sub(r"[^a-z0-9]+", "", a.title.lower())
            if a.id in seen_ids or tkey in seen_titles:
                continue
            seen_ids.add(a.id)
            seen_titles.add(tkey)
            out.append(a)
            n += 1
        log.info("feed ok: %3d articles  %s", n, url[:100])
    return out


def prefilter(articles: list[Article], target_rounds: list[str], require_round_keyword: bool) -> list[Article]:
    """Cheap regex gate so the LLM only sees plausible funding stories for the rounds we care about."""
    rx = round_keyword_regex(target_rounds) if require_round_keyword else _FUNDING_WORDS
    return [a for a in articles if rx.search(f"{a.title} {a.summary}")]
