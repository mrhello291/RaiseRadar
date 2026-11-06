import json
import smtplib

import httpx
import pytest

from conftest import html_response, make_fetcher
from raiseradar import notify
from raiseradar.config import Config
from raiseradar.contacts import extract_emails
from raiseradar.db import DB
from raiseradar.drafting import FOOTER, validate
from raiseradar.matching import score_job
from raiseradar.me import Me, Project, init_me, load_me
from raiseradar.outreach import Outreach, Store
from raiseradar.tracker import Tracker

SECRET = "s" * 32
ENV = {"OUTREACH_NTFY_TOPIC": "t1", "OUTREACH_NTFY_REPLY_TOPIC": "r1", "OUTREACH_APPROVAL_SECRET": SECRET,
       "OUTREACH_SMTP_USER": "me@gmail.com", "OUTREACH_SMTP_PASSWORD": "app-password"}


def make_me():
    return Me(name="Asha Rao", email="asha@example.com", headline="Backend engineer, 3 years, data pipelines",
              years_experience=3, skills=["python", "postgres", "kubernetes"], roles_wanted=["backend", "platform"],
              links={"github": "https://github.com/asha"},
              projects=[Project("Pipekit", "https://github.com/asha/pipekit", "A Python tool that replays failed Postgres ingestion jobs.", ["python", "postgres"])])


GOOD_BODY = ("Hi Lumenfield team,\n\nI saw you are hiring a Backend Engineer and recently raised a Series B. I built Pipekit "
             "(https://github.com/asha/pipekit), a Python tool that replays failed Postgres ingestion jobs, which looks close to "
             "the data work in the role description you posted. I have three years of experience with Python services and "
             "Postgres in production. Is the role still open, and who is the right person to talk to about it?\n\nThanks,\nAsha Rao")


class FakeLLM:
    def __init__(self, reply=None):
        self.reply, self.calls = reply, 0

    def complete_json(self, system, user, validate=None, **kw):
        self.calls += 1
        r = self.reply or {"subject": "Backend Engineer at Lumenfield", "body": GOOD_BODY}
        if validate and validate(r) is False:
            from raiseradar.llm import LLMUnavailable
            raise LLMUnavailable("rejected")
        return r


class FakeSMTP:
    sent: list = []
    fail_login = False

    def __init__(self, host, port):
        pass

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def login(self, u, p):
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def _reset_smtp():
    FakeSMTP.sent, FakeSMTP.fail_login = [], False


def cfg_for(tmp_path, **o):
    c = Config(root=tmp_path)
    c.data["outreach"].update(enabled=True, min_gap_seconds=0, **o)
    return c


def seed(db: DB, name="Lumenfield", domain="lumenfield.io", confidence="verified", title="Backend Engineer",
         desc="Build Python services on Postgres. Kubernetes a plus."):
    cid, _ = db.upsert_company(name, origin="news")
    db.update_company(cid, website=f"https://{domain}", domain=domain, website_confidence=confidence)
    db.conn.execute("INSERT INTO jobs(id, company_id, title, location, url, description, exp_min, active) VALUES(?,?,?,?,?,?,?,1)",
                    (f"{name}-{title}", cid, title, "Remote", f"https://{domain}/jobs/1", desc, 2))
    db.conn.commit()
    return cid


def site_handler(request):
    if request.url.path in ("/contact", "/careers"):
        return html_response('<a href="mailto:careers@lumenfield.io">Jobs</a> <p>press@lumenfield.io, jane.doe@lumenfield.io, x@other.com</p>')
    return httpx.Response(404)


def make_outreach(tmp_path, db, handler=site_handler, llm=None, ntfy=None, tracker=None, imap=None, **o):
    cfg = cfg_for(tmp_path, gmail_drafts=imap is not None, **o)
    http = httpx.Client(transport=httpx.MockTransport(ntfy or (lambda r: httpx.Response(200, text="{}"))))
    return Outreach(cfg, make_me(), Store(tmp_path / "o.db"), db, fetcher=make_fetcher(handler), llm=llm if llm is not None else FakeLLM(),
                    tracker=tracker, env=ENV, sleep=lambda s: None, smtp_factory=FakeSMTP, http_client=http, imap_factory=imap), cfg


# ------------------------------------------------------------------ contacts, matching, drafting
def test_only_role_addresses_on_the_company_domain():
    html = '<a href="mailto:Careers@lumenfield.io?subject=x">x</a> jane.doe@lumenfield.io noreply@lumenfield.io press@lumenfield.io info@other.com hello@jobs.lumenfield.io'
    assert extract_emails(html, "lumenfield.io") == ["careers@lumenfield.io", "hello@jobs.lumenfield.io"]


def test_matching_needs_wanted_title_and_a_project():
    me = make_me()
    m = score_job(me, "Backend Engineer", "Python and Postgres on Kubernetes")
    assert m and m.project.name == "Pipekit" and m.score >= 0.5
    assert score_job(me, "Account Executive", "Python Postgres") is None
    assert score_job(me, "Backend Engineer", "Python", exp_min=8) is None
    assert score_job(me, "Backend Engineer", "Ruby on Rails only").project is None


