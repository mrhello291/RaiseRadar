import pytest

from raiseradar.experience import (extract_experience, find_salary, infer_from_title, infer_workplace, level_to_years,
                                      role_family, seniority_of)


@pytest.mark.parametrize("text,mn,mx", [
    ("5+ years of experience building backend services", 5, None),
    ("You have 3-5 years of professional experience with Python", 3, 5),
    ("Minimum of 4 years' experience in sales", 4, None),
    ("At least two years of experience in a similar role", 2, None),
    ("2 to 4 years experience required", 2, 4),
    ("7+ yrs experience in data engineering", 7, None),
    ("Requirements:\n- 6+ years of hands-on experience with Kubernetes\n- 2+ years managing teams", 6, None),
    # real misses found on live JDs
    ("WHAT YOU'LL NEED - 1-2 years of experience in field, event, growth, or ABM marketing", 1, 2),
    ("WHAT YOU'LL NEED - 10+ years in solutions engineering, sales engineering, or a related field", 10, None),
    ("What You'll Need: - 3-6 years in Talent Operations, Recruiting Operations", 3, 6),
    ("- 3+ years of product marketing experience at a high-growth startup", 3, None),
    ("- 5-7+ years in solutions architecture", 5, 7),
    ("You bring 4 years as a backend developer", 4, None),
    ("About the role\nWhat you bring to your team\n- 6+ years of experience building and maintaining software", 6, None),   # "your team" is not "our team"
    ("- 5-8+ years across business operations, strategic finance", 5, 8),
    ("- 2+ years doing investigative or risk analytics", 2, None),
    ("- 3 to 8 years at a top-tier firm in management consulting", 3, 8),
    ("Minimum Qualifications\n- 8+ years in business development, partnerships", 8, None),
])
def test_explicit_years(text, mn, mx):
    e = extract_experience(text)
    assert (e.min, e.max, e.source) == (mn, mx, "jd")
    assert e.mentions


@pytest.mark.parametrize("text", [
    "We were founded 10 years ago and have 40 employees.",
    "Over the last 5 years we grew revenue 10x.",
    "Our team has been building this for 8 years.",
    "4 year vesting schedule with a 1 year cliff",
    "Join us in the next 3 years of growth",
    "Our founders have 20 years in banking between them.",
    "Over 10 years ago we started in a garage.",
    "We've been building payments for 12 years in this market.",
    "Ranked among the top 10% fastest-growing companies two years in a row.",
])
def test_company_blurbs_are_not_requirements(text):
    e = extract_experience(text)
    assert e.min is None


def test_months_to_years_band_is_not_a_minimum():
    e = extract_experience("Fresher (6months–2 year) 📅 Contract Duration: 6 Months")
    assert (e.min, e.source) == (0.0, "entry-phrase")


def test_optional_mentions_lose_to_required():
    text = "5+ years of experience required. 10+ years of experience is a plus."
    assert extract_experience(text).min == 5
    only_pref = "Experience with Rust is a plus (3+ years of experience preferred)."
    e = extract_experience(only_pref)
    assert e.min == 3


@pytest.mark.parametrize("text", [
    "No prior experience required, we will train you.",
    "Entry-level role for fresh graduates",
    "Great for freshers!",
])
def test_entry_phrases(text):
    e = extract_experience(text)
    assert (e.min, e.source) == (0.0, "entry-phrase")


def test_title_fallback():
    assert extract_experience("", "Senior Backend Engineer").min == 5
    assert extract_experience("", "Software Engineering Intern").max == 0
    assert extract_experience("", "Staff Engineer").min == 8
    assert extract_experience("", "Account Executive").min is None
    e = extract_experience("Build things with us", "Junior Developer")
    assert e.source == "title" and e.max == 2


def test_jd_beats_title():
    e = extract_experience("3+ years of experience in React", "Senior Frontend Engineer")
    assert (e.min, e.source) == (3, "jd")


def test_labels():
    assert extract_experience("5+ years of experience").label() == "5+ yrs"
    assert extract_experience("3-5 years of experience").label() == "3-5 yrs"
    assert extract_experience("", "Senior Engineer").label() == "~5+ yrs"
    assert extract_experience("nothing here").label() == "not stated"


def test_seniority_and_family():
    assert seniority_of("Sr. Software Engineer") == "senior"
    assert seniority_of("Engineering Manager") == "lead/manager"
    assert seniority_of("Office Manager") == "mid" and seniority_of("Product Manager") == "mid"
    assert extract_experience("", "Account Manager").min is None and extract_experience("", "Engineering Manager").min == 6
    assert seniority_of("Software Engineer") == "mid"
    assert seniority_of("Head of Growth") == "exec"
    assert role_family("Senior Backend Engineer") == "engineering"
    assert role_family("Machine Learning Engineer") == "data/ml"
    assert role_family("Account Executive") == "sales"
    assert role_family("Product Designer") == "design"
    assert role_family("Customer Success Manager") == "customer"
    assert role_family("Wizard", "Engineering") == "engineering"


def test_salary():
    assert find_salary("Base pay: $120,000 - $160,000 per year plus equity").startswith("$120,000 - $160,000")
    assert find_salary("INR 20 - 30 LPA") != ""
    assert find_salary("competitive salary") == ""


def test_level_mapping():
    assert level_to_years("Mid-Senior Level") == (3.0, 6.0)
    assert level_to_years("Entry level") == (0.0, 2.0)
    assert level_to_years("") is None
    assert infer_from_title("Lead Designer") == (6.0, None)


@pytest.mark.parametrize("location,title,stated,flag,expected", [
    ("New York, NY; Remote (US)", "Engineer", "", None, "remote"),
    ("Remote - Europe", "Engineer", "", None, "remote"),
    ("Berlin", "Engineer (Hybrid)", "", None, "hybrid"),
    ("London (Hybrid)", "Designer", "", None, "hybrid"),
    ("San Francisco - On-site", "Engineer", "", None, "onsite"),
    ("Austin, TX", "Engineer", "", None, ""),
    ("Austin, TX", "Engineer", "Hybrid", None, "hybrid"),            # explicit ATS value wins over text
    ("Remote", "Engineer", "On-site", None, "onsite"),
    ("Austin", "Engineer", "", True, "remote"),
    ("Anywhere in India", "Engineer", "", None, "remote"),
    ("Remoteville, KS", "Engineer", "", None, ""),                      # a town name is not a work mode
])
def test_infer_workplace(location, title, stated, flag, expected):
    assert infer_workplace(location, title, stated, flag) == expected
