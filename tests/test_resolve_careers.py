import pytest

from raiseradar import careers, resolve
from raiseradar.models import RawJob
from raiseradar.util import norm_name, slug_candidates
from conftest import html_response, json_response, make_fetcher


def page(title, body="", extra_head=""):
    return f"<html><head><title>{title}</title>{extra_head}</head><body><h1>{title}</h1><p>{body}</p>" + "<p>lorem ipsum dolor sit amet consectetur</p>" * 20 + "</body></html>"


# ----------------------------------------------------------------------------- names / candidates
def test_norm_name_and_slugs():
    assert norm_name("Acme Labs, Inc.") == norm_name("acme labs") == "acmelabs"
    assert norm_name("Café Büro GmbH") == "cafebuero" or norm_name("Café Büro GmbH").startswith("cafe")
    s = slug_candidates("Fireworks AI Inc", "fireworks.ai")
    assert s[0] == "fireworks" and "fireworksai" in s
    assert slug_candidates("Peak Energy")[:2] == ["peakenergy", "peak-energy"]


def test_candidate_domains_prioritise_hint_and_country():
    c = resolve.candidate_domains("Simplismart", "India", hint="https://www.simplismart.ai/about")
    assert c[0] == "simplismart.ai" and "simplismart.com" in c and "simplismart.in" in c
    assert len(c) == len(set(c))


# ----------------------------------------------------------------------------- page scoring
def test_score_accepts_real_company_page():
    html = page("Infisical | Open source secret management", "Infisical helps teams manage secrets")
    assert resolve.score_page(html, "https://infisical.com/", "Infisical", "secrets management platform") >= 3


def test_score_rejects_parked_and_unrelated_pages():
    assert resolve.score_page(page("acme.com", "This domain is for sale. Buy this domain today."), "https://acme.com", "Acme") < 3
    assert resolve.score_page(page("Totally Different Corp", "We sell shoes"), "https://acme.com", "Acme") < 3
    assert resolve.score_page("<html><body></body></html>", "https://acme.com", "Acme") < 3


FILLER = " ".join(f"word{chr(97 + i % 26)}{chr(97 + i // 26)}x" for i in range(120))   # 120 distinct words: a real, text-rich page


def test_score_uses_description_to_reject_namesakes():
    shoes = page("Hive", "Hive makes premium running shoes and sneakers for athletes everywhere. " + FILLER)
    soft = page("Hive", "Hive is project management software for teams, with automation and analytics. " + FILLER)
    desc = "project management software platform"
    assert resolve.score_page(soft, "https://hive.com", "Hive", desc) >= 3
    assert resolve.score_page(shoes, "https://hive.com", "Hive", desc) < 3


def test_resolve_rejects_cross_domain_redirect(monkeypatch):
    """casap.com redirects to an unrelated brand whose page happens to mention 'Casap' (a real false positive we hit)."""
    import httpx
    monkeypatch.setattr(resolve, "_dns_ok", lambda h: True)

    def handler(req):
        host = req.url.host
        if host == "casap.com":
            return httpx.Response(302, headers={"location": "https://realestate.orisha.com/"})
        if host == "realestate.orisha.com":
            return html_response(page("Orisha Casap", "Orisha Casap: software for holiday residences"))
        return html_response("", 404)

    assert resolve.resolve_website(make_fetcher(handler), "Casap", max_probes=4) is None


def test_resolve_accepts_same_brand_redirect(monkeypatch):
    import httpx
    monkeypatch.setattr(resolve, "_dns_ok", lambda h: h == "acme.com")

    def handler(req):
        if req.url.host == "acme.com":
            return httpx.Response(301, headers={"location": "https://www.acme.com/en"})
        return html_response(page("Acme - the platform", "Acme"))

    r = resolve.resolve_website(make_fetcher(handler), "Acme")
    assert r and r.domain == "acme.com"


def test_resolve_picks_first_verified_candidate(monkeypatch):
    monkeypatch.setattr(resolve, "_dns_ok", lambda h: h in {"acme.com", "acme.io"})

    def handler(req):
        if req.url.host == "acme.com":
            return html_response(page("acme.com", "This domain is for sale"))
        if req.url.host == "acme.io":
            return html_response(page("Acme - build faster", "Acme platform"))
        return html_response("", 404)

    r = resolve.resolve_website(make_fetcher(handler), "Acme")
    assert r and r.domain == "acme.io" and r.confidence == "unsure"      # no description to cross-check -> not fully trusted