def test_resume_link_follows_the_role_and_is_required_in_the_draft():
    from raiseradar.drafting import draft_email
    from raiseradar.matching import Match
    from raiseradar.me import Resume
    me = make_me()
    me.resumes = [Resume("sw", "https://x.example/sw.pdf", ["backend"]), Resume("ml", "https://x.example/ml.pdf", ["machine learning"])]
    assert me.resume_for("Backend Engineer") == "https://x.example/sw.pdf"
    assert me.resume_for("Machine Learning Engineer") == "https://x.example/ml.pdf"
    assert me.resume_for("Designer") == ""
    assert "https://x.example/sw.pdf" in me.allowed_urls()
    match = Match(0.7, project=me.projects[0], project_hits=["python"])
    company, job = {"name": "Lumenfield"}, {"title": "Backend Engineer"}
    # a model draft without the link is rejected, so the template (which includes it) is used
    _s, body, how = draft_email(FakeLLM(), me, company, job, match, "", "https://x.example/sw.pdf")
    assert how == "template" and "Resume: https://x.example/sw.pdf" in body
    # a model draft that includes it is accepted
    good = GOOD_BODY.replace("\n\nThanks,", "\n\nhttps://x.example/sw.pdf\n\nThanks,")
    _s, body, how = draft_email(FakeLLM({"subject": "Backend Engineer at Lumenfield", "body": good}), me, company, job, match, "", "https://x.example/sw.pdf")
    assert how == "model"


def test_website_link_is_required_in_the_draft_when_the_profile_has_one():
    from raiseradar.drafting import draft_email
    from raiseradar.matching import Match
    me = make_me()
    me.links["site"] = "https://asha.example.dev"
    match = Match(0.7, project=me.projects[0], project_hits=["python"])
    company, job = {"name": "Lumenfield"}, {"title": "Backend Engineer"}
    _s, body, how = draft_email(FakeLLM(), me, company, job, match)   # model draft has no site link: template is used
    assert how == "template" and "Website: https://asha.example.dev" in body
    good = GOOD_BODY.replace("\n\nThanks,", "\n\nhttps://asha.example.dev\n\nThanks,")
    assert draft_email(FakeLLM({"subject": "Backend Engineer at Lumenfield", "body": good}), me, company, job, match)[2] == "model"


def test_strong_projects_beat_weak_ones_that_share_generic_words():
    me = make_me()
    me.projects = [Project("Dashboard", "", "Faster BI dashboard.", ["data", "pipeline", "python", "sql", "analytics"], strength=1),
                   Project("RCA agents", "", "Production multi-agent service at Northwind.", ["python", "llm"], strength=3),
                   Project("Pipekit", "", "Replays failed Postgres jobs.", ["postgres"], strength=2)]
    m = score_job(me, "Backend Engineer", "Python, SQL, data pipelines, analytics, LLM features, Postgres")
    assert m.project.name == "RCA agents" and [p.name for p, _ in m.also] == ["Pipekit"]     # the weak one is never used here
    me.projects = me.projects[:1]
    assert score_job(me, "Backend Engineer", "Python data pipelines").project.name == "Dashboard"   # ...unless nothing else fits


def test_prompt_carries_two_projects_and_the_achievements():
    from raiseradar.drafting import build_prompt
    me = make_me()
    me.projects.append(Project("RCA agents", "", "Production multi-agent service at Northwind.", ["python"], strength=3))
    me.achievements = ["1st prize among 111 participants at Hackfest 2025"]
    m = score_job(me, "Backend Engineer", "Python Postgres")
    prompt = build_prompt(me, {"name": "Lumenfield"}, {"title": "Backend Engineer", "description": "Python Postgres"}, m, "")
    assert "1. " in prompt and "2. " in prompt and "RCA agents" in prompt and "Pipekit" in prompt
    assert "1st prize among 111" in prompt


def test_relevant_know_how_and_brief_mentions_reach_the_prompt():
    from raiseradar.drafting import build_prompt, fallback
    from raiseradar.me import Expertise
    me = make_me()
    me.projects += [Project(f"P{i}", "", f"Summary {i}.", ["python"], short=f"short {i}", strength=2) for i in range(3)]
    me.expertise = [Expertise("preference optimisation (DPO-Positive)", "Trained LoRA adapters with DPO-Positive.", ["dpo", "alignment"]),
                    Expertise("diffusion", "Latent diffusion.", ["diffusion"])]
    m = score_job(me, "Backend Engineer", "Python Postgres; experience with DPO and alignment a plus")
    assert [e.area for e, _ in m.expertise] == ["preference optimisation (DPO-Positive)"]      # only what the role asks for
    assert len(m.also) == 3
    prompt = build_prompt(me, {"name": "Lumenfield"}, {"title": "Backend Engineer", "description": ""}, m, "")
    assert "KNOW-HOW" in prompt and "DPO-Positive" in prompt and "Latent diffusion" not in prompt
    assert "(mention briefly at most) short" in prompt                                        # projects 3+ only in passing
    _s, body = fallback(me, {"name": "Lumenfield"}, {"title": "Backend Engineer"}, m, "")
    assert "Hands-on with" not in body                                      # template stays short when a project fits...
    from raiseradar.matching import Match
    _s, body = fallback(me, {"name": "Lumenfield"}, {"title": "Backend Engineer"}, Match(0.3, expertise=m.expertise), "")
    assert "Hands-on with preference optimisation (DPO-Positive)." in body  # ...and uses know-how when none does


