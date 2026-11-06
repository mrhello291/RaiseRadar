"""Everything the outreach bot is allowed to say about you, in one hand-editable file (`data/me.toml`).

The file is private: it lives in `data/` (git-ignored) and is never written into reports, CSV, JSON or the digest.
The drafting step is only given what is in here, so anything you want mentioned must be written here first.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATE = '''# About me: the only facts the outreach bot may use when it writes to a founder.
# Keep it true and specific. Edit freely; `raiseradar me check` tells you what is still missing.

name = ""                      # how you sign emails, e.g. "Asha Rao"
email = ""                     # reply address (usually the Gmail account you send from)
headline = ""                  # one line, e.g. "Backend engineer, 3 years, distributed systems and data pipelines"
location = ""                  # e.g. "Bengaluru, open to remote"
years_experience = 0           # used to skip roles that ask for much more than you have (0 = do not filter)
resume_url = ""                # optional link; the bot never attaches files
about = """
Two or three honest sentences about what you build and what you want next.
"""

# Words that describe your skills. They are matched against role titles and descriptions.
skills = ["python", "postgres", "kubernetes"]
# Role titles you want (regular expressions, case-insensitive). The bot only writes about roles that match one.
roles_wanted = ["backend", "platform", "data engineer"]
# Titles to never write about (regular expressions), e.g. seniority you do not match yet.
exclude_titles = ["senior", "staff", "principal", "director", "manager", "head of"]

# Optional: one public resume link per kind of role. The email links the first one whose `roles` match the job title.
# [[resumes]]
# name = "Backend / software"
# url = "https://example.com/resume-software.pdf"
# roles = ["backend", "software", "platform", "full.?stack"]

[links]
github = ""
site = ""
linkedin = ""

# Your portfolio. The bot picks the project that best fits the role and may only describe it with these words.
[[projects]]
name = "Example project"
url = "https://example.com"
summary = "One plain sentence: what it does and one concrete fact (a number, a technology, who uses it)."
tags = ["python", "postgres"]
strength = 2   # 3 = flagship, 2 = solid, 1 = only used when nothing stronger fits the role

# Prizes, rankings, roles: most impressive first, worded so their weight is clear (who ran it, how many competed).
# The email cites the one or two most relevant to the role (by tags).
# [[achievements]]
# text = "1st prize among 111 participants at Some Hackathon 2025, run by Some Company"
# tags = ["agents", "llm"]

# Internships and jobs. "describe" ones are told with their result; "brief" ones are only named, never described.
# [[experience]]
# company = "Acme"
# title = "Software Engineering Intern"
# kind = "describe"
# line = "built ... (a first-person clause that follows 'At Acme I ...')"
# required = true

# Techniques and research areas you have used hands-on. The email may name the ones the role needs, in one line.
# [[expertise]]
# area = "preference optimisation (DPO, DPO-Positive)"
# detail = "Trained LoRA adapters with DPO-Positive on easy-to-hard synthetic negatives."
# tags = ["dpo", "rlhf", "alignment", "post-training"]
'''


@dataclass
class Project:
    name: str
    url: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    short: str = ""    # how it reads after "built ..." in a 300-character note, e.g. "a faster dashboard at a retail startup"
    strength: int = 2  # 3 = flagship, 2 = solid, 1 = only mention when nothing stronger fits the role
    pitch: str = ""    # optional: the one sentence to use when there is room for only one (else the summary's first)


@dataclass
class Achievement:
    """A prize, ranking or role, worded so its weight is clear (who ran it, how many competed). `tags` say which roles
    it is most relevant to; untagged ones are used by their order."""
    text: str
    tags: list[str] = field(default_factory=list)


def achievement_text(a) -> str:
    return getattr(a, "text", a)


@dataclass
class Experience:
    """An internship or job. `describe` entries are told with their result; `brief` entries are only named
    (company, role, remote or not) and what was done there is never described. `required` entries must appear in
    every email."""
    company: str
    title: str = ""
    when: str = ""
    where: str = ""        # e.g. "on-site, Bengaluru" or "remote, for a San Francisco Bay Area startup"
    kind: str = "describe" # "describe" or "brief"
    line: str = ""         # describe: a first-person clause after "At <company> I ..."; brief: a noun phrase, e.g. "a remote internship at X"
    detail: str = ""       # describe: the fuller facts the drafter may use
    required: bool = True
    project: str = ""      # name of the [[projects]] entry that is this job's work, so it is not told twice


@dataclass
class Expertise:
    """Something you have done hands-on (a technique, a research area), stated concretely, with the words a job post
    would use for it. The email may name the relevant ones in one short line."""
    area: str          # e.g. "preference optimisation (DPO, DPO-Positive, curriculum negatives)"
    detail: str = ""   # one or two sentences of evidence: what you did with it, which papers you built on
    tags: list[str] = field(default_factory=list)


@dataclass
class Resume:
    """One public resume link and the role titles it is the right one to send for (regexes)."""
    name: str
    url: str
    roles: list[str] = field(default_factory=list)


@dataclass
class Me:
    name: str = ""
    email: str = ""
    headline: str = ""
    location: str = ""
    years_experience: float = 0
    resume_url: str = ""
    short_intro: str = ""      # under ~110 characters, used in the 300-character LinkedIn note
    about: str = ""
    skills: list[str] = field(default_factory=list)
    roles_wanted: list[str] = field(default_factory=list)
    achievements: list[str] = field(default_factory=list)   # most impressive first; the drafter may cite one relevant one
    exclude_titles: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    projects: list[Project] = field(default_factory=list)
    resumes: list[Resume] = field(default_factory=list)
    expertise: list[Expertise] = field(default_factory=list)
    experience: list[Experience] = field(default_factory=list)
    identity: str = ""     # who you are without the jobs, e.g. "a final-year CS student at IIT Mandi"
    availability: str = ""        # what you are open to, e.g. an internship now that can convert to full-time
    availability_short: str = ""  # the same in a few words, for 300-character LinkedIn notes

    def resume_for(self, title: str) -> str:
        """The public resume link that fits this role title: the first [[resumes]] entry whose `roles` match,
        else `resume_url`, else ''."""
        for r in self.resumes:
            for p in r.roles:
                try:
                    hit = re.search(p, title or "", re.I)
                except re.error:
                    hit = p.lower() in (title or "").lower()
                if hit and r.url:
                    return r.url
        return self.resume_url

    def excluded(self, title: str) -> bool:
        for p in self.exclude_titles:
            try:
                if re.search(p, title or "", re.I):
                    return True
            except re.error:
                if p.lower() in (title or "").lower():
                    return True
        return False

    def missing(self) -> list[str]:
        """Plain-language list of what must be filled in before outreach makes sense."""
        out = []
        if not self.name.strip():
            out.append("name")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.email.strip()):
            out.append("email")
        if not self.headline.strip():
            out.append("headline")
        if not self.skills:
            out.append("skills")
        real = [p for p in self.projects if p.name.strip() and p.summary.strip() and p.name != "Example project"]
        if not real:
            out.append("at least one real project (the template's example does not count)")
        if not self.roles_wanted:
            out.append("roles_wanted")
        return out

    def role_patterns(self) -> list[re.Pattern]:
        out = []
        for p in self.roles_wanted:
            try:
                out.append(re.compile(p, re.I))
            except re.error:
                out.append(re.compile(re.escape(p), re.I))
        return out

    def allowed_urls(self) -> set[str]:
        urls = {u for u in [self.resume_url, *(r.url for r in self.resumes), *self.links.values(), *(p.url for p in self.projects)] if u}
        return {u.rstrip("/").lower() for u in urls}

    def facts(self) -> str:
        """The text handed to the drafting model: everything it may use, nothing else."""
        lines = [f"Name: {self.name}", f"Headline: {self.headline}"]
        if self.location:
            lines.append(f"Location: {self.location}")
        if self.about.strip():
            lines.append("About: " + " ".join(self.about.split()))
        lines.append("Skills: " + ", ".join(self.skills))
        for k, v in self.links.items():
            if v:
                lines.append(f"{k}: {v}")
        return "\n".join(lines)


def load_me(path: str | Path) -> Me:
    p = Path(path)
    if not p.is_file():
        return Me()
    with open(p, "rb") as fh:
        d = tomllib.load(fh)
    projects = [Project(name=str(x.get("name", "")), url=str(x.get("url", "")), short=str(x.get("short", "")), summary=str(x.get("summary", "")),
                        tags=[str(t) for t in x.get("tags", [])], strength=int(x.get("strength", 2)), pitch=str(x.get("pitch", "")))
                for x in d.get("projects", []) if isinstance(x, dict)]
    return Me(
        name=str(d.get("name", "")), email=str(d.get("email", "")), headline=str(d.get("headline", "")),
        location=str(d.get("location", "")), years_experience=float(d.get("years_experience") or 0),
        resume_url=str(d.get("resume_url", "")), short_intro=str(d.get("short_intro", "")), about=str(d.get("about", "")),
        skills=[str(s) for s in d.get("skills", [])], roles_wanted=[str(r) for r in d.get("roles_wanted", [])],
        achievements=[Achievement(str(a["text"]), [str(t) for t in a.get("tags", [])]) if isinstance(a, dict) else Achievement(str(a))
                      for a in d.get("achievements", []) if (isinstance(a, dict) and a.get("text")) or isinstance(a, str)],
        exclude_titles=[str(r) for r in d.get("exclude_titles", [])],
        links={str(k): str(v) for k, v in (d.get("links") or {}).items()}, projects=projects,
        identity=str(d.get("identity", "")),
        availability=str(d.get("availability", "")), availability_short=str(d.get("availability_short", "")),
        experience=[Experience(company=str(x["company"]), title=str(x.get("title", "")), when=str(x.get("when", "")),
                               where=str(x.get("where", "")), kind=str(x.get("kind", "describe")), line=str(x.get("line", "")),
                               detail=str(x.get("detail", "")), required=bool(x.get("required", True)), project=str(x.get("project", "")))
                    for x in d.get("experience", []) if isinstance(x, dict) and x.get("company")],
        expertise=[Expertise(area=str(x.get("area", "")), detail=str(x.get("detail", "")), tags=[str(t) for t in x.get("tags", [])])
                   for x in d.get("expertise", []) if isinstance(x, dict) and x.get("area")],
        resumes=[Resume(name=str(x.get("name", "")), url=str(x.get("url", "")), roles=[str(r) for r in x.get("roles", [])])
                 for x in d.get("resumes", []) if isinstance(x, dict)],
    )


def init_me(path: str | Path) -> bool:
    """Write the template. Never overwrites an existing file. Returns True if it wrote one."""
    p = Path(path)
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(TEMPLATE, encoding="utf-8")
    p.chmod(0o600)
    return True