def test_resolve_trusts_hint_with_lower_threshold(monkeypatch):
    monkeypatch.setattr(resolve, "_dns_ok", lambda h: h == "weird-name.xyz")
    handler = lambda req: html_response(page("Welcome", "We are Weirdo company")) if req.url.host == "weird-name.xyz" else html_response("", 404)
    assert resolve.resolve_website(make_fetcher(handler), "Weirdo", hint="https://weird-name.xyz") is None  # score too low even for hints
    handler2 = lambda req: html_response(page("Weirdo Inc", "hello")) if req.url.host == "weird-name.xyz" else html_response("", 404)
    r = resolve.resolve_website(make_fetcher(handler2), "Weirdo", hint="https://weird-name.xyz")
    assert r and r.confidence == "hint"


# ----------------------------------------------------------------------------- careers discovery
HOME_WITH_LINK = '<html><body><nav><a href="/product">Product</a><a href="/careers">Careers</a></nav></body></html>'


def test_discover_follows_careers_link_to_lever_board():
    def handler(req):
        u = str(req.url)
        if u.rstrip("/") == "https://acme.com":
            return html_response(HOME_WITH_LINK)
        if u == "https://acme.com/careers":
            return html_response('<html><a href="https://jobs.lever.co/acme-co">Open roles</a></html>')
        return html_response("", 404)

    info = careers.discover(make_fetcher(handler), "https://acme.com", "Acme")
    assert (info.ats, info.ats_slug, info.ats_confidence, info.supported) == ("lever", "acme-co", "linked", True)
    assert info.careers_url == "https://acme.com/careers"


def test_discover_sees_ats_link_directly_on_homepage():
    handler = lambda req: html_response('<html><a href="https://job-boards.greenhouse.io/acme">We are hiring</a></html>') if req.url.host == "acme.com" else html_response("", 404)
    info = careers.discover(make_fetcher(handler), "https://acme.com", "Acme")
    assert info.ats == "greenhouse" and info.careers_url == "https://boards.greenhouse.io/acme"


def test_discover_probes_and_verifies_guessed_boards():
    def handler(req):
        h, u = req.url.host, str(req.url)
        if h == "acme.com":
            return html_response("<html><body>Nothing about jobs</body></html>")
        if h == "api.ashbyhq.com" and "/acme?" in u:
            return json_response({"jobs": [{"id": "1", "title": "Engineer", "descriptionPlain": "Acme is hiring a great engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/1"}]})
        if h == "boards-api.greenhouse.io" and "/acme/" in u:   # a *different* company squatting the slug
            return json_response({"jobs": [{"id": 9, "title": "Baker", "absolute_url": "https://x/9", "content": "We bake bread"}]})
        return html_response("", 404)

    info = careers.discover(make_fetcher(handler), "https://acme.com", "Acme")
    assert (info.ats, info.ats_slug, info.ats_confidence) == ("ashby", "acme", "guessed")


def test_discover_prefers_live_board_over_stale_one():
    def handler(req):
        h = req.url.host
        if h == "acme.com":
            return html_response("<html></html>")
        if h == "boards-api.greenhouse.io":
            return json_response({"jobs": [{"id": 1, "title": "Old", "absolute_url": "u", "first_published": "2021-01-01T00:00:00Z", "content": "Acme old job"}]})
        if h == "api.ashbyhq.com":
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            return json_response({"jobs": [{"id": "n", "title": "New", "descriptionPlain": "Acme new job", "publishedAt": now, "jobUrl": "u"}]})
        return html_response("", 404)

    assert careers.discover(make_fetcher(handler), "https://acme.com", "Acme").ats == "ashby"


def test_discover_reports_unsupported_ats_and_custom_pages():
    wd = lambda req: html_response('<html><a href="https://acme.wd3.myworkdayjobs.com/Careers">Careers</a></html>') if req.url.host == "acme.com" else html_response("", 404)
    info = careers.discover(make_fetcher(wd), "https://acme.com", "Acme", probe=False)
    assert info.ats == "workday" and not info.supported and "not supported" in info.notes

    plain = lambda req: html_response(HOME_WITH_LINK) if str(req.url).rstrip("/") == "https://acme.com" else (html_response("<html>Join our team</html>") if req.url.path == "/careers" else html_response("", 404))
    info = careers.discover(make_fetcher(plain), "https://acme.com", "Acme", probe=False)
    assert info.ats is None and info.careers_url == "https://acme.com/careers" and "no machine-readable" in info.notes


def test_discover_unreachable_homepage_and_robots():
    info = careers.discover(make_fetcher(lambda r: html_response("", 500)), "https://acme.com", "Acme")
    assert "unreachable" in info.notes

    import httpx

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /", headers={"content-type": "text/plain"})
        return html_response("<html>hi</html>")

    assert "robots" in careers.discover(make_fetcher(handler, robots=True), "https://acme.com", "Acme").notes