def test_internships_lead_and_brief_ones_are_named_without_their_work():
    from raiseradar.drafting import build_prompt, fallback
    from raiseradar.me import Experience
    me = make_me()
    me.identity = "a final-year CS student"
    rca = Project("RCA agents", "", "Production multi-agent service at Northwind.", ["python"], strength=3)
    me.projects.append(rca)
    me.experience = [Experience("Northwind", "SWE Intern", kind="describe", line="built a production multi-agent service",
                                detail="Built a production multi-agent service.", project="RCA agents"),
                     Experience("Shopfront", "Data Analytics Intern", kind="brief", line="a remote internship at Shopfront")]
    m = score_job(me, "Backend Engineer", "Python Postgres")
    _s, body = fallback(me, {"name": "Lumenfield"}, {"title": "Backend Engineer"}, m, "")
    opening = body.split("\n")[2]
    assert opening == ("I'm Asha Rao, a final-year CS student. At Northwind (SWE Intern) I built a production multi-agent service. "
                       "Before that I did a remote internship at Shopfront.")
    assert body.count("RCA agents") == 0 and "Pipekit" in body          # the job's own project is not told twice
    prompt = build_prompt(me, {"name": "Lumenfield"}, {"title": "Backend Engineer", "description": ""}, m, "")
    assert "DESCRIBE: Built a production" in prompt and "MENTION ONLY" in prompt
    assert "does not mention the Shopfront internship" == validate(me, "Lumenfield", "Backend Engineer", "s", GOOD_BODY.replace("Hi Lumenfield team,", "Hi Lumenfield team, Northwind."))
    ok = GOOD_BODY.replace("Hi Lumenfield team,", "Hi Lumenfield team,\n\nI interned at Northwind and remotely at Shopfront.")
    assert validate(me, "Lumenfield", "Backend Engineer", "s", ok) is None


def test_achievements_follow_the_role_and_skip_ones_a_project_already_covers():
    from raiseradar.drafting import rank_achievements
    from raiseradar.me import Achievement
    me = make_me()
    me.achievements = [Achievement("1st prize at Hackfest 2025", ["agents"]), Achievement("Best Model prize at DataCup 2026", ["credit", "tabular"]),
                       Achievement("TA for Deep Learning", [])]
    assert rank_achievements(me, {"title": "ML Engineer", "description": "tabular credit risk"})[0].startswith("Best Model")
    assert rank_achievements(me, {"title": "AI Engineer, Agents", "description": ""})[0].startswith("1st prize")
    finny = Project("LedgerRAG (1st prize, Hackfest 2025)", short="LedgerRAG (1st prize at Hackfest)")
    assert not any("Hackfest" in t for t in rank_achievements(me, {"title": "AI Engineer, Agents"}, [finny]))


def test_a_good_but_too_long_draft_gets_one_rewrite_instead_of_the_template():
    class TwoReplies(FakeLLM):
        def __init__(self, replies):
            super().__init__(); self.replies, self.prompts = list(replies), []
        def complete_json(self, system, user, validate=None, **kw):
            self.prompts.append(user); r = self.replies.pop(0)
            if validate and validate(r) is False:
                from raiseradar.llm import LLMUnavailable
                raise LLMUnavailable("rejected")
            return r
    from raiseradar.drafting import draft_email
    from raiseradar.matching import Match
    me = make_me()
    long_body = GOOD_BODY.replace("Is the role", " ".join(["More detail."] * 90) + " Is the role")
    llm = TwoReplies([{"subject": "Backend Engineer at Lumenfield", "body": long_body}, {"subject": "Backend Engineer at Lumenfield", "body": GOOD_BODY}])
    _s, body, how = draft_email(llm, me, {"name": "Lumenfield"}, {"title": "Backend Engineer"}, Match(0.7, project=me.projects[0]))
    assert how == "model" and len(llm.prompts) == 2 and "WAS THROWN AWAY" in llm.prompts[1]


