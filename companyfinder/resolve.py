"""Turn a company *name* (from news) into its verified official website.

News gives us "Acme raises $30M", never the website. We generate plausible domains, then only accept one if the
page itself proves it belongs to that company (name in <title>/<h1>/og:site_name, not a parked domain).
"""
from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .http import Fetcher
from .util import distinctive_tokens, domain_of, name_tokens, registrable_label

log = logging.getLogger(__name__)

BASE_TLDS = [".com", ".ai", ".io", ".co", ".app", ".dev", ".tech", ".so", ".xyz"]
COUNTRY_TLDS = {
    "india": [".in", ".co.in"], "united kingdom": [".co.uk"], "uk": [".co.uk"], "england": [".co.uk"],
    "germany": [".de"], "singapore": [".sg"], "australia": [".com.au"], "france": [".fr"], "netherlands": [".nl"],
    "canada": [".ca"], "brazil": [".com.br"], "south africa": [".co.za"], "israel": [".co.il"], "sweden": [".se"],
    "spain": [".es"], "japan": [".co.jp"], "united arab emirates": [".ae"], "uae": [".ae"], "ireland": [".ie"],
    "nigeria": [".com.ng"], "kenya": [".co.ke"], "indonesia": [".co.id"], "switzerland": [".ch"], "finland": [".fi"],
    "denmark": [".dk"], "norway": [".no"], "poland": [".pl"], "estonia": [".ee"], "lithuania": [".lt"],
}
PREFIXES = ["get", "try", "use", "join", "go"]
SUFFIXES = ["hq", "app", "labs"]
PARKED = re.compile(
    r"domain (?:is )?for sale|buy this domain|this domain (?:may be|is) (?:for sale|parked)|parked (?:free|domain)|"
    r"hugedomains|sedo\.com|dan\.com|afternic|godaddy\.com/domainsearch|domain name is available|coming soon\s*$",
    re.I,
)


@dataclass
class Resolved:
    url: str
    domain: str
    confidence: str     # "verified" | "hint"
    score: int


def candidate_domains(name: str, country: str | None = None, hint: str | None = None) -> list[str]:
    toks = [t for t in name_tokens(name)]
    from .util import LEGAL_SUFFIXES  # local import keeps the module header small

    toks = [t for t in toks if t not in LEGAL_SUFFIXES] or toks
    core = distinctive_tokens(name)
    slugs: list[str] = []
    for s in ("".join(toks), "-".join(toks), "".join(core), "-".join(core)):
        if s and s not in slugs and len(s) >= 2:
            slugs.append(s)
    tlds = list(BASE_TLDS)
    for tld in COUNTRY_TLDS.get((country or "").strip().lower(), []):
        tlds.insert(1, tld)
    out: list[str] = []
    if hint:
        d = domain_of(hint)
        if d:
            out.append(d)
    for s in slugs[:3]:
        for t in tlds:
            out.append(s + t)
    first = core[0] if len(core) >= 2 and len(core[0]) >= 4 else None     # "Upwind Security" -> upwind.io
    if first:
        for t in (".com", ".io", ".ai", ".co"):
            out.append(first + t)
    for s in slugs[:1]:
        for p in PREFIXES:
            out.append(f"{p}{s}.com")
        for x in SUFFIXES:
            out.append(f"{s}{x}.com")
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def _compress(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def score_page(html: str, final_url: str, name: str, description: str | None = None) -> int:
    """How strongly this page proves it belongs to `name`. >=3 is accepted."""
    soup = BeautifulSoup(html, "html.parser")
    head_bits = [soup.title.get_text(" ") if soup.title else ""]
    for sel in ({"property": "og:site_name"}, {"property": "og:title"}, {"name": "application-name"}, {"name": "twitter:title"}):
        tag = soup.find("meta", attrs=sel)
        if tag and tag.get("content"):
            head_bits.append(tag["content"])
    h1 = soup.find("h1")
    if h1:
        head_bits.append(h1.get_text(" "))
    head = _compress(" ".join(head_bits))
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    body_text = soup.get_text(" ")
    body = _compress(body_text[:6000])
    if PARKED.search(body_text[:3000]) or (len(body_text.strip()) < 40 and not head):
        return -5

    toks = distinctive_tokens(name)
    joined = "".join(toks)
    in_head = bool(joined) and (joined in head or all(_compress(t) in head for t in toks))
    in_body = bool(joined) and joined in body
    label = registrable_label(domain_of(final_url))
    all_joined = "".join(name_tokens(name))
    domain_match = label in {joined, all_joined} or (len(joined) >= 4 and joined in label)
    score = (2 if in_head else 0) + (1 if domain_match else 0) + (1 if in_body else 0)

    if description:
        words = {w for w in re.findall(r"[a-z]{5,}", description.lower())}
        page_words = set(re.findall(r"[a-z]{5,}", body_text[:6000].lower()))
        overlap = len(words & page_words)
        if overlap >= 2:
            score += 1
        elif len(toks) <= 1 and len(page_words) > 60 and words and overlap == 0:
            score -= 2   # short/common names: demand some topical agreement when we have a description
    return score


def description_overlap(html: str, description: str | None) -> int:
    """How many distinctive words of the LLM's description of the company also appear on the page."""
    if not description:
        return 0
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    page_words = set(re.findall(r"[a-z]{5,}", soup.get_text(" ")[:8000].lower()))
    return len(set(re.findall(r"[a-z]{5,}", description.lower())) & page_words)


def _dns_ok(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


def resolve_website(fetcher: Fetcher, name: str, *, country: str | None = None, hint: str | None = None,
                    description: str | None = None, max_probes: int = 24) -> Resolved | None:
    probes = 0
    hint_domain = domain_of(hint) if hint else None
    for dom in candidate_domains(name, country, hint):
        if probes >= max_probes:
            break
        if not _dns_ok(dom):
            continue
        probes += 1
        resp = fetcher.get("https://" + dom, retries=0) or fetcher.get("http://" + dom, retries=0)
        if resp is None or resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html"):
            continue
        final = str(resp.url)
        score = score_page(resp.text[:400_000], final, name, description)
        # redirected to an unrelated brand (casap.com -> some other company's site): not ours
        final_label, cand_label = registrable_label(domain_of(final)), registrable_label(dom)
        if final_label != cand_label and "".join(distinctive_tokens(name)) not in final_label:
            score -= 2
        threshold = 2 if dom == hint_domain else 3
        log.debug("resolve %s: %s -> score %d", name, dom, score)
        if score >= threshold:
            p = urlparse(final)
            host = p.hostname or domain_of(final)
            port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
            if dom == hint_domain:
                confidence = "hint"
            else:
                # "verified" only when the page also talks about what the LLM said the company does; otherwise a
                # namesake (Hive, Matrix, ASI ...) cannot be ruled out. Very short names are never auto-trusted.
                supported = description_overlap(resp.text[:400_000], description) >= 2
                confidence = "verified" if supported and len("".join(distinctive_tokens(name))) > 4 else "unsure"
            return Resolved(url=f"{p.scheme}://{host}{port}", domain=domain_of(final), confidence=confidence, score=score)
    return None