def test_discover_jsonld_fallback():
    ld = '<script type="application/ld+json">{"@type":"JobPosting","title":"Engineer","description":"x"}</script>'
    def handler(req):
        if req.url.host != "acme.com":
            return html_response("", 404)
        if req.url.path in ("", "/"):
            return html_response(HOME_WITH_LINK)
        if req.url.path == "/careers":
            return html_response(f"<html>{ld}</html>")
        return html_response("", 404)
    info = careers.discover(make_fetcher(handler), "https://acme.com", "Acme", probe=False)
    assert info.ats == "jsonld" and info.supported
    jobs = careers.fetch_jobs(make_fetcher(handler), "jsonld", info.ats_slug)
    assert [j.title for j in jobs] == ["Engineer"]


def test_board_belongs():
    jobs = [RawJob("1", "Engineer", "https://x", description="Join Peak Energy to build batteries")]
    assert careers.board_belongs(jobs, "Peak Energy", "peakenergy.com")
    assert not careers.board_belongs([RawJob("1", "Baker", "https://x", description="bread")], "Peak Energy", "peakenergy.com")


def test_blocked_homepage_still_finds_the_job_board():
    """Bot-walled sites (HTTP 403) often still have a public ATS board."""
    def handler(req):
        if req.url.host == "acme.com":
            return html_response("Forbidden", 403)
        if req.url.host == "boards-api.greenhouse.io" and "/acme/" in str(req.url):
            return json_response({"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "u", "content": "Acme builds anvils"}]})
        return html_response("", 404)

    info = careers.discover(make_fetcher(handler), "https://acme.com", "Acme")
    assert (info.ats, info.ats_slug, info.ats_confidence, info.supported, info.retry_soon) == ("greenhouse", "acme", "guessed", True, False)


def test_unreachable_homepage_without_board_is_retried_soon():
    info = careers.discover(make_fetcher(lambda r: html_response("", 500)), "https://acme.com", "Acme")
    assert info.retry_soon and not info.supported and "unreachable" in info.notes


def test_unsupported_board_links_to_the_board_not_the_homepage():
    home = '<html><nav><a href="https://ats.rippling.com/en-GB/vouch/jobs">Careers</a></nav></html>'
    handler = lambda req: html_response(home) if req.url.host == "vouch.com" else html_response("", 404)
    info = careers.discover(make_fetcher(handler), "https://vouch.com", "Vouch", probe=False)
    assert (info.ats, info.ats_slug, info.supported) == ("rippling", "vouch", False)       # not the locale "en-GB"
    assert info.careers_url == "https://ats.rippling.com/en-GB/vouch/jobs"


def _site(html_by_host):
    return lambda req: html_response(html_by_host[req.url.host]) if req.url.host in html_by_host else html_response("", 404)


def test_confidence_needs_description_support_and_a_long_enough_name(monkeypatch):
    monkeypatch.setattr(resolve, "_dns_ok", lambda h: True)
    body = "Globex builds secret management software for engineering teams. " + FILLER
    site = _site({"globex.com": page("Globex | secrets management", body), "acme.com": page("Acme", "Acme makes secret management software for engineering teams. " + FILLER)})
    desc = "secret management software for engineering teams"
    assert resolve.resolve_website(make_fetcher(site), "Globex", description=desc).confidence == "verified"
    assert resolve.resolve_website(make_fetcher(site), "Globex", description="event marketing agency") is None       # contradicts the page: a namesake
    assert resolve.resolve_website(make_fetcher(site), "Globex", description="software agency").confidence == "unsure"   # only 1 shared word
    assert resolve.resolve_website(make_fetcher(site), "Globex").confidence == "unsure"
    assert resolve.resolve_website(make_fetcher(site), "Acme", description=desc).confidence == "unsure"     # 4-letter name: never auto-trusted


def test_resolved_url_has_no_default_port_and_first_word_candidates_work(monkeypatch):
    monkeypatch.setattr(resolve, "_dns_ok", lambda h: True)
    assert "upwind.io" in resolve.candidate_domains("Upwind Security")
    assert "upwind.io" not in resolve.candidate_domains("Upwind")[:0] and resolve.candidate_domains("Peak Energy")[0] == "peakenergy.com"
    site = _site({"upwind.io": page("Upwind | Cloud security platform", "Upwind secures cloud workloads. " + FILLER)})
    r = resolve.resolve_website(make_fetcher(site), "Upwind Security", description="cloud security platform")
    assert r and r.url == "https://upwind.io" and ":443" not in r.url


def test_first_word_candidate_still_requires_every_name_word_on_the_page(monkeypatch):
    monkeypatch.setattr(resolve, "_dns_ok", lambda h: True)
    site = _site({"peak.com": page("Peak - games studio", "Peak makes mobile games. " + FILLER)})
    assert resolve.resolve_website(make_fetcher(site), "Peak Energy") is None