def test_availability_is_required_in_drafts_and_kept_in_linkedin_notes_when_it_fits():
    from raiseradar.drafting import fallback, linkedin_note
    from raiseradar.matching import Match
    me = make_me()
    me.availability = "I graduate in May 2027 and am open to an internship now that can convert to full-time."
    me.availability_short = "Open to an internship now that converts to full-time."
    assert "full-time" in validate(me, "Lumenfield", "Backend Engineer", "s", GOOD_BODY)
    assert validate(me, "Lumenfield", "Backend Engineer", "s", GOOD_BODY.replace("Is the role", "I am open to a full-time role. Is the role")) is None
    _s, body = fallback(me, {"name": "Lumenfield"}, {"title": "Backend Engineer"}, Match(0.7, project=me.projects[0]), "")
    assert me.availability in body
    note = linkedin_note(me, "Lumenfield", "Backend Engineer", me.projects[0])
    assert "Pipekit" in note and "converts to full-time" in note and len(note) <= 300


def test_filler_phrases_are_rejected():
    me = make_me()
    bad = GOOD_BODY.replace("I saw you are hiring", "I am applying for the role; I saw you are hiring")
    assert "filler" in validate(me, "Lumenfield", "Backend Engineer", "s", bad)
    assert "filler" in validate(me, "Lumenfield", "Backend Engineer", "s", GOOD_BODY.replace("looks close to", "aligns with"))


def test_template_leads_with_the_best_evidence_and_an_achievement():
    from raiseradar.drafting import fallback
    from raiseradar.matching import Match
    me = make_me()
    me.short_intro = "a final-year CS student"
    me.achievements = ["1st prize among 111 participants at Hackfest 2025"]
    _s, body = fallback(me, {"name": "Lumenfield"}, {"title": "Backend Engineer"}, Match(0.7, project=me.projects[0], project_hits=["python"]), "")
    first = body.split("\n")[2]
    assert first.startswith("I'm Asha Rao, a final-year CS student.") and "Pipekit" in first
    assert "1st prize among 111" in body and "I saw you're hiring" not in body


def test_excluded_titles_are_whole_words():
    me = make_me()
    me.exclude_titles = [r"\bsenior\b", r"\bsr\b"]
    assert score_job(me, "Senior Backend Engineer", "Python Postgres") is None
    assert score_job(me, "Backend Engineer, Israel", "Python Postgres") is not None


def test_validation_blocks_invented_links_placeholders_and_unsigned():
    me = make_me()
    assert validate(me, "Lumenfield", "Backend Engineer", "Backend Engineer at Lumenfield", GOOD_BODY) is None
    assert "link" in validate(me, "Lumenfield", "Backend Engineer", "s", GOOD_BODY.replace("https://github.com/asha/pipekit", "https://evil.example/x"))
    assert "placeholder" in validate(me, "Lumenfield", "Backend Engineer", "s", GOOD_BODY.replace("Lumenfield team", "[Founder name]"))
    assert "signed" in validate(me, "Lumenfield", "Backend Engineer", "s", GOOD_BODY.replace("Asha Rao", "A."))


def test_profile_template_roundtrip_and_missing(tmp_path):
    p = tmp_path / "me.toml"
    assert init_me(p) and not init_me(p)
    me = load_me(p)
    assert "name" in me.missing() and any("project" in m for m in me.missing())
    assert make_me().missing() == []


# ------------------------------------------------------------------ the pipeline
def test_plan_drafts_for_verified_company_only(tmp_path, db):
    seed(db)
    seed(db, "Namesake", "namesake.io", confidence="unsure")
    ou, _ = make_outreach(tmp_path, db)
    drafts = ou.plan()
    assert [d["company"] for d in drafts] == ["Lumenfield"]
    d = drafts[0]
    assert d["to_email"] == "careers@lumenfield.io" and d["body"].endswith(FOOTER)
    assert ou.store.get(d["id"])["status"] == "pending"
    assert ou.plan() == []     # one draft per company, ever


def test_incomplete_profile_refuses_to_plan(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db)
    ou.me = Me()
    with pytest.raises(Exception, match="profile"):
        ou.plan()


def test_dry_run_stores_nothing(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db)
    assert len(ou.run(dry_run=True)["drafts"]) == 1
    assert ou.store.list() == []


def test_no_contact_address_means_no_draft(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db, handler=lambda r: httpx.Response(404))
    assert ou.plan() == []
    ou.store.add_contact("lumenfield.io", "founders@lumenfield.io")     # you add one yourself
    assert ou.plan()[0]["to_email"] == "founders@lumenfield.io"


def test_no_email_becomes_a_lead_with_apply_link_and_short_linkedin_note(tmp_path, db):
    cid = seed(db)
    db.conn.execute("UPDATE jobs SET apply_url='https://lumenfield.io/apply/9'"); db.conn.commit()
    ou, _ = make_outreach(tmp_path, db, handler=lambda r: httpx.Response(404))
    assert ou.plan() == [] and len(ou.last_leads) == 1
    lead = ou.store.leads("new")[0]
    assert lead["apply_url"] == "https://lumenfield.io/apply/9" and lead["company"] == "Lumenfield"
    assert 0 < len(lead["linkedin_note"]) <= 300 and "Pipekit" in lead["linkedin_note"]
    assert lead["message"] and "reply \"no\"" not in lead["message"]       # no cold-email opt-out footer on an application text
    ou.plan(); assert len(ou.store.leads()) == 1                            # one lead per company


