"""Deterministic job enrichment: years of experience, seniority, role family, salary.

No LLM here on purpose: ATS data is structured and these patterns are cheap, instant and testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_YEARS = r"(?:years?|yrs?)"
_RANGE = re.compile(rf"(?<![\d.$£€/-])(\d{{1,2}})\s*\+?\s*(?:-|–|—|to|or)\s*(\d{{1,2}})\s*\+?\s*{_YEARS}\b", re.I)
_SINGLE = re.compile(rf"(?<![\d.$£€/-])(\d{{1,2}})\s*(\+|plus)?\s*{_YEARS}\b", re.I)
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_WORD_RANGE = re.compile(rf"\b({'|'.join(_WORD_NUM)})\s*(?:\+|plus)?\s*{_YEARS}\b", re.I)

# "5+ years of/in/as ..." is a requirement on its own; otherwise experience-ish words must be nearby
_STRONG_AFTER = re.compile(
    r"\s*(?:of|in|as|with|at|across|spanning|doing|specifically|including|working|worked|experience|exp\b|building|developing|designing|managing|leading|selling|relevant|professional|hands|"
    r"industry|full|progressive|proven|demonstrated|prior|previous|practical|commercial|direct|related|similar|combined|total|overall|"
    r"sales|software|engineering|customer|data|product|operations|marketing|design|finance|legal|account)",
    re.I,
)
_POSITIVE = re.compile(
    r"experience|\bexp\b|expertise|background|hands[- ]on|proven|track record|professional|"
    r"working (?:as|with|in)|worked (?:as|with|in)|\bas an? \w+|of (?:building|developing|designing|managing|leading|writing|operating|working)",
    re.I,
)
# disqualifiers look only at the text right around the number (company blurbs: "10 years ago", "our founders have 20 years in ...")
_NEGATIVE = re.compile(
    r"\bago\b|\bold\b|\bsince\b|founded|established|anniversary|vesting|\bcliff\b|warranty|\bin business\b(?=\s*(?:[,.;:!)]|$|for\b|since\b|and\b))|"
    r"we(?:'ve| have) (?:been|spent|worked|built)|history of|collectively|\bour (?:founders?|team|leadership|executives?|ceo)|\bfounders? (?:have|has)|in a row|years running|consecutive",
    re.I,
)
_TIME_WINDOW = re.compile(r"(?:past|last|next|previous|coming|following|within|every|over the)\s+(?:\w+\s+)?$", re.I)
_OPTIONAL = re.compile(r"preferred|nice[- ]to[- ]have|bonus|a plus|is a plus|ideally|desirable|optional|not required|advantage", re.I)
_ENTRY = re.compile(
    r"no (?:prior |previous |relevant |professional )?experience (?:is )?(?:required|needed|necessary)|"
    r"\bfreshers?\b|fresh graduates?|new[- ]?grads?|entry[- ]level|recent graduates?|graduate (?:programme|program|scheme)|"
    r"early[- ]career|0\s*(?:-|–|to)\s*[12]\s*" + _YEARS,
    re.I,
)


@dataclass
class Experience:
    min: float | None = None
    max: float | None = None
    source: str | None = None        # "jd" | "title" | "level" | "entry-phrase" | None
    mentions: list[str] = field(default_factory=list)

    def label(self) -> str:
        if self.min is None:
            return "not stated"
        approx = "~" if self.source in ("title", "level") else ""
        if self.max is not None and self.max != self.min:
            return f"{approx}{self.min:g}-{self.max:g} yrs"
        if self.max is not None:
            return f"{approx}{self.min:g} yrs" if self.min == 0 else f"{approx}{self.min:g} yrs"
        return f"{approx}{self.min:g}+ yrs"


def _snippet(text: str, start: int, end: int, limit: int = 240) -> str:
    """The sentence (or bullet line) that holds the match, shortened at word boundaries if it is very long."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    le = len(text) if le < 0 else le
    prev = text.rfind(". ", ls, start)
    seg_start = prev + 2 if prev >= 0 else ls
    nxt = text.find(". ", end, le)
    seg_end = nxt + 1 if nxt >= 0 else le
    if seg_end - seg_start > limit:                      # long run-on line: keep a window around the match
        cut_s, cut_e = max(seg_start, start - 100), min(seg_end, end + 130)
        piece = text[cut_s:cut_e]
        if cut_s > seg_start and " " in piece:
            piece = "..." + piece.split(" ", 1)[1]
        if cut_e < seg_end and " " in piece:
            piece = piece.rsplit(" ", 1)[0] + "..."
    else:
        piece = text[seg_start:seg_end]
    return re.sub(r"\s+", " ", piece).strip(" -*\u2022\t")


