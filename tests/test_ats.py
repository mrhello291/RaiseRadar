import html as htmllib
import json

import pytest

from companyfinder import ats
from companyfinder.ats import ashby, greenhouse, jsonld, lever, recruitee, smartrecruiters, workable
from companyfinder.util import strip_html
from conftest import json_response, make_fetcher


def test_strip_html_handles_greenhouse_double_escaping_and_lists():
    raw = htmllib.escape("<h2>About</h2><p>We build things.</p><ul><li>5+ years of experience</li><li>Python</li></ul>")
    text = strip_html(raw)
    assert "About" in text and "- 5+ years of experience" in text and "- Python" in text and "<" not in text


def test_greenhouse_parse():
    data = {"jobs": [{"id": 1, "title": "Backend Engineer", "absolute_url": "https://x/1", "location": {"name": "Remote - India"},
                      "first_published": "2026-10-01T10:00:00-04:00", "updated_at": "2026-10-02T10:00:00-04:00",
                      "departments": [{"name": "Engineering"}], "content": htmllib.escape("<p>3+ years of experience</p>"),
                      "pay_input_ranges": [{"min_cents": 10000000, "max_cents": 15000000, "currency_type": "USD"}]}]}
    [j] = greenhouse.parse(data)
    assert (j.ext_id, j.title, j.remote, j.department) == ("1", "Backend Engineer", True, "Engineering")
    assert j.posted_at == "2026-10-01T14:00:00+00:00" and "3+ years" in j.description and "100,000 - 150,000" in j.salary_text


def test_lever_parse():
    data = [{"id": "abc", "text": "Data Scientist", "hostedUrl": "https://jobs.lever.co/x/abc", "applyUrl": "https://jobs.lever.co/x/abc/apply",
             "categories": {"location": "Berlin", "department": "Data", "commitment": "Full-time"}, "workplaceType": "hybrid",
             "descriptionPlain": "Intro text", "lists": [{"text": "Requirements", "content": "<li>4 years of experience</li>"}],
             "additionalPlain": "Benefits", "createdAt": 1790000000000,
             "salaryRange": {"currency": "EUR", "interval": "per-year-salary", "min": 70000, "max": 90000}}]
    [j] = lever.parse(data)
    assert j.remote is False and j.employment_type == "Full-time" and j.department == "Data"
    assert "Requirements" in j.description and "4 years of experience" in j.description and "Benefits" in j.description
    assert "70,000 - 90,000" in j.salary_text and j.posted_at.startswith("2026-")


def test_ashby_parse_skips_unlisted_and_merges_locations():
    data = {"jobs": [
        {"id": "1", "title": "Designer", "location": "NYC", "secondaryLocations": [{"location": "Remote (US)"}], "isRemote": True,
         "jobUrl": "https://jobs.ashbyhq.com/x/1", "descriptionPlain": "Design things", "department": "Design",
         "compensation": {"compensationTierSummary": "$100K - $140K"}, "publishedAt": "2026-10-01T00:00:00.000+00:00"},
        {"id": "2", "title": "Hidden", "isListed": False}]}
    jobs = ashby.parse(data)
    assert len(jobs) == 1 and jobs[0].location == "NYC; Remote (US)" and jobs[0].remote is True and jobs[0].salary_text == "$100K - $140K"


def test_recruitee_and_workable_parse():
    [r] = recruitee.parse({"offers": [{"id": 5, "title": "QA", "careers_url": "https://x.recruitee.com/o/qa", "description": "<p>desc</p>",
                                       "requirements": "<p>2 years of experience</p>", "city": "Paris", "country": "France", "status": "published"}]})
    assert r.location == "Paris, France" and "2 years" in r.description
    [w] = workable.parse({"jobs": [{"shortcode": "AB12", "title": "Sales", "url": "https://apply.workable.com/x/j/AB12", "city": "Rome",
                                    "country": "Italy", "telecommuting": True, "description": "<p>hi</p>", "experience": "Mid-Senior level"}]}, "x")
    assert w.remote is True and w.experience_hint == "Mid-Senior level" and w.location == "Rome, Italy"