def test_lead_notification_lists_company_and_link_once(tmp_path, db):
    seed(db)
    seen = []
    ou, _ = make_outreach(tmp_path, db, handler=lambda r: httpx.Response(404),
                          ntfy=lambda r: (seen.append(json.loads(r.content)), httpx.Response(200, text="{}"))[1])
    ou.plan()
    assert ou.notify_leads() == 1 and ou.notify_leads() == 0
    assert seen[0]["topic"] == "t1" and "Lumenfield" in seen[0]["message"] and "https://lumenfield.io/jobs/1" in seen[0]["message"]
    assert "actions" not in seen[0]                                         # a plain list, nothing to approve


def test_lead_is_replaced_by_an_email_once_an_address_is_added(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db, handler=lambda r: httpx.Response(404))
    ou.plan(); assert ou.store.leads("new")
    ou.store.add_contact("lumenfield.io", "founders@lumenfield.io")
    assert len(ou.plan()) == 1 and ou.store.leads("new") == [] and ou.store.leads()[0]["status"] == "emailed"


def test_dry_run_reports_leads_but_stores_none(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db, handler=lambda r: httpx.Response(404))
    res = ou.run(dry_run=True)
    assert len(res["leads"]) == 1 and ou.store.leads() == []


def test_website_lookups_are_capped_per_run(tmp_path, db):
    for i in range(4):
        seed(db, f"Co{i}", f"co{i}.io")
    calls = []
    ou, _ = make_outreach(tmp_path, db, handler=lambda r: (calls.append(r.url.host), httpx.Response(404))[1], max_lookups_per_run=2)
    ou.plan()
    assert len({h for h in calls}) == 2                                      # stopped after 2 companies; the rest wait for the next run
    calls.clear(); ou.plan()
    assert len({h for h in calls}) == 2                                      # next run continues with the others, not the same two


def test_template_used_when_model_fails_and_still_validated(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db, llm=FakeLLM({"subject": "x", "body": "too short"}))
    d = ou.plan()[0]
    assert d["how"] == "template" and "Pipekit" in d["body"]


def test_blocked_domain_gets_no_draft_and_pending_one_is_dropped(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db)
    did = ou.plan()[0]["id"]
    ou.store.block("https://www.lumenfield.io/careers")
    assert ou.store.get(did)["status"] == "skipped"
    assert ou.store.decide(did, "approve", "x") is False


def test_applied_companies_are_left_alone(tmp_path, db):
    seed(db)
    t = Tracker(tmp_path / "apps.db")
    t.add_manual("Lumenfield", "Backend Engineer", status="applied")
    ou, _ = make_outreach(tmp_path, db, tracker=t)
    assert ou.plan() == []


# ------------------------------------------------------------------ approval
def phone_replies(ou, draft, action="approve", sig=None):
    chash = notify.content_hash(draft["to_email"], draft["subject"], draft["body"])
    sig = sig or notify.sign(SECRET, draft["id"], action, chash)
    return json.dumps({"id": "m1", "event": "message", "message": f"{action}:{draft['id']}:{sig}"})


def test_notification_has_two_signed_buttons(tmp_path, db):
    seed(db)
    seen = []
    ou, _ = make_outreach(tmp_path, db, ntfy=lambda r: (seen.append(json.loads(r.content)), httpx.Response(200, text="{}"))[1])
    ou.plan()
    assert ou.notify_pending() == 1 and ou.notify_pending() == 0
    body = seen[0]
    assert body["topic"] == "t1" and [a["label"] for a in body["actions"]] == ["Send", "Skip"]
    assert all(a["url"].endswith("/r1") for a in body["actions"])


def test_tap_approves_and_email_is_sent_once(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db)
    d = ou.plan()[0]
    d = ou.store.get(d["id"])
    ou.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=phone_replies(ou, d))))
    assert ou.collect_decisions()["approved"] == 1
    assert ou.send_approved()["sent"] == 1
    assert len(FakeSMTP.sent) == 1 and FakeSMTP.sent[0]["To"] == "careers@lumenfield.io"
    assert ou.store.get(d["id"])["status"] == "sent"
    assert ou.collect_decisions()["approved"] == 0 and ou.send_approved()["sent"] == 0     # a repeated tap does nothing
    assert len(FakeSMTP.sent) == 1


def test_forged_and_stale_taps_are_ignored(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db)
    d = ou.store.get(ou.plan()[0]["id"])
    ou.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=phone_replies(ou, d, sig="0" * 24))))
    assert ou.collect_decisions() == {"approved": 0, "skipped": 0, "ignored": 1}
    old = phone_replies(ou, d)                                   # a genuine tap on the text as it was...
    ou.store.update(d["id"], body=d["body"] + "\nP.S. changed")   # ...but the draft was edited afterwards
    ou.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=old)))
    ou.store.set_meta("ntfy_cursor", "all")
    assert ou.collect_decisions()["approved"] == 0
    assert ou.send_approved()["sent"] == 0 and FakeSMTP.sent == []