def _qualifies(text: str, start: int, end: int) -> tuple[bool, bool]:
    """(counts_as_requirement, is_optional) for the number mention text[start:end]."""
    before = text[max(0, start - 50):start]
    after = text[end:end + 40]
    if re.search(r"months?\s*(?:-|–|—|to)\s*$", before):      # "6 months - 2 years": the upper end of a band, not a minimum
        return False, False
    if _TIME_WINDOW.search(before[-28:]) or _NEGATIVE.search(before + " " + after[:28]):
        return False, False
    if not (_STRONG_AFTER.match(after) or _POSITIVE.search(text[max(0, start - 70): end + 90])):
        return False, False
    return True, bool(_OPTIONAL.search(text[max(0, start - 60): end + 60]))


def extract_experience(text: str, title: str = "") -> Experience:
    text = text or ""
    mentions: list[tuple[int, float, float | None, bool, str]] = []   # pos, min, max, optional, snippet
    covered: list[tuple[int, int]] = []

    for m in _RANGE.finditer(text):
        lo, hi = int(m.group(1)), int(m.group(2))
        ok, opt = _qualifies(text, m.start(), m.end())
        if ok and 0 <= lo <= hi <= 30:
            mentions.append((m.start(), lo, hi, opt, _snippet(text, m.start(), m.end())))
        covered.append((m.start(), m.end()))
    for m in _SINGLE.finditer(text):
        if any(a <= m.start() < b for a, b in covered):
            continue
        n = int(m.group(1))
        ok, opt = _qualifies(text, m.start(), m.end())
        if ok and 0 <= n <= 30:
            mentions.append((m.start(), n, None, opt, _snippet(text, m.start(), m.end())))
    for m in _WORD_RANGE.finditer(text):
        n = _WORD_NUM[m.group(1).lower()]
        ok, opt = _qualifies(text, m.start(), m.end())
        if ok:
            mentions.append((m.start(), n, None, opt, _snippet(text, m.start(), m.end())))

    mentions.sort(key=lambda x: x[0])
    snippets = [x[4] for x in mentions][:6]
    required = [x for x in mentions if not x[3]]
    chosen = (required or mentions or [None])[0]
    if chosen:
        return Experience(min=float(chosen[1]), max=float(chosen[2]) if chosen[2] is not None else None, source="jd", mentions=snippets)

    if _ENTRY.search(text) or _ENTRY.search(title):
        return Experience(min=0.0, max=1.0, source="entry-phrase", mentions=[])

    t = infer_from_title(title)
    if t:
        return Experience(min=t[0], max=t[1], source="title", mentions=[])
    return Experience()


# "manager" alone is NOT a seniority signal (Product/Account/Office Manager are often mid-level)
# "staff" is a level (Staff Engineer) except in "Member of Technical Staff", "Chief of Staff" or "Staff Accountant"
_STAFF = r"(?<!technical )(?<!of )\bstaff\b(?!\s+(?:accountant|nurse|writer|attorney|auditor|assistant))"
_LEAD = r"\b(lead|team lead|tech lead|engineering manager|manager,? engineering|group manager|general manager|managing)\b"

_TITLE_LEVELS: list[tuple[re.Pattern, tuple[float, float | None]]] = [
    (re.compile(r"\b(intern|internship|trainee|apprentice|working student|werkstudent)\b", re.I), (0.0, 0.0)),
    (re.compile(r"\b(graduate|new grad|entry[- ]level|junior|jr\.?|associate)\b", re.I), (0.0, 2.0)),
    (re.compile(r"\b(vp|vice president|head of|chief|cto|cfo|coo|cmo|director)\b", re.I), (10.0, None)),
    (re.compile(r"\b(distinguished|fellow)\b", re.I), (12.0, None)),
    (re.compile(r"\bprincipal\b", re.I), (10.0, None)),
    (re.compile(_STAFF, re.I), (8.0, None)),
    (re.compile(_LEAD, re.I), (6.0, None)),
    (re.compile(r"\b(senior|sr\.?)\b", re.I), (5.0, None)),
    (re.compile(r"\b(ii|2)\b$", re.I), (2.0, 5.0)),
    (re.compile(r"\b(iii|3)\b$", re.I), (4.0, 8.0)),
]


def infer_from_title(title: str) -> tuple[float, float | None] | None:
    for rx, rng in _TITLE_LEVELS:
        if rx.search(title or ""):
            return rng
    return None


