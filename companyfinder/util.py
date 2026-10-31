"""Small shared helpers: names, slugs, HTML->text, dates."""
from __future__ import annotations

import hashlib
import html as htmllib
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "pvt", "private", "gmbh", "ag", "sa", "plc", "bv", "nv", "oy", "ab", "pte", "pty", "llp", "lp",
}
# words that carry no identity when checking that a web page belongs to a company
GENERIC_WORDS = {"ai", "labs", "lab", "technologies", "technology", "tech", "systems", "software", "the", "app", "hq", "io"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha(text: str, n: int = 24) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:n]


def parse_dt(value) -> str | None:
    """Best-effort conversion of ISO strings / epoch (s or ms) / RFC822 to an ISO-8601 UTC string."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat()
        s = str(value).strip()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        return None


def days_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        return None


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def name_tokens(name: str) -> list[str]:
    s = ascii_fold(name).lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t]


def norm_name(name: str) -> str:
    """Canonical key for de-duplicating company names: lowercase, no punctuation, no legal suffixes."""
    toks = name_tokens(name)
    while len(toks) > 1 and toks[-1] in LEGAL_SUFFIXES:
        toks.pop()
    return "".join(toks)


def distinctive_tokens(name: str) -> list[str]:
    toks = [t for t in name_tokens(name) if t not in LEGAL_SUFFIXES]
    core = [t for t in toks if t not in GENERIC_WORDS]
    return core or toks


def domain_of(url: str) -> str:
    host = urlparse(url if "//" in url else "//" + url).netloc.lower()
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def registrable_label(domain: str) -> str:
    """'careers.acme.co.uk' -> 'acme'. Approximate (no public-suffix list), good enough for matching."""
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac"} and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def registrable_domain(domain: str) -> str:
    """'blog.acme.co.uk' -> 'acme.co.uk', 'www.acme.io' -> 'acme.io' (approximate; no public-suffix list)."""
    parts = (domain or "").lower().split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else (domain or "")


def slug_candidates(name: str, domain: str | None = None) -> list[str]:
    """Plausible ATS board slugs for a company, most likely first."""
    toks = [t for t in name_tokens(name) if t not in LEGAL_SUFFIXES] or name_tokens(name)
    out: list[str] = []

    def add(s: str):
        s = s.strip("-")
        if s and s not in out:
            out.append(s)

    if domain:
        add(registrable_label(domain))
    add("".join(toks))
    add("-".join(toks))
    core = [t for t in toks if t not in GENERIC_WORDS]
    if core and core != toks:
        add("".join(core))
        add("-".join(core))
    return out[:5]


def strip_html(raw: str | None) -> str:
    """HTML (possibly entity-escaped HTML, as Greenhouse returns) -> readable plain text with line breaks."""
    if not raw:
        return ""
    text = raw
    # Greenhouse double-escapes: "&lt;p&gt;..." -> unescape until tags appear
    if "&lt;" in text and "<" not in text:
        text = htmllib.unescape(text)
    soup = BeautifulSoup(text, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for blk in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr"]):
        blk.insert_after("\n")
    out = soup.get_text()
    out = htmllib.unescape(out).replace("\xa0", " ")
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n[ \t]+", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def clean_title(t: str | None) -> str:
    return re.sub(r"\s+", " ", t or "").strip()