def test_skip_tap(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db)
    d = ou.store.get(ou.plan()[0]["id"])
    ou.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=phone_replies(ou, d, "skip"))))
    assert ou.collect_decisions()["skipped"] == 1
    assert ou.send_approved()["sent"] == 0


# ------------------------------------------------------------------ sending rules
def approve(ou, did):
    assert ou.store.decide(did, "approve", "terminal")


def test_nothing_unapproved_is_ever_sent(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db)
    ou.plan()
    assert ou.send_approved()["sent"] == 0 and FakeSMTP.sent == []


def test_master_switch_off_holds_everything(tmp_path, db):
    seed(db)
    ou, cfg = make_outreach(tmp_path, db)
    approve(ou, ou.plan()[0]["id"])
    ou.o["enabled"] = False
    assert ou.send_approved() == {"sent": 0, "failed": 0, "held": 1} and FakeSMTP.sent == []


def test_daily_cap(tmp_path, db):
    for i in range(3):
        seed(db, f"Co{i}", f"co{i}.io")
    handler = lambda r: html_response('<a href="mailto:careers@' + r.url.host + '">x</a>') if r.url.path == "/careers" else httpx.Response(404)
    ou, _ = make_outreach(tmp_path, db, handler=handler,
                          llm=FakeLLM({"subject": "Backend Engineer at Co", "body": GOOD_BODY.replace("Lumenfield", "Co")}), daily_cap=2, max_drafts_per_run=5)
    for d in ou.plan():
        approve(ou, d["id"])
    res = ou.send_approved()
    assert res["sent"] == 2 and res["held"] == 1 and len(FakeSMTP.sent) == 2
    assert ou.send_approved()["sent"] == 0


def test_bad_login_stops_and_marks_failed(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db)
    did = ou.plan()[0]["id"]
    approve(ou, did)
    FakeSMTP.fail_login = True
    assert ou.send_approved()["failed"] == 1
    d = ou.store.get(did)
    assert d["status"] == "failed" and "app password" in d["error"]


def test_blocking_after_approval_prevents_sending(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db)
    did = ou.plan()[0]["id"]
    approve(ou, did)
    ou.store.block("lumenfield.io")
    assert ou.send_approved()["sent"] == 0 and FakeSMTP.sent == []


def test_sent_is_recorded_in_tracker(tmp_path, db):
    seed(db)
    t = Tracker(tmp_path / "apps.db")
    ou, _ = make_outreach(tmp_path, db, tracker=t)
    approve(ou, ou.plan()[0]["id"])
    ou.send_approved()
    assert t.list()[0]["company"] == "Lumenfield" and "Cold email" in t.list()[0]["note"]


def test_header_injection_is_refused(tmp_path, db):
    from raiseradar import mailer
    with pytest.raises(mailer.MailError):
        mailer.build_message("A", "a@b.co", "a@b.co", "x@y.co", "hi\nBcc: z@z.co", "body")


def test_full_run_collects_before_asking_and_sends_only_approved(tmp_path, db):
    seed(db)
    ou, _ = make_outreach(tmp_path, db)
    first = ou.run()
    assert first["new_drafts"] == 1 and first["notified"] == 1 and first["send"]["sent"] == 0
    approve(ou, ou.store.list()[0]["id"])
    assert ou.run()["send"]["sent"] == 1


# ------------------------------------------------------------------ Gmail drafts, Sent check, richer notification
from datetime import date, datetime as _dt
from email.message import EmailMessage

from raiseradar.gmailbox import MARKER
from raiseradar.outreach import jd_excerpt

DRAFTS, SENT, TRASH = "[Gmail]/Drafts", "[Gmail]/Sent Mail", "[Gmail]/Trash"


class FakeGmail:
    """Shared mailbox state; calling it returns one IMAP connection (imaplib-like)."""
    def __init__(self, fail=False):
        self.folders = {DRAFTS: {}, SENT: {}, TRASH: {}}
        self.next_uid, self.fail = 1, fail

    def put(self, folder, to, body, when=None, marker=None):
        m = EmailMessage()
        m["To"], m["Subject"] = to, "x"
        if marker:
            m[MARKER] = marker
        m.set_content(body)
        self.folders[folder][str(self.next_uid).encode()] = (m.as_bytes(), when or date.today())
        self.next_uid += 1

    def bodies(self, folder):
        from raiseradar.gmailbox import plain_body
        return [plain_body(raw).strip() for raw, _ in self.folders[folder].values()]

    def __call__(self, host, port):
        if self.fail:
            raise OSError("network down")
        return _Conn(self)