_SENIORITY = [
    ("intern", re.compile(r"\b(intern|internship|trainee|apprentice|working student|werkstudent)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|graduate|new grad|entry[- ]level|associate)\b", re.I)),
    ("exec", re.compile(r"\b(vp|vice president|chief|cto|cfo|coo|cmo|head of)\b", re.I)),
    ("director", re.compile(r"\bdirector\b", re.I)),
    ("principal+", re.compile(r"\b(principal|distinguished|fellow)\b|" + _STAFF, re.I)),
    ("lead/manager", re.compile(_LEAD, re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?)\b", re.I)),
]


def seniority_of(title: str, exp: Experience | None = None) -> str:
    for label, rx in _SENIORITY:
        if rx.search(title or ""):
            return label
    if exp and exp.min is not None:
        if exp.min <= 1:
            return "junior"
        if exp.min >= 5:
            return "senior"
    return "mid"


_ROLE_FAMILIES = [
    ("data/ml", re.compile(r"data (?:scien|engineer|analy)|machine learning|\bml\b|\bai\b|\bllm\b|research (?:scientist|engineer)|analytics|mlops|nlp|computer vision|deep learning", re.I)),
    ("engineering", re.compile(r"engineer|technical staff|developer|\bsde\b|software|backend|back-end|frontend|front-end|full[- ]?stack|devops|\bsre\b|platform|infrastructure|mobile|\bios\b|android|qa\b|quality assurance|security|architect|firmware|embedded|programmer", re.I)),
    ("design", re.compile(r"designer|\bux\b|\bui\b|design (?:lead|manager|systems?)|creative|brand design", re.I)),
    ("product", re.compile(r"product (?:manager|owner|lead|analyst|operations)|\bpm\b|program manager|technical program", re.I)),
    ("sales", re.compile(r"\bsales\b|account (?:executive|manager)|business development|\bbdr\b|\bsdr\b|solutions? (?:engineer|consultant)|revenue|partnerships?", re.I)),
    ("marketing", re.compile(r"marketing|growth|content|seo|community|communications|\bpr\b|brand", re.I)),
    ("customer", re.compile(r"customer (?:success|support|experience)|support (?:engineer|specialist)|implementation|onboarding", re.I)),
    ("people", re.compile(r"recruit|talent|people (?:ops|partner|operations)|\bhr\b|human resources", re.I)),
    ("finance/legal", re.compile(r"financ|account(?:ant|ing)|controller|legal|counsel|compliance|payroll|tax\b", re.I)),
    ("operations", re.compile(r"operations?|ops\b|supply chain|logistics|procurement|office|administrat|strategy|chief of staff", re.I)),
]


def role_family(title: str, department: str = "") -> str:
    for text in (title or "", department or ""):
        for fam, rx in _ROLE_FAMILIES:
            if rx.search(text):
                return fam
    return "other"


_SALARY = re.compile(
    r"(?:[$€£₹]|USD|EUR|GBP|INR|CAD|AUD|SGD)\s?\d[\d,.]*\s?(?:k|K|LPA|lakhs?|lacs?|M)?"
    r"\s*(?:-|–|—|to)\s*(?:[$€£₹]|USD|EUR|GBP|INR|CAD|AUD|SGD)?\s?\d[\d,.]*\s?(?:k|K|LPA|lakhs?|lacs?|M)?"
    r"(?:\s*(?:per|/|a)\s*(?:year|yr|annum|month|hour|hr))?",
    re.I,
)


def find_salary(text: str) -> str:
    m = _SALARY.search(text or "")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else ""


def level_to_years(level: str) -> tuple[float, float | None] | None:
    """Map an ATS-provided experience level label (SmartRecruiters/Workable) to approximate years."""
    l = (level or "").lower()
    if not l:
        return None
    table = [
        (("intern", "student"), (0.0, 0.0)),
        (("entry", "junior", "graduate", "no experience"), (0.0, 2.0)),
        (("associate",), (1.0, 3.0)),
        (("mid", "intermediate"), (3.0, 6.0)),
        (("senior",), (5.0, None)),
        (("director", "executive", "vp"), (10.0, None)),
    ]
    for keys, rng in table:
        if any(k in l for k in keys):
            return rng
    return None


_REMOTE = re.compile(r"\bremote(?:ly)?\b|work from home|\bwfh\b|work from anywhere|\banywhere\b|distributed", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ONSITE = re.compile(r"\bon[- ]?site\b|\bin[- ]office\b|\bin[- ]person\b", re.I)


def infer_workplace(location: str = "", title: str = "", stated: str = "", remote_flag: bool | None = None) -> str:
    """'remote' (a remote option exists) | 'hybrid' | 'onsite' | '' (not stated).

    `stated` is what the ATS says explicitly and always wins; otherwise fall back to words in the location/title."""
    st = (stated or "").lower().replace("-", "").replace(" ", "")
    if st in ("remote", "hybrid", "onsite"):
        return st
    text = f"{location} {title}"
    if _REMOTE.search(text) or remote_flag:
        return "remote"
    if _HYBRID.search(text):
        return "hybrid"
    if _ONSITE.search(text):
        return "onsite"
    return ""
