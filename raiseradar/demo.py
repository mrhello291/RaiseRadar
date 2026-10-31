"""Sample report built from FICTIONAL companies, so anyone can try the tool in seconds, with no API key and no network.

The sample goes through the same code as real data (job enrichment: experience, level, work mode, pay; the profile
filter; the report builder). Only the inputs are invented. Everything is built in memory and nothing in your real
database or your real report folder is touched. Company sites use the reserved ".example" domain and every link
points at example.com, so no sample entry can be mistaken for (or point at) a real company.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ats as ats_mod
from .config import Config
from .db import DB
from .models import RawJob
from .pipeline import Pipeline
from .report import build_data, write_files

PROFILE = {
    "roles": ["engineer", "developer", "data scientist"],
    "exclude_titles": ["manager", "director", "head of", "principal", r"^staff\b"],
    "max_experience_years": 4,
}
PROFILE_NOTE = "The 'fits you' badges use a sample profile: engineering / data roles needing at most 4 years of experience."

PUBLISHERS = ["Sample Tech Daily", "Demo Startup Wire", "Example Ventures Weekly"]

# --------------------------------------------------------------------------- job description building blocks
_INTRO = {
    "eng": "You will design, build and run production systems that our customers depend on every day, and own what you ship from design to on-call.",
    "data": "You will turn messy real-world data into models and metrics that change product decisions, and take them from notebook to production.",
    "product": "You will decide what we build next, working with customers, engineers and designers to turn problems into shipped product.",
    "design": "You will shape how our product looks, feels and behaves, from first sketch to polished detail.",
    "sales": "You will win and grow customers by understanding their problems and showing how we solve them better than anyone else.",
    "ops": "You will keep the company running smoothly and build the systems and habits that let us grow without chaos.",
    "people": "You will help us find, hire and keep the people who build this company.",
}
_DO = {
    "eng": ["Design and ship features end to end, with tests and monitoring", "Review code and help raise the quality bar for the whole team",
            "Debug hard production problems and make the system faster and more reliable", "Work closely with product and design on what to build"],
    "data": ["Build, evaluate and deploy models and data pipelines", "Define metrics and run experiments with product teams",
             "Explain results clearly to people who are not data specialists", "Keep data quality high as the company grows"],
    "product": ["Own the roadmap for your area and say no to the right things", "Talk to customers every week and turn what you learn into specs",
                "Define success metrics and check that releases hit them", "Work with engineering to ship in small, safe steps"],
    "design": ["Own design for a product area from research to release", "Build and extend our design system",
               "Prototype quickly and test ideas with real customers", "Partner with engineers so the shipped result matches the design"],
    "sales": ["Run the full sales cycle from first call to signed contract", "Build a pipeline through outbound work and partners",
              "Feed what you learn from customers back to product", "Hit and beat a quarterly number"],
    "ops": ["Own processes and tools across teams and keep them simple", "Report on the numbers that matter to leadership",
            "Work with every team to remove blockers", "Prepare the company for audits and growth"],
    "people": ["Run hiring end to end for several teams", "Build interview processes that are fair and fast",
               "Keep candidates informed and excited", "Help managers grow their teams"],
}
_PERKS = ("Benefits\n- Equity, because everyone here owns part of the company\n- Health cover and a learning budget\n"
          "- Flexible time off and a home-office stipend\n- Paid parental leave")


def _jd(company: str, blurb: str, title: str, fam: str, exp: str, extra: str, pay: str, where: str) -> str:
    do = "\n".join(f"- {x}" for x in _DO[fam])
    parts = [f"About {company}\n{blurb}", f"The role: {title}\n{_INTRO[fam]} {where}",
             f"What you will do\n{do}", f"What we are looking for\n- {exp}\n- {extra}\n- Clear written communication and good judgement"]
    if pay:
        parts.append(f"Compensation\n{pay}")
    parts.append(_PERKS)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- the sample world
# job tuple: (title, family, department, location, workplace, type, posted_days_ago or None, experience line, extra requirement, pay line, late)
# "late" jobs are added by a second fetch: with no board date they show up as freshly noticed.
E, D, P, G, S, O, H = "eng", "data", "product", "design", "sales", "ops", "people"
FT = "Full-time"

COMPANIES = [
    dict(name="Lumenfield Robotics", slug="lumenfield", ats="greenhouse", country="United States", city="San Francisco", round="Series C",
         amount="$120 million", usd=120, days=2, investors=["Harbor Point Capital", "Northlight Ventures", "Brightwater Partners"], lead=["Harbor Point Capital"],
         desc="Warehouse robots that pick and pack orders", blurb="Lumenfield builds robots that pick and pack orders in warehouses. Founded in 2019, we now run in 40 sites across three continents.",
         jobs=[("Senior Robotics Software Engineer", E, "Engineering", "San Francisco, CA", "hybrid", FT, 1, "5+ years of professional software engineering experience, ideally in C++ or Rust", "Experience with real-time systems or robotics", "Salary range: $175,000 - $220,000 USD", False),
               ("Software Engineer, Fleet Platform", E, "Engineering", "Remote - United States", "remote", FT, 4, "2-4 years of experience building backend services", "Comfortable with Python and cloud infrastructure", "Salary range: $135,000 - $170,000 USD", False),
               ("Machine Learning Engineer, Perception", D, "Machine Learning", "San Francisco, CA", "onsite", FT, 9, "3+ years of experience training and shipping computer vision models", "Strong PyTorch skills", "Salary range: $165,000 - $205,000 USD", False),
               ("New Grad Software Engineer", E, "Engineering", "San Francisco, CA", "onsite", FT, 3, "No prior professional experience required. We welcome new graduates", "A degree in computer science or a related field, or equivalent projects", "Salary range: $120,000 - $145,000 USD", False),
               ("Field Operations Manager", O, "Operations", "Columbus, OH", "onsite", FT, None, "At least 6 years of experience in logistics or warehouse operations", "Willingness to travel up to 30%", "", False),
               ("Mechanical Engineering Intern", E, "Hardware", "San Francisco, CA", "onsite", "Internship", None, "Currently enrolled in a mechanical or robotics engineering program", "Hands-on project experience", "", True)]),
    dict(name="Tessera Health", slug="tessera-health", ats="lever", country="United Kingdom", city="London", round="Series B",
         amount="$45 million", usd=45, days=6, investors=["Marlowe Capital", "Quayside Ventures"], lead=["Marlowe Capital"],
         desc="Software that runs clinical trials faster", blurb="Tessera Health makes software that helps hospitals and drug makers run clinical trials in months instead of years.",
         jobs=[("Backend Engineer", E, "Engineering", "London, UK", "hybrid", FT, 5, "3-5 years of experience with Python or Go", "Experience with regulated or healthcare data is a plus", "Salary: GBP 70,000 - 95,000", False),
               ("Senior Frontend Engineer", E, "Engineering", "Remote - Europe", "remote", FT, 12, "5+ years of experience with TypeScript and React", "An eye for accessible, dense data interfaces", "Salary: GBP 85,000 - 110,000", False),
               ("Data Scientist, Trial Analytics", D, "Data", "London, UK", "hybrid", FT, 0, "2+ years of experience in biostatistics or applied data science", "Familiarity with SQL and R or Python", "Salary: GBP 65,000 - 85,000", False),
               ("Clinical Product Manager", P, "Product", "London, UK", "hybrid", FT, 20, "4+ years of product management experience, ideally in healthtech", "Comfort talking with clinicians", "", False),
               ("Customer Success Associate", S, "Customer Success", "London, UK", "onsite", FT, None, "1-2 years of experience in a customer-facing role", "Calm under pressure", "", False)]),
    dict(name="Parallax Pay", slug="parallax-pay", ats="ashby", country="Singapore", city="Singapore", round="Series B",
         amount="$60 million", usd=60, days=11, investors=["Meridian Growth", "Silkroad Capital", "Lantern Fund"], lead=["Meridian Growth", "Silkroad Capital"],
         desc="Cross-border payments for small businesses", blurb="Parallax Pay moves money across borders for small businesses, cheaply and in minutes. We serve customers in 30 countries.",
         jobs=[("Staff Backend Engineer, Ledger", E, "Engineering", "Singapore", "hybrid", FT, 7, "8+ years of experience building financial or high-volume systems", "Deep knowledge of transactions and consistency", "Salary: SGD 190,000 - 250,000", False),
               ("Backend Engineer, Payments", E, "Engineering", "Singapore", "hybrid", FT, 2, "3+ years of experience with Java or Kotlin", "Experience with payment networks is a plus", "Salary: SGD 110,000 - 150,000", False),
               ("Risk Analyst", D, "Risk", "Singapore", "onsite", FT, 16, "1-3 years of experience in fraud, risk or credit analytics", "Strong SQL", "", False),
               ("Compliance Manager", O, "Compliance", "Remote - APAC", "remote", FT, 30, "Minimum 6 years of experience in financial-services compliance", "Knowledge of MAS regulations", "", False),
               ("Product Designer", G, "Design", "Singapore", "hybrid", FT, 25, "3+ years of product design experience with a strong portfolio", "Experience designing complex financial flows", "", False),
               ("Software Engineer Intern", E, "Engineering", "Singapore", "onsite", "Internship", None, "No experience required. Open to students graduating within a year", "Basic programming skills in any language", "", True)]),
    dict(name="Kestrel Grid", slug="kestrel-grid", ats="greenhouse", country="Germany", city="Berlin", round="Series C",
         amount="$200 million", usd=200, days=15, investors=["Fennel Climate Fund", "Aldwych Capital", "Stahl Ventures", "Northlight Ventures"], lead=["Fennel Climate Fund"],
         desc="Software that balances electricity grids", blurb="Kestrel Grid makes software that helps grid operators balance wind, solar and batteries in real time.",
         jobs=[("Senior Software Engineer, Optimization", E, "Engineering", "Berlin, Germany", "hybrid", FT, 14, "5+ years of experience with numerical or optimization software", "Python and a systems language", "", False),
               ("Data Engineer", D, "Data", "Remote - Germany", "remote", FT, 6, "2-4 years of experience building data pipelines", "Experience with time-series data", "", False),
               ("Site Reliability Engineer", E, "Platform", "Berlin, Germany", "hybrid", FT, 40, "3+ years of experience running services on Kubernetes", "On-call experience", "", False),
               ("Grid Analyst (Working Student)", D, "Analytics", "Berlin, Germany", "onsite", "Part-time", None, "No experience required. We are happy to teach the energy basics", "Enrolled at a university", "", False),
               ("Head of Sales, DACH", S, "Sales", "Berlin, Germany", "hybrid", FT, 22, "10+ years of experience selling to utilities or energy companies", "Fluent German and English", "", False)]),
    dict(name="Mirabel Labs", slug="mirabel-labs", ats="workable", country="India", city="Bengaluru", round="Series B",
         amount="$38 million", usd=38, days=3, investors=["Banyan Capital", "Monsoon Ventures"], lead=["Banyan Capital"],
         desc="Developer tools that speed up code review", blurb="Mirabel Labs builds developer tools that make code review fast and calm. Thousands of teams use us every day.",
         jobs=[("Software Engineer", E, "Engineering", "Bengaluru, India", "hybrid", FT, 1, "1-3 years of experience writing production code", "Solid computer science fundamentals", "", False),
               ("Senior Software Engineer, Backend", E, "Engineering", "Remote - India", "remote", FT, 8, "5-8 years of experience designing distributed systems", "Experience with Postgres at scale", "", False),
               ("Machine Learning Engineer", D, "Machine Learning", "Bengaluru, India", "hybrid", FT, 2, "2+ years of experience with language models in production", "Experience evaluating model quality", "", False),
               ("Technical Content Marketer", S, "Marketing", "Remote - India", "remote", FT, 18, "3+ years of experience writing and speaking for developers", "Comfortable reading code", "", False),
               ("Associate Software Engineer (Graduates)", E, "Engineering", "Bengaluru, India", "onsite", FT, 4, "Fresh graduates welcome. No professional experience needed", "Strong problem-solving ability", "", False),
               ("Engineering Manager", E, "Engineering", "Bengaluru, India", "hybrid", FT, 35, "7+ years of experience, including 2+ years managing engineers", "Track record of growing teams", "", False)]),
    dict(name="Foxglove Security", slug="foxglove-security", ats="lever", country="Israel", city="Tel Aviv", round="Series B",
         amount="$52 million", usd=52, days=9, investors=["Cedar Hill Partners", "Dunmore Capital"], lead=["Cedar Hill Partners"],
         desc="Cloud security that finds real attack paths", blurb="Foxglove Security shows security teams the attack paths that matter in their cloud, instead of thousands of alerts.",
         jobs=[("Security Researcher", E, "Research", "Tel Aviv, Israel", "onsite", FT, 10, "3+ years of experience in vulnerability research or red teaming", "Knowledge of cloud provider internals", "", False),
               ("Backend Engineer (Go)", E, "Engineering", "Tel Aviv, Israel", "hybrid", FT, 13, "4+ years of experience with Go and distributed systems", "Experience with graph databases is a plus", "", False),
               ("Solutions Engineer", S, "Sales", "Remote - Europe", "remote", FT, 21, "3-5 years of experience in a technical pre-sales role", "Strong presentation skills", "", False),
               ("Senior Product Manager", P, "Product", "Tel Aviv, Israel", "hybrid", FT, 28, "6+ years of product experience in security or infrastructure", "Ability to work with deeply technical users", "", False)]),
    dict(name="Quillstack", slug="quillstack", ats="ashby", country="Canada", city="Toronto", round="Series B",
         amount="$30 million", usd=30, days=19, investors=["Maple Row Capital", "Ironbark Ventures"], lead=["Maple Row Capital"],
         desc="AI assistant for contract review", blurb="Quillstack helps legal teams review contracts in minutes. Our assistant reads, flags risks and drafts replies that lawyers can trust.",
         jobs=[("Full-Stack Engineer", E, "Engineering", "Toronto, Canada", "hybrid", FT, 3, "2+ years of experience with TypeScript and Python", "Interest in language models", "Salary range: CAD 120,000 - 150,000", False),
               ("AI Engineer", D, "Machine Learning", "Remote - Canada", "remote", FT, 0, "3+ years of experience building products on language models", "Experience with retrieval and evaluation", "Salary range: CAD 150,000 - 185,000", False),
               ("Legal Content Specialist", O, "Operations", "Toronto, Canada", "hybrid", FT, 15, "Law degree and 2+ years of experience practising or in legal operations", "Attention to detail", "", False),
               ("Account Executive", S, "Sales", "Toronto, Canada", "hybrid", FT, 26, "4+ years of experience selling software to law firms or legal departments", "A record of hitting quota", "", False)]),
    dict(name="Verdant Freight", slug="verdant-freight", ats="smartrecruiters", country="Netherlands", city="Amsterdam", round="Series C",
         amount="EUR 85 million", usd=92, days=27, investors=["Polder Capital", "Greenline Partners", "Aldwych Capital"], lead=["Polder Capital"],
         desc="Electric trucking and freight marketplace", blurb="Verdant Freight runs electric trucks and a freight marketplace, helping shippers cut emissions without paying more.",
         jobs=[("Software Engineer, Marketplace", E, "Engineering", "Amsterdam, Netherlands", "hybrid", FT, 11, "3+ years of experience building web services", "Experience with routing or pricing problems is a plus", "", False),
               ("Operations Analyst", O, "Operations", "Rotterdam, Netherlands", "onsite", FT, 17, "1-2 years of experience in logistics or consulting", "Excellent spreadsheet skills", "", False),
               ("Fleet Data Scientist", D, "Data", "Remote - Europe", "remote", FT, 23, "3+ years of experience with forecasting and optimization", "Python and SQL", "", False),
               ("Talent Partner", H, "People", "Amsterdam, Netherlands", "hybrid", FT, 31, "3+ years of experience in technical recruiting", "Experience building hiring processes from scratch", "", False)]),
    dict(name="Nimbus Orchard", slug="nimbus-orchard", ats="recruitee", country="Brazil", city="Sao Paulo", round="Series B",
         amount="$41 million", usd=41, days=33, investors=["Cerrado Capital", "Tropic Ventures"], lead=["Cerrado Capital"],
         desc="Satellite and weather data for farmers", blurb="Nimbus Orchard turns satellite images and weather data into simple advice for farmers, covering 12 million hectares.",
         jobs=[("Data Engineer", D, "Data", "Sao Paulo, Brazil", "hybrid", FT, 29, "3-5 years of experience with Python and cloud data tools", "Experience with geospatial data is a plus", "", False),
               ("Backend Developer", E, "Engineering", "Remote - Brazil", "remote", FT, 37, "2+ years of experience with Node.js or Python", "Portuguese and English", "", False),
               ("Agronomist, Customer Success", S, "Customer Success", "Ribeirao Preto, Brazil", "onsite", FT, 44, "Degree in agronomy and 2+ years of field experience", "Willingness to travel", "", False)]),
    dict(name="Halcyon Bio Systems", slug="halcyon-bio", ats="greenhouse", country="United States", city="Boston", round="Series C",
         amount="$150 million", usd=150, days=44, investors=["Charles River Growth", "Helix Partners", "Northlight Ventures"], lead=["Charles River Growth", "Helix Partners"],
         desc="Lab automation for drug discovery", blurb="Halcyon Bio Systems builds automated labs that run thousands of experiments a day, so scientists can test ideas faster.",
         jobs=[("Software Engineer, Lab Automation", E, "Engineering", "Boston, MA", "onsite", FT, 24, "2-5 years of experience writing software for instruments or automation", "Python, plus any lab or hardware exposure", "Salary range: $130,000 - $165,000 USD", False),
               ("Computational Biologist", D, "Science", "Boston, MA", "hybrid", FT, 38, "PhD or 3+ years of industry experience in computational biology", "Experience with large biological datasets", "Salary range: $140,000 - $180,000 USD", False),
               ("Principal Scientist, Assay Development", O, "Science", "Boston, MA", "onsite", FT, 52, "10+ years of experience developing biochemical assays", "Experience leading a team of scientists", "", False)]),
    dict(name="Ember Commerce", slug="ember-commerce", ats="lever", country="Australia", city="Sydney", round="Series B",
         amount="$28 million", usd=28, days=51, investors=["Harbourside Capital"], lead=["Harbourside Capital"], website_confidence="unsure",
         desc="Checkout and returns for online stores", blurb="Ember Commerce gives online stores a faster checkout and easier returns, which lifts sales and lowers costs.",
         jobs=[("Software Engineer, Checkout", E, "Engineering", "Sydney, Australia", "hybrid", FT, 19, "2-4 years of experience with web applications", "Experience with payments is a plus", "", False),
               ("Growth Marketing Manager", S, "Marketing", "Remote - Australia", "remote", FT, 47, "4+ years of experience in B2B or ecommerce growth", "Strong analytical skills", "", False)]),
    dict(name="Tidewater AI", slug="tidewater-ai", ats="ashby", country="United States", city="New York", round="Series B",
         amount="$70 million", usd=70, days=1, investors=["Gotham Ventures", "Brightwater Partners", "Ironbark Ventures"], lead=["Gotham Ventures"],
         desc="Voice agents for customer support", blurb="Tidewater AI builds voice agents that answer customer calls, so support teams can focus on the hard cases.",
         jobs=[("Member of Technical Staff, Voice", D, "Research", "New York, NY", "hybrid", FT, 0, "3+ years of experience in speech or language modelling", "Published work or open-source contributions are a plus", "Salary range: $200,000 - $280,000 USD", False),
               ("Software Engineer, Infrastructure", E, "Engineering", "Remote - US", "remote", FT, 1, "4+ years of experience with low-latency services", "Experience with WebRTC is a plus", "Salary range: $170,000 - $230,000 USD", False),
               ("Forward Deployed Engineer", E, "Engineering", "New York, NY", "onsite", FT, 6, "2+ years of experience in a customer-facing engineering role", "Strong Python", "Salary range: $150,000 - $200,000 USD", False),
               ("Product Designer", G, "Design", "New York, NY", "hybrid", FT, 10, "3-6 years of product design experience", "A portfolio with shipped work", "", False),
               ("Enterprise Account Executive", S, "Sales", "New York, NY", "hybrid", FT, None, "Minimum of 7 years of experience in enterprise software sales", "A record of closing six-figure deals", "", True)]),
    dict(name="Pebblewood", slug="pebblewood", ats="ashby", country="United States", city="Austin", round=None, yc=("Summer 2022", 42),
         desc="Scheduling software for home-service businesses", blurb="Pebblewood is scheduling and invoicing software for plumbers, electricians and other home-service businesses.",
         jobs=[("Founding Engineer", E, "Engineering", "Austin, TX", "hybrid", FT, 15, "3+ years of experience as a generalist engineer", "Ready to own large parts of the product", "Salary range: $140,000 - $180,000 USD", False),
               ("Customer Support Specialist", S, "Support", "Remote - US", "remote", FT, 33, "No experience required. We train you on the product", "Patience and clear writing", "", False)]),
    dict(name="Sable Mobility", slug="sable-mobility", ats="workday", country="France", city="Paris", round="Series C",
         amount="EUR 110 million", usd=119, days=8, investors=["Seine Capital", "Aldwych Capital"], lead=["Seine Capital"],
         desc="Shared electric scooters and bikes", blurb="Sable Mobility runs shared electric scooters and bikes in 60 cities.",
         jobs=[], careers="https://example.com/careers/sable-mobility", jobs_status="uses workday (not supported yet)"),
    dict(name="Orbit Ledger", slug="orbit-ledger", ats=None, country="Switzerland", city="Zurich", round="Series B",
         amount="$34 million", usd=34, days=13, investors=["Alpine Growth"], lead=["Alpine Growth"],
         desc="Accounting automation for mid-sized firms", blurb="Orbit Ledger automates month-end accounting for mid-sized firms.",
         jobs=[], jobs_status="no careers page found"),
]


# Roles that were open at the start and have closed since (so the sample shows "No longer listed"), and roles that appeared
# a few days in (so a company's open-roles line goes up). Same tuple layout as the jobs above.
GONE = {
    "lumenfield": [("Warehouse Controls Engineer", E, "Engineering", "San Francisco, CA", "onsite", FT, 38, "3+ years of experience with PLCs or motion control", "Comfortable on a factory floor", "", False)],
    "tessera-health": [("Platform Engineer", E, "Engineering", "London, UK", "hybrid", FT, 41, "3+ years of experience running Kubernetes in production", "Experience with regulated environments", "", False),
                       ("Clinical Data Analyst", D, "Data", "London, UK", "hybrid", FT, 30, "2+ years of experience with SQL and clinical datasets", "Knowledge of healthcare data standards", "", False)],
    "parallax-pay": [("Backend Engineer, Payments", E, "Engineering", "Singapore", "hybrid", FT, 25, "3+ years of experience building payment systems", "Experience with card networks is a plus", "", False)],
}
RAMP = {
    "quillstack": [("Machine Learning Engineer", D, "Engineering", "Toronto, Canada", "hybrid", FT, 6, "2+ years of experience training and serving models", "Interest in language models", "", False),
                   ("Backend Engineer, Platform", E, "Engineering", "Remote - Canada", "remote", FT, 5, "3+ years of experience with Python or Go", "Comfortable owning a service end to end", "", False),
                   ("Developer Relations Engineer", E, "Engineering", "Toronto, Canada", "hybrid", FT, 5, "2+ years of experience writing technical content or sample code", "Enjoys explaining things", "", False)],
    "ember-commerce": [("Software Engineer, Search", E, "Engineering", "Sydney, Australia", "hybrid", FT, 6, "2-4 years of experience with search or ranking", "Experience with Elasticsearch or similar", "", False),
                       ("Data Engineer", D, "Data", "Sydney, Australia", "hybrid", FT, 5, "2-4 years of experience with data pipelines", "Python and SQL", "", False)],
}
# Tracked sample applications: (company slug, job position within that company's open roles, status, days since applied, days since last news, note)
SAMPLE_APPS = [
    ("lumenfield", 1, "interviewing", 10, 2, "Phone screen done. On-site loop next week."),
    ("tessera-health", 0, "applied", 22, 22, "Applied through the careers page."),
    ("kestrel-grid", 0, "saved", None, 1, "Look at this after the weekend."),
    ("mirabel-labs", 0, "ignored", None, 3, ""),
]
SAMPLE_CLOSED_APP = ("parallax-pay", "applied", 31, 31, "No reply yet.")        # applied to a role that has since closed


def _days_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).replace(microsecond=0).isoformat()


def _raw(c: dict, spec: tuple, idx: int) -> RawJob:
    title, fam, dept, loc, wp, etype, posted, exp, extra, pay, _late = spec
    where = {"remote": "This role is fully remote.", "hybrid": "This role is hybrid, with a few days a week in the office.",
             "onsite": "This role is based in our office."}[wp]
    return RawJob(
        ext_id=f"{c['slug']}-{idx}", title=title, url=f"https://example.com/careers/{c['slug']}/{idx}", apply_url=f"https://example.com/careers/{c['slug']}/{idx}/apply",
        location=loc, workplace=wp, department=dept, employment_type=etype, posted_at=None if posted is None else _days_ago(posted),
        description=_jd(c["name"], c["blurb"], title, fam, exp, extra, pay, where),
    )


def build_demo_db() -> tuple[Pipeline, DB]:
    """Seed an in-memory database with the sample companies, using the real enrichment code."""
    cfg = Config()
    cfg.data["profile"] = dict(PROFILE)
    db = DB(":memory:")
    p = Pipeline(cfg, db, fetcher=None, llm=None)   # no fetcher: the sample never touches the network
    first_fetch = _days_ago(12)
    now = _days_ago(0)
    for c in COMPANIES:
        fields = dict(description=c["desc"], country=c["country"], city=c["city"], website=f"https://{c['slug']}.example",
                      domain=f"{c['slug']}.example", website_confidence="yc" if c.get("yc") else c.get("website_confidence", "verified"))
        extra = {"yc_batch": c["yc"][0], "yc_url": f"https://example.com/yc/{c['slug']}", "team_size": c["yc"][1]} if c.get("yc") else None
        cid, _ = db.upsert_company(c["name"], origin="yc" if c.get("yc") else "news", extra=extra, **fields)
        if c.get("round"):
            for i, pub in enumerate(PUBLISHERS[: 1 + (len(c["investors"]) > 1) + (len(c["investors"]) > 2)]):
                db.add_funding(cid, round=c["round"], amount_usd_m=c["usd"], amount_text=c["amount"], investors=c["investors"], lead_investors=c["lead"],
                               announced_at=_days_ago(c["days"] + i * 0.4), source_name=pub, source_url=f"https://example.com/news/{c['slug']}-{i}",
                               headline=f"{c['name']} raises {c['amount']} {c['round']} to grow")
        ats = c.get("ats")
        if ats in ats_mod.ADAPTERS:
            db.update_company(cid, ats=ats, ats_slug=c["slug"], ats_confidence="linked", careers_url=f"https://example.com/careers/{c['slug']}", careers_checked_at=now)
        elif ats:
            db.update_company(cid, ats=ats, careers_url=c.get("careers"), careers_checked_at=now, jobs_status=c.get("jobs_status"))
        else:
            db.update_company(cid, careers_checked_at=now, jobs_status=c.get("jobs_status"))
        if ats in ats_mod.ADAPTERS:
            # Five fetches over twelve days, like five real runs: the first is the starting snapshot, roles in GONE disappear
            # (missing twice in a row before they count as closed), roles in RAMP appear, and the last fetch brings the "late" roles.
            specs = list(enumerate(c["jobs"]))
            base = [(i, sp) for i, sp in specs if not sp[-1]]
            gone = [(100 + i, g) for i, g in enumerate(GONE.get(c["slug"], []))]
            ramp = [(200 + i, r) for i, r in enumerate(RAMP.get(c["slug"], []))]

            def fetch(items):
                return [p.enrich_job(_raw(c, sp, i), ats, c["slug"]) for i, sp in items]
            db.sync_jobs(cid, fetch(base + gone), first_fetch, first_fetch=True)
            db.sync_jobs(cid, fetch(base + gone), _days_ago(8))
            db.sync_jobs(cid, fetch(base + ramp), _days_ago(5))
            db.sync_jobs(cid, fetch(base + ramp), _days_ago(3))
            db.sync_jobs(cid, fetch(specs + ramp), now)
            db.update_company(cid, jobs_fetched_at=now, jobs_status="ok")
    return p, db


def build_demo_data() -> dict:
    p, _db = build_demo_db()
    data = build_data(p)
    by_name = {c["name"]: c for c in COMPANIES}
    for c in data["companies"]:
        spec = by_name[c["name"]]
        if c["board_url"]:      # never link a sample company to a real job board
            c["board_url"] = f"https://example.com/careers/{spec['slug']}"
    data["demo"] = {"profile": PROFILE_NOTE}
    data["tracker"] = _sample_tracker(data)
    return data


def _sample_tracker(data: dict) -> dict:
    """A few made-up applications, so the sample shows the tracker. Read-only: the sample page never saves anything."""
    from .tracker import LABELS, STATUSES, job_code
    apps, by_slug = [], {}
    for c in COMPANIES:
        by_slug[c["slug"]] = next(x for x in data["companies"] if x["name"] == c["name"])

    def entry(i, comp, job, status, applied_days, news_days, note, listing):
        applied = None if applied_days is None else _days_ago(applied_days)[:10]
        events = [{"status": status, "at": _days_ago(news_days), "note": note}]
        if applied and status != "applied":
            events.insert(0, {"status": "applied", "at": _days_ago(applied_days), "note": ""})
        return {"id": i, "key": job["key"], "code": job_code(job["key"]), "company": comp["name"], "title": job["title"], "location": job["location"] or "",
                "url": job["url"], "status": status, "applied_at": applied, "note": note, "created_at": _days_ago(applied_days or news_days),
                "updated_at": _days_ago(news_days), "events": events, "listing": listing}
    for n, (slug, pos, status, applied_days, news_days, note) in enumerate(SAMPLE_APPS, 1):
        comp = by_slug[slug]
        apps.append(entry(n, comp, comp["jobs"][pos], status, applied_days, news_days, note, "open"))
    slug, status, applied_days, news_days, note = SAMPLE_CLOSED_APP
    comp = by_slug[slug]
    apps.append(entry(len(apps) + 1, comp, comp["closed"][0], status, applied_days, news_days, note, "closed"))
    return {"apps": apps, "follow_up_days": 14.0, "statuses": [[k, LABELS[k]] for k in STATUSES]}


def write_demo(out_dir: str | Path) -> dict:
    """Write the sample report (index.html, data.json, CSVs, digest.md) into out_dir and return a short summary."""
    data = build_demo_data()
    info = write_files(data, Path(out_dir))
    info["demo"] = True
    return info