class _Conn:
    def __init__(self, g):
        self.g, self.cur = g, None

    def login(self, u, p): return "OK", [b"ok"]
    def logout(self): return "BYE", []
    def expunge(self): return "OK", []

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
                      b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"']

    def select(self, name, readonly=False):
        self.cur = name.strip('"')
        return "OK", [b"1"]

    def append(self, folder, flags, when, raw):
        self.g.folders[folder.strip('"')][str(self.g.next_uid).encode()] = (raw, date.today())
        self.g.next_uid += 1
        return "OK", [b"appended"]

    def uid(self, cmd, *args):
        box = self.g.folders[self.cur]
        if cmd == "SEARCH":
            crit = list(args[1:])
            to = crit[crit.index("TO") + 1].strip('"').lower()
            since = _dt.strptime(crit[crit.index("SINCE") + 1], "%d-%b-%Y").date() if "SINCE" in crit else None
            hits = [u for u, (raw, when) in box.items()
                    if to in raw.decode().lower().split("\nsubject")[0] and (since is None or when >= since)]
            return "OK", [b" ".join(hits)]
        if cmd == "FETCH":
            return "OK", [(b"1 (BODY[] {n})", box[args[0]][0]), b")"]
        if cmd == "MOVE":
            self.g.folders[args[1].strip('"')][args[0]] = box.pop(args[0])
            return "OK", []
        return "NO", []


def gmail_outreach(tmp_path, db, gmail, seen=None, **kw):
    ntfy = (lambda r: (seen.append(json.loads(r.content)), httpx.Response(200, text="{}"))[1]) if seen is not None else None
    return make_outreach(tmp_path, db, imap=gmail, ntfy=ntfy, **kw)[0]


def test_notification_opens_the_job_post_shows_why_and_has_an_edit_button(tmp_path, db):
    seed(db, desc="We sell rockets.\n\nRequirements:\n- 2+ years of Python\n- Postgres in production\n- Kubernetes a plus\n\nPerks: snacks")
    g, seen = FakeGmail(), []
    ou = gmail_outreach(tmp_path, db, g, seen)
    ou.plan()
    assert ou.notify_pending() == 1
    n = seen[0]
    assert n["click"] == "https://lumenfield.io/jobs/1"
    assert [a["label"] for a in n["actions"]] == ["Send", "Edit in Gmail", "Skip"]
    edit = n["actions"][1]
    assert edit["action"] == "view" and "mail.google.com" in edit["url"] and "authuser=me%40gmail.com" in edit["url"]
    msg = n["message"]
    assert "Your skills it asks for: python, postgres" in msg and "Asks for 2+ years (you: 3)" in msg
    assert "--- From the job post ---\nRequirements:" in msg and "We sell rockets" not in msg
    d = ou.store.list()[0]
    assert g.bodies(DRAFTS) == [d["body"].strip()] and d["gmail_marker"].startswith(f"rr-{d['id']}-")
    assert len(msg.encode()) <= 3900


def test_no_edit_button_when_gmail_is_unreachable(tmp_path, db):
    seed(db)
    seen = []
    ou = gmail_outreach(tmp_path, db, FakeGmail(fail=True), seen)
    ou.plan()
    assert ou.notify_pending() == 1 and [a["label"] for a in seen[0]["actions"]] == ["Send", "Skip"]


def test_approved_unedited_draft_is_sent_and_its_gmail_copy_removed(tmp_path, db):
    seed(db)
    g = FakeGmail()
    ou = gmail_outreach(tmp_path, db, g)
    ou.plan(); ou.notify_pending()
    approve(ou, ou.store.list()[0]["id"])
    assert ou.send_approved()["sent"] == 1 and len(FakeSMTP.sent) == 1
    assert g.folders[DRAFTS] == {} and len(g.folders[TRASH]) == 1


def test_sent_from_gmail_is_noticed_and_never_sent_again(tmp_path, db):
    seed(db)
    g, t = FakeGmail(), Tracker(tmp_path / "apps.db")
    ou = gmail_outreach(tmp_path, db, g, tracker=t)
    ou.plan(); ou.notify_pending()
    d = ou.store.list()[0]
    g.folders[DRAFTS].clear()                                           # you edited it and sent it from Gmail
    g.put(SENT, d["to_email"], "my own better version")
    approve(ou, d["id"])                                                # ...and also tapped Send
    assert ou.send_approved()["sent"] == 0 and FakeSMTP.sent == []
    assert ou.store.get(d["id"])["status"] == "sent_by_you"
    assert "from Gmail" in t.list()[0]["note"]


def test_sync_marks_a_pending_draft_you_sent_yourself(tmp_path, db):
    seed(db)
    g = FakeGmail()
    ou = gmail_outreach(tmp_path, db, g)
    ou.plan(); ou.notify_pending()
    d = ou.store.list()[0]
    g.put(SENT, d["to_email"], "sent by hand")
    assert ou.sync_gmail()["sent_by_you"] == 1 and ou.store.get(d["id"])["status"] == "sent_by_you"
    assert ou.run()["send"]["sent"] == 0 and FakeSMTP.sent == []