def test_smartrecruiters_fetch_paginates_and_adds_detail():
    def handler(req):
        url = str(req.url)
        if "/postings/p1" in url:
            return json_response({"postingUrl": "https://jobs.smartrecruiters.com/x/p1-eng", "jobAd": {"sections": {
                "jobDescription": {"title": "Job Description", "text": "<p>Build</p>"}, "qualifications": {"title": "Qualifications", "text": "<p>5+ years of experience</p>"}}}})
        if "offset=0" in url:
            return json_response({"totalFound": 2, "content": [{"id": "p1", "name": "Engineer", "location": {"fullLocation": "Warsaw, PL", "remote": True},
                                                                  "experienceLevel": {"label": "Mid-Senior Level"}}]})
        return json_response({"totalFound": 2, "content": [{"id": "p2", "name": "Analyst", "location": {}}]})
    # first page only returns 1 of 2 so the adapter must continue at offset=100 ... emulate with limit override
    jobs = smartrecruiters.fetch(make_fetcher(handler), "x", 10)
    assert jobs is not None and jobs[0].title == "Engineer" and "5+ years" in jobs[0].description
    assert jobs[0].experience_hint == "Mid-Senior Level" and jobs[0].remote is True


def test_smartrecruiters_unknown_company_returns_none():
    f = make_fetcher(lambda r: json_response({"totalFound": 0, "content": []}))
    assert smartrecruiters.fetch(f, "nobody") is None


@pytest.mark.parametrize("module,url,payload", [
    (greenhouse, "boards-api.greenhouse.io", {"jobs": []}),
    (lever, "api.lever.co", []),
    (ashby, "api.ashbyhq.com", {"jobs": []}),
])
def test_adapters_distinguish_missing_board_from_empty_board(module, url, payload):
    assert module.fetch(make_fetcher(lambda r: json_response(payload)), "x") == []
    assert module.fetch(make_fetcher(lambda r: json_response({"error": "nf"}, 404)), "x") is None


def test_jsonld_parses_jobposting_variants():
    page = """<html><script type="application/ld+json">{"@context":"https://schema.org","@graph":[
      {"@type":"Organization","name":"Acme"},
      {"@type":"JobPosting","title":"Support Engineer","description":"&lt;p&gt;2+ years of experience&lt;/p&gt;","datePosted":"2026-10-20",
       "employmentType":["FULL_TIME"],"jobLocation":{"address":{"addressLocality":"Austin","addressRegion":"TX","addressCountry":"US"}},
       "baseSalary":{"currency":"USD","value":{"minValue":90000,"maxValue":110000,"unitText":"YEAR"}},"identifier":{"value":"J-7"}}]}</script>
      <script type="application/ld+json">not json</script></html>"""
    [j] = jsonld.parse_html(page, "https://acme.com/careers")
    assert j.title == "Support Engineer" and j.ext_id == "J-7" and j.location == "Austin, TX, US"
    assert "2+ years" in j.description and "90000 - 110000" in j.salary_text


@pytest.mark.parametrize("snippet,expected", [
    ('<a href="https://boards.greenhouse.io/acme">Jobs</a>', ("greenhouse", "acme")),
    ('<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme2"></iframe>', ("greenhouse", "acme2")),
    ('<a href="https://job-boards.greenhouse.io/foo/jobs/123">', ("greenhouse", "foo")),
    ('<a href="https://jobs.lever.co/bar">', ("lever", "bar")),
    ('<script src="https://jobs.ashbyhq.com/baz/embed"></script>', ("ashby", "baz")),
    ('<a href="https://apply.workable.com/qux/">', ("workable", "qux")),
    ('<a href="https://careers.smartrecruiters.com/Quux">', ("smartrecruiters", "Quux")),
    ('<a href="https://zed.recruitee.com/o/job">', ("recruitee", "zed")),
])
def test_detect_ats_supported(snippet, expected):
    hits = ats.detect_ats(snippet)
    assert (hits[0][0], hits[0][1]) == expected and hits[0][2] is True


def test_detect_ats_ignores_noise_and_flags_unsupported():
    assert ats.detect_ats('<a href="https://www.recruitee.com/pricing">x</a><a href="https://api.recruitee.com/x">') == []
    assert ats.detect_ats('<a href="https://boards.greenhouse.io/embed/job_board/js?for=real">')[0][1] == "real"
    hits = ats.detect_ats('<a href="https://acme.wd5.myworkdayjobs.com/en-US/careers">')
    assert hits == [("workday", "acme", False)]
    mixed = ats.detect_ats('<a href="https://acme.bamboohr.com/careers">b</a><a href="https://jobs.lever.co/acme">l</a>')
    assert mixed[0][0] == "lever" and mixed[1][0] == "bamboohr"