def test_edited_but_unsent_draft_is_not_sent_by_the_bot(tmp_path, db):
    seed(db)
    g, seen = FakeGmail(), []
    ou = gmail_outreach(tmp_path, db, g, seen)
    ou.plan(); ou.notify_pending()
    d = ou.store.list()[0]
    g.folders[DRAFTS].clear()
    g.put(DRAFTS, d["to_email"], "my edited version, not sent yet")     # Gmail saves an edit as a new draft
    approve(ou, d["id"])
    assert ou.send_approved()["sent"] == 0 and FakeSMTP.sent == []
    assert ou.store.get(d["id"])["status"] == "in_gmail"
    assert "send your edit from Gmail" in seen[-1]["title"]
    ou.sync_gmail()
    assert g.bodies(DRAFTS) == ["my edited version, not sent yet"]      # your edit is never deleted


def test_gmail_down_holds_a_draft_that_has_a_gmail_copy(tmp_path, db):
    seed(db)
    g = FakeGmail()
    ou = gmail_outreach(tmp_path, db, g)
    ou.plan(); ou.notify_pending()
    approve(ou, ou.store.list()[0]["id"])
    g.fail = True
    assert ou.send_approved() == {"sent": 0, "failed": 0, "held": 1} and FakeSMTP.sent == []
    g.fail = False
    assert ou.send_approved()["sent"] == 1


def test_address_you_emailed_before_is_not_cold_emailed(tmp_path, db):
    seed(db)
    g = FakeGmail()
    g.put(SENT, "careers@lumenfield.io", "an old email", when=date(2025, 1, 1))
    ou = gmail_outreach(tmp_path, db, g)
    ou.plan()
    did = ou.store.list()[0]["id"]
    approve(ou, did)
    assert ou.send_approved()["sent"] == 0 and FakeSMTP.sent == []
    assert ou.store.get(did)["status"] == "skipped" and ou.store.get(did)["decided_via"] == "already emailed"


def test_skipped_draft_copy_is_removed_from_gmail(tmp_path, db):
    seed(db)
    g = FakeGmail()
    ou = gmail_outreach(tmp_path, db, g)
    ou.plan(); ou.notify_pending()
    d = ou.store.list()[0]
    ou.store.decide(d["id"], "skip", "phone")
    assert ou.sync_gmail()["cleaned"] == 1 and g.folders[DRAFTS] == {}
    assert ou.sync_gmail()["cleaned"] == 0


def test_jd_excerpt_starts_at_the_requirements():
    jd = "About us: we are great.\n\nWho You Are:\n\n- 5 years of Go\n\n- Kafka"
    assert jd_excerpt(jd).startswith("Who You Are:\n- 5 years of Go")
    assert jd_excerpt("Just a blurb with no headings") == "Just a blurb with no headings"
    assert jd_excerpt("word " * 400).endswith(" ...") and len(jd_excerpt("word " * 400)) <= 905


def test_old_outreach_database_gets_the_new_columns(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE drafts(id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT, company TEXT NOT NULL, domain TEXT NOT NULL UNIQUE, "
              "role_title TEXT NOT NULL, job_url TEXT, to_email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, score REAL, "
              "evidence TEXT, how TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, notified_at TEXT, decided_at TEXT, "
              "decided_via TEXT, sent_at TEXT, message_id TEXT, error TEXT)")
    c.commit(); c.close()
    assert Store(p).list() == [] and Store(p).gmail_copies_to_remove() == []


def test_schedule_writes_a_timer_and_cron_lines(tmp_path):
    from raiseradar import schedule as sch
    assert sch.calendar_every(30) == "*:0/30" and sch.calendar_every(120) == "0/2:00"
    with pytest.raises(ValueError):
        sch.calendar_every(7)
    calls = []
    root = tmp_path / "my repo"
    names = sch.install(root, "/venv/bin/python", 30, False, run=lambda cmd, check: calls.append(cmd), unit_dir=tmp_path / "units")
    assert names == ["raiseradar-outreach.timer"]
    svc = (tmp_path / "units" / "raiseradar-outreach.service").read_text()
    assert f'-c "{root / "config.toml"}" outreach run' in svc
    assert "OnCalendar=*:0/30" in (tmp_path / "units" / "raiseradar-outreach.timer").read_text()
    assert calls[-1] == ["systemctl", "--user", "enable", "--now", "raiseradar-outreach.timer"]
    assert sch.remove(run=lambda cmd, check: None, unit_dir=tmp_path / "units") == ["raiseradar-outreach.timer"]
    plan = sch.jobs(root, "/venv/bin/python", 30, True)
    assert set(plan) == {"raiseradar-outreach", "raiseradar-run", "raiseradar-jobs"}
    assert sch.cron_lines(plan, 30, root)[0].startswith("*/30 * * * *")
