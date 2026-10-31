"""Reports: one self-contained HTML page (search + filters, no server), CSV exports, and a markdown digest."""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import ats as ats_mod
from .experience import Experience
from .pipeline import Pipeline
from .profile import company_score
from .tracker import LABELS, STATUSES, Tracker, app_views, job_code
from .util import days_since, parse_dt


def _group_funding(rows: list[dict]) -> list[dict]:
    """Several articles about the same round -> one entry listing every source."""
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["round"], {"round": r["round"], "amount_text": None, "amount_usd_m": None,
                                           "investors": [], "lead_investors": [], "announced_at": None, "sources": []})
        if g["amount_usd_m"] is None and r["amount_usd_m"]:
            g["amount_usd_m"], g["amount_text"] = r["amount_usd_m"], r["amount_text"]
        elif g["amount_text"] is None and r["amount_text"]:
            g["amount_text"] = r["amount_text"]
        for key in ("investors", "lead_investors"):
            for name in r[key]:
                if name.lower() not in {x.lower() for x in g[key]}:
                    g[key].append(name)
        if r["announced_at"] and (g["announced_at"] is None or r["announced_at"] < g["announced_at"]):
            g["announced_at"] = r["announced_at"]
        g["sources"].append({"name": r["source_name"], "url": r["source_url"], "headline": r["headline"], "date": r["announced_at"]})
    out = sorted(groups.values(), key=lambda g: g["announced_at"] or "", reverse=True)
    for g in out:
        g["sources"].sort(key=lambda s: s["date"] or "")
    return out


def _job_is_new(j: dict, new_days: float) -> bool:
    """New = posted recently (the job board's own date) or, when it gives none, first noticed by a later fetch.
    A role that was already open on our very first look at the company is not new, even though we saw it today."""
    posted = days_since(j["posted_at"])
    if posted is not None:
        return posted <= new_days
    if j.get("baseline"):
        return False
    seen = days_since(j["first_seen"])
    return seen is not None and seen <= new_days


def _company_is_new(funding: list[dict], new_days: float) -> bool:
    """A "new" company is one whose funding was announced in the last few days."""
    ages = [days_since(f["announced_at"]) for f in funding]
    return any(a is not None and a <= new_days for a in ages)


MIN_TREND_DAYS = 3      # a change in open roles is only reported once the history spans at least this many days


def open_change(trend: list[list], window: int = 30) -> dict | None:
    """How the number of open roles moved: {"change": +7, "days": 12} or None when there is not enough history.
    Compares the latest count with the one `window` days earlier (or the oldest we have, if the history is shorter)."""
    if len(trend) < 2:
        return None
    last_day, last = date.fromisoformat(trend[-1][0]), trend[-1][1]
    cutoff = last_day - timedelta(days=window)
    older = [t for t in trend if date.fromisoformat(t[0]) <= cutoff]
    base = older[-1] if older else trend[0]
    span = (last_day - date.fromisoformat(base[0])).days
    if span < MIN_TREND_DAYS:
        return None
    return {"change": last - base[1], "days": span}


def _closed_view(rows: list[dict]) -> list[dict]:
    out = []
    for j in rows:
        seen = None if j.get("baseline") else j["first_seen"]       # a role already open at our first look has no known start
        out.append({"key": j["id"], "code": job_code(j["id"]), "title": j["title"], "location": j["location"] or "", "url": j["url"],
                    "closed_at": j["closed_at"] or j["last_seen"], "opened_at": j["posted_at"] or seen, "relisted": bool(j.get("reopened"))})
    return out


def build_data(p: Pipeline, tracker: bool = False) -> dict:
    new_days = float(p.cfg["general"]["new_days"])
    hc = p.cfg["history"]
    today = datetime.now(timezone.utc).date()
    trend_from = (today - timedelta(days=int(hc["trend_days"]))).isoformat()
    closed_from = (datetime.now(timezone.utc) - timedelta(days=int(hc["show_closed_days"]))).replace(microsecond=0).isoformat()
    companies = []
    for c in p.scoped():
        funding = _group_funding(p.db.funding_for(c["id"]))
        jobs_rows = p.db.jobs_for(c["id"])
        jobs = []
        for j in jobs_rows:
            exp = Experience(min=j["exp_min"], max=j["exp_max"], source=j["exp_source"])
            jobs.append({
                "key": j["id"], "code": job_code(j["id"]), "relisted": bool(j.get("reopened")),
                "title": j["title"], "department": j["department"], "location": j["location"], "remote": j["remote"], "workplace": j["workplace"] or "",
                "type": j["employment_type"], "url": j["url"], "apply_url": j["apply_url"], "posted_at": j["posted_at"],
                "exp_label": exp.label(), "exp_min": j["exp_min"], "exp_max": j["exp_max"], "exp_source": j["exp_source"],
                "exp_mentions": json.loads(j["exp_mentions"] or "[]"), "seniority": j["seniority"], "family": j["role_family"],
                "salary": j["salary_text"], "matches": bool(j["matches_profile"]) and p.profile.active,
                "seen_at": None if j.get("baseline") else j["first_seen"],
                "is_new": _job_is_new(j, new_days),
                "description": j["description"] or "",
            })
        jobs.sort(key=lambda j: (not j["matches"], not j["is_new"], j["title"].lower()))
        matched = sum(1 for j in jobs if j["matches"])
        extra = c.get("extra") or {}
        trend = [list(t) for t in p.db.open_counts(c["id"], trend_from)]
        companies.append({
            "id": c["id"], "name": c["name"], "website": c["website"], "website_confidence": c["website_confidence"],
            "description": c["description"], "country": c["country"], "city": c["city"], "origins": c["origins"],
            "yc": {"batch": extra.get("yc_batch"), "url": extra.get("yc_url"), "team": extra.get("team_size")} if "yc" in c["origins"] else None,
            "careers_url": c["careers_url"], "ats": c["ats"], "ats_confidence": c["ats_confidence"],
            "board_url": ats_mod.board_url(c["ats"], c["ats_slug"]) if c["ats"] in ats_mod.ADAPTERS and c["ats_slug"] else None,
            "jobs_status": c["jobs_status"], "funding": funding, "jobs": jobs,
            "open": len(jobs), "matched": matched, "new_jobs": sum(1 for j in jobs if j["is_new"]),
            "trend": trend, "open_change": open_change(trend), "closed": _closed_view(p.db.closed_jobs(c["id"], closed_from)),
            "is_new": _company_is_new(funding, new_days),
            "score": company_score(p.db.funding_for(c["id"]), len(jobs), matched, is_yc="yc" in c["origins"]),
        })
    companies.sort(key=lambda c: -c["score"])
    data = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "demo": False,
        "profile": {"active": p.profile.active, "max_experience": p.profile.max_experience, "locations": p.profile.locations},
        "companies": companies,
    }
    if tracker:     # your own applications: only ever added for a private page, see write_report()
        tc = p.cfg["tracker"]
        data["tracker"] = {"apps": app_views(Tracker(p.cfg.path("tracker", "path")), p.db), "follow_up_days": float(tc["follow_up_days"]),
                           "statuses": [[k, LABELS[k]] for k in STATUSES]}
    return data


# --------------------------------------------------------------------------- CSV / digest
def write_csv(data: dict, out_dir: Path) -> None:
    with open(out_dir / "companies.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "website", "country", "city", "latest_round", "amount", "announced_at", "investors", "found_via", "found_via_url",
                    "careers_url", "ats", "open_jobs", "matching_jobs", "open_change", "open_change_days", "score"])
        for c in data["companies"]:
            f = c["funding"][0] if c["funding"] else {}
            src = (f.get("sources") or [{}])[0]
            w.writerow([c["name"], c["website"], c["country"], c["city"], f.get("round", ""), f.get("amount_text") or f.get("amount_usd_m") or "",
                        f.get("announced_at", ""), "; ".join(f.get("investors", [])), src.get("name", ""), src.get("url", ""),
                        c["board_url"] or c["careers_url"], c["ats"], c["open"], c["matched"],
                        (c["open_change"] or {}).get("change", ""), (c["open_change"] or {}).get("days", ""), c["score"]])
    with open(out_dir / "jobs.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "title", "location", "remote", "department", "experience", "exp_min", "seniority", "role_family", "salary",
                    "posted_at", "matches_profile", "url"])
        for c in data["companies"]:
            for j in c["jobs"]:
                w.writerow([c["name"], j["title"], j["location"], j["remote"], j["department"], j["exp_label"], j["exp_min"], j["seniority"],
                            j["family"], j["salary"], j["posted_at"], int(j["matches"]), j["url"]])


def write_digest(data: dict, out_dir: Path) -> str:
    lines = [f"# RaiseRadar digest ({data['generated_at'][:16].replace('T', ' ')} UTC)", ""]
    new_c = [c for c in data["companies"] if c["is_new"]]
    lines.append(f"## New companies ({len(new_c)})")
    for c in new_c[:40]:
        f = c["funding"][0] if c["funding"] else {}
        lines.append(f"- **{c['name']}** {f.get('round', '')} {f.get('amount_text') or ''} {('(' + ', '.join(f['investors'][:3]) + ')') if f.get('investors') else ''}"
                     f" - {c['open']} open roles - {c['board_url'] or c['careers_url'] or 'no careers link'}")
    active = data["profile"]["active"]
    new_j = [(c, j) for c in data["companies"] for j in c["jobs"] if j["is_new"] and (j["matches"] or not active)]
    lines += ["", f"## New roles{' matching your profile' if active else ''} ({len(new_j)})"]
    for c, j in new_j[:60]:
        lines.append(f"- {c['name']}: [{j['title']}]({j['url']}) - {j['location'] or 'n/a'} - exp {j['exp_label']}")
    surge = sorted((c for c in data["companies"] if c["open_change"] and c["open_change"]["change"] >= 3), key=lambda c: -c["open_change"]["change"])
    lines += ["", f"## Hiring up ({len(surge)})"]
    for c in surge[:30]:
        lines.append(f"- **{c['name']}**: {c['open_change']['change']:+d} open roles in {c['open_change']['days']} days (now {c['open']})")
    text = "\n".join(lines) + "\n"
    (out_dir / "digest.md").write_text(text, encoding="utf-8")
    return text


def excerpt(text: str, chars: int) -> tuple[str, bool]:
    """First `chars` characters of a job description, cut at a word boundary. Returns (text, was_cut)."""
    text = (text or "").strip()
    if len(text) <= chars:
        return text, False
    if chars <= 0:
        return "", True
    cut = text[:chars]
    if not text[chars].isspace():                       # do not end in the middle of a word
        space = cut.rfind(" ")
        if space > chars // 2:
            cut = cut[:space]
    return cut.rstrip() + " ...", True


def make_public(data: dict, jd_chars: int) -> dict:
    """Prepare a report for a public website: job descriptions are the employers' text, so show an excerpt and link to the posting."""
    data["public"] = {"jd_chars": jd_chars}
    for c in data["companies"]:
        for j in c["jobs"]:
            j["description"], j["excerpt"] = excerpt(j["description"], jd_chars)
    return data


def write_report(p: Pipeline, out_dir: Path | None = None, public: bool | None = None, tracker: bool | None = None) -> dict:
    """Write the report files. `public` (default: [report] public in config.toml) shortens job descriptions and asks search engines not to index the page."""
    rc = p.cfg["report"]
    is_public = bool(public if public is not None else rc["public"])
    embed = bool(p.cfg["tracker"]["embed_in_report"]) if tracker is None else tracker
    data = build_data(p, tracker=embed and not is_public)
    if is_public:
        make_public(data, int(rc["jd_excerpt_chars"]))
    return write_files(data, out_dir or p.cfg.path("general", "report_dir"))


def write_files(data: dict, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = HTML.replace("__DATA__", blob)
    data = {k: v for k, v in data.items() if k != "tracker"}      # your applications go into the page only: not data.json, CSV or the digest
    if data.get("public"):
        html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8"><meta name="robots" content="noindex, nofollow">', 1)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    write_csv(data, out_dir)
    write_digest(data, out_dir)
    return {"report": str(out_dir / "index.html"), "companies": len(data["companies"]),
            "hiring": sum(1 for c in data["companies"] if c["open"]), "jobs": sum(c["open"] for c in data["companies"])}


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RaiseRadar</title>
<style>
:root{--onacc:#fff;--bg:#f6f7f9;--card:#fff;--ink:#16181d;--mut:#667085;--line:#e4e7ec;--acc:#3b5bdb;--acc2:#e7ecff;--ok:#0b7a43;--okbg:#e3f6ec;--warn:#9a5b00;--warnbg:#fff3d6}
@media (prefers-color-scheme:dark){:root{--onacc:#0f1115;--bg:#0f1115;--card:#171a21;--ink:#e8eaee;--mut:#98a2b3;--line:#272b35;--acc:#8aa4ff;--acc2:#222a4a;--ok:#5fd39a;--okbg:#12301f;--warn:#f0b955;--warnbg:#35290f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{padding:20px 16px 8px;max-width:1100px;margin:auto}h1{margin:0;font-size:22px}.sub{color:var(--mut);font-size:13px}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px}
.bar .in{max-width:1100px;margin:auto;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input,select{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit}
input[type=search]{flex:1;min-width:200px}input[type=number]{width:84px}label.chk{display:flex;gap:5px;align-items:center;color:var(--mut)}
main{max-width:1100px;margin:auto;padding:12px 16px 60px;display:grid;gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.top{display:flex;gap:10px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap}
.name{font-size:17px;font-weight:650}.desc{color:var(--mut);margin-top:2px}
.badges{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.b{border-radius:999px;padding:2px 9px;font-size:12px;background:var(--acc2);color:var(--acc);white-space:nowrap}
.b.ok{background:var(--okbg);color:var(--ok)}.b.warn{background:var(--warnbg);color:var(--warn)}.b.mut{background:var(--line);color:var(--mut)}
.meta{margin-top:8px;color:var(--mut);font-size:13px;display:grid;gap:2px}.meta b{color:var(--ink);font-weight:600}
.score{font-variant-numeric:tabular-nums;color:var(--mut);font-size:12px;text-align:right}
details{margin-top:10px}summary{cursor:pointer;font-weight:600;padding:4px 0}
table{width:100%;border-collapse:collapse;margin-top:6px}th,td{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:500;font-size:12px}tr.job{cursor:pointer}tr.job:hover{background:var(--acc2)}
.jd{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:2px 0 6px;max-height:420px;overflow:auto}
.ev{color:var(--mut);font-size:12px;margin:0 0 6px}.empty{color:var(--mut);text-align:center;padding:40px}
.lbl{color:var(--mut);font-size:12px}.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:3px 10px;cursor:pointer;font:inherit;font-size:13px}
.chip.on{background:var(--acc);color:var(--onacc);border-color:var(--acc)}
button.reset{background:none;border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:6px 10px;cursor:pointer;font:inherit}
.bar .in+.in{margin-top:8px}
.demo{margin-top:12px;border:1px solid var(--warn);background:var(--warnbg);color:var(--ink);border-radius:10px;padding:10px 14px;font-size:13px}
.demo code{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:12px;white-space:nowrap}
.spark{display:block;margin:4px 0 0 auto;color:var(--acc)}
select.st{padding:3px 6px;font-size:13px;max-width:140px}input.note{width:100%;max-width:520px;padding:4px 8px;font-size:13px}
.toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:9;background:var(--ink);color:var(--bg);border-radius:10px;padding:9px 14px;display:flex;gap:12px;align-items:center;box-shadow:0 4px 18px #0004}
.toast[hidden]{display:none}.toast button{background:none;color:inherit;border:1px solid currentColor;border-radius:8px;padding:2px 9px;cursor:pointer;font:inherit}
code.cmd{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:12px}
@media(max-width:700px){.hide-s{display:none}.bar{position:static}}
</style></head><body>
<header><h1>RaiseRadar</h1><div class="sub" id="sub"></div>
<div class="demo" id="demo" hidden><b>Sample data.</b> Every company, investor, news item and job on this page is made up, so you can try the filters.
<br><b>To see real companies:</b> run <code>raiseradar setup</code> (paste a free OpenRouter key from openrouter.ai/keys), then <code>raiseradar run</code>.
The first real run takes about <b>30 minutes</b> because free AI models are slow; later runs take a few minutes.
No key? <code>raiseradar run --skip-news</code> still shows real Y Combinator companies and their open roles.</div></header>
<div class="bar">
<div class="in">
<input type="search" id="q" placeholder="Search company, job title, investor, country...">
<select id="round"><option value="">Any round</option></select>
<select id="origin"><option value="">All sources</option><option value="news">Funding news</option><option value="yc">Y Combinator</option><option value="manual">Added by me</option></select>
<select id="country"><option value="">Any HQ country</option></select>
<select id="sort"><option value="score">Best match</option><option value="funding">Newest funding</option><option value="jobs">Most open roles</option><option value="momentum">Hiring up the most</option></select>
</div>
<div class="in"><span class="lbl" style="min-width:64px">Work mode</span><span class="chips" id="wpChips"></span></div>
<div class="in"><span class="lbl" style="min-width:64px">Level</span><span class="chips" id="lvlChips"></span></div>
<div class="in">
<input type="search" id="loc" placeholder="Job location (e.g. india, london)" title="Matches the job location text. Comma-separate several places (any of them)." style="max-width:260px">
<select id="family"><option value="">Any role type</option></select>
<select id="age" title="Uses the date the job board gives; if it gives none, the date this tool first noticed the role"><option value="">Any posting age</option><option value="7">Posted in last 7 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option></select>
<label class="chk">Max exp (yrs) <input type="number" id="maxexp" min="0" max="30" placeholder="any"></label>
<select id="track" title="Roles you marked in the tracker" style="display:none"><option value="">My roles: hide not interested</option><option value="untracked">Not tracked yet</option><option value="saved">Saved</option><option value="progress">Applied / interviewing / offer</option><option value="rejected">Rejected</option><option value="ignored">Not interested</option><option value="all">Everything</option></select>
<label class="chk"><input type="checkbox" id="hiring" checked> Hiring only</label>
<label class="chk"><input type="checkbox" id="mine"> Matches my profile</label>
<label class="chk"><input type="checkbox" id="fresh"> New only</label>
<label class="chk" title="Hide companies whose website was auto-matched without enough evidence"><input type="checkbox" id="hideunsure"> Hide unverified websites</label>
<button class="reset" id="reset" type="button">Reset</button>
</div></div>
<main id="list"></main>
<div class="toast" id="toast" hidden></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const $ = id => document.getElementById(id);
const h = (t, a, ...k) => { const e = document.createElement(t); for (const [x, v] of Object.entries(a || {})) { if (x === 'class') e.className = v; else if (x === 'text') e.textContent = v; else if (v !== null && v !== undefined) e.setAttribute(x, v); } k.flat().forEach(c => c && e.append(c)); return e; };
const ago = iso => { if (!iso) return ''; const d = (Date.now() - Date.parse(iso)) / 864e5; return d < 1 ? 'today' : d < 2 ? 'yesterday' : d < 60 ? Math.floor(d) + 'd ago' : Math.floor(d / 30) + 'mo ago'; };
const badge = (t, c) => h('span', { class: 'b ' + (c || ''), text: t });
const link = (t, u) => u ? h('a', { href: u, target: '_blank', rel: 'noopener noreferrer', text: t }) : h('span', { text: t });

// ---- my applications. Embedded in the page for a private report; refreshed (and made editable) when the page is served by `raiseradar serve`.
const T = { live: false, token: '', list: [], apps: new Map(), days: (D.tracker && D.tracker.follow_up_days) || 14,
  statuses: (D.tracker && D.tracker.statuses) || [['saved', 'Saved'], ['applied', 'Applied'], ['interviewing', 'Interviewing'], ['offer', 'Offer'], ['rejected', 'Rejected'], ['ignored', 'Not interested']] };
const SL = Object.fromEntries(T.statuses), ACTIVE = ['applied', 'interviewing', 'offer'];
const UI = { jd: new Set(), det: new Map() };      // what the visitor has opened, kept across re-renders
const setApps = list => { T.list = list || []; T.apps = new Map(T.list.filter(a => a.key).map(a => [a.key, a])); };
setApps(D.tracker && D.tracker.apps);
const stOf = j => (T.apps.get(j.key) || {}).status || '';
const showTrack = () => !D.public && (T.live || T.list.length > 0);
const followUp = a => a.status === 'applied' && (Date.now() - Date.parse(a.updated_at)) / 864e5 >= T.days;
const stBadge = st => st ? badge(SL[st] || st, st === 'saved' ? '' : ACTIVE.includes(st) ? 'ok' : 'mut') : '';
let toastTimer;
function toast(msg, label, fn) {
  const el = $('toast'); el.replaceChildren(h('span', { text: msg }));
  if (label) { const b = h('button', { type: 'button', text: label }); b.onclick = () => { el.hidden = true; fn(); }; el.append(b); }
  el.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, 7000);
}
async function api(path, body) {
  try {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-RaiseRadar-Token': T.token }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { toast(j.error || 'Could not save (' + r.status + ')'); return false; }
    setApps(j.applications); render(); return true;
  } catch (e) { toast('Could not reach the server: ' + e.message); return false; }
}
const refOf = a => a.key ? { key: a.key } : { id: a.id };
function chooser(value, onpick, none) {
  const s = h('select', { class: 'st' }, h('option', { value: '', text: none }), ...T.statuses.map(([k, l]) => h('option', { value: k, text: l })));
  s.value = value; s.onclick = e => e.stopPropagation(); s.onchange = () => onpick(s.value); return s;
}
function setJob(j, status) {
  const before = stOf(j);
  api(status ? 'api/tracker/set' : 'api/tracker/delete', status ? { key: j.key, status } : { key: j.key }).then(ok => {
    if (ok && status === 'ignored') toast('Hidden: ' + j.title, 'Undo', () => api(before ? 'api/tracker/set' : 'api/tracker/delete', before ? { key: j.key, status: before } : { key: j.key }));
  });
}
async function goLive() {
  if (D.public || D.demo || location.protocol === 'file:') return;
  try {
    const r = await fetch('api/tracker', { cache: 'no-store' }); if (!r.ok) return;
    const j = await r.json();
    T.live = !!j.writable; T.token = j.token || ''; T.days = j.follow_up_days || T.days; setApps(j.applications); sync(); render();
  } catch (e) { }
}
const sync = () => { $('track').style.display = showTrack() ? '' : 'none'; };

const rounds = [...new Set(D.companies.flatMap(c => c.funding.map(f => f.round)))].sort();
rounds.forEach(r => $('round').append(h('option', { value: r, text: r })));
[...new Set(D.companies.flatMap(c => c.jobs.map(j => j.family)))].sort().forEach(f => $('family').append(h('option', { value: f, text: f })));
if (!D.profile.active) $('mine').parentElement.style.display = 'none';
if (D.demo) { $('demo').hidden = false; document.title = 'RaiseRadar (sample data)'; if (D.demo.profile) $('demo').append(h('br'), h('span', { class: 'sub', text: D.demo.profile })); }
const ageDays = j => { const t = Date.parse(j.posted_at || j.seen_at || ''); return isNaN(t) ? null : (Date.now() - t) / 864e5; };
const ageText = j => j.posted_at ? 'posted ' + ago(j.posted_at) : (j.seen_at ? 'noticed ' + ago(j.seen_at) : '');
$('sub').textContent = `${D.companies.length} companies · ${D.companies.reduce((s, c) => s + c.open, 0)} open roles · generated ${ago(D.generated_at)} · ` +
  (D.profile.locations.length ? 'locations: ' + D.profile.locations.join(', ') : 'no location filter');

const WP = [['remote', 'Remote'], ['hybrid', 'Hybrid'], ['onsite', 'On-site'], ['', 'Not stated']];
const LV = [['intern', 'Intern'], ['junior', 'Junior'], ['mid', 'Mid'], ['senior', 'Senior'], ['lead/manager', 'Lead / Manager'], ['principal+', 'Principal+'], ['director', 'Director'], ['exec', 'Exec']];
const sel = { wp: new Set(), lvl: new Set() };
const allJobs = D.companies.flatMap(c => c.jobs);
function makeChips(boxId, defs, key, field) {
  defs.forEach(([v, label]) => {
    const n = allJobs.filter(j => (j[field] || '') === v).length;
    if (!n) return;
    const b = h('button', { class: 'chip', type: 'button', 'data-v': v, text: `${label} ${n}` });
    b.onclick = () => { sel[key].has(v) ? sel[key].delete(v) : sel[key].add(v); b.classList.toggle('on', sel[key].has(v)); save(); render(); };
    $(boxId).append(b);
  });
}
makeChips('wpChips', WP, 'wp', 'workplace');
makeChips('lvlChips', LV, 'lvl', 'seniority');
[...new Set(D.companies.map(c => c.country).filter(Boolean))].sort().forEach(x => $('country').append(h('option', { value: x, text: x })));

const IDS = ['q', 'round', 'origin', 'country', 'sort', 'loc', 'family', 'maxexp', 'age', 'track'], CHK = ['hiring', 'mine', 'fresh', 'hideunsure'];
function save() {
  try { localStorage.setItem('cf-filters', JSON.stringify({ v: Object.fromEntries(IDS.map(i => [i, $(i).value])), c: Object.fromEntries(CHK.map(i => [i, $(i).checked])), wp: [...sel.wp], lvl: [...sel.lvl] })); } catch (e) { }
}
function restore() {
  try {
    const s = JSON.parse(localStorage.getItem('cf-filters') || 'null'); if (!s) return;
    IDS.forEach(i => { if (s.v && s.v[i] !== undefined) $(i).value = s.v[i]; });
    CHK.forEach(i => { if (s.c && s.c[i] !== undefined) $(i).checked = s.c[i]; });
    (s.wp || []).forEach(x => sel.wp.add(x)); (s.lvl || []).forEach(x => sel.lvl.add(x));
    document.querySelectorAll('#wpChips .chip').forEach(b => b.classList.toggle('on', sel.wp.has(b.dataset.v)));
    document.querySelectorAll('#lvlChips .chip').forEach(b => b.classList.toggle('on', sel.lvl.has(b.dataset.v)));
  } catch (e) { }
}
$('reset').onclick = () => {
  IDS.forEach(i => { $(i).value = i === 'sort' ? 'score' : ''; }); CHK.forEach(i => { $(i).checked = i === 'hiring'; });
  sel.wp.clear(); sel.lvl.clear(); document.querySelectorAll('.chip.on').forEach(b => b.classList.remove('on')); save(); render();
};

function jobVisible(j) {
  const me = $('maxexp').value;
  if (me !== '' && j.exp_min !== null && j.exp_min > +me) return false;
  if ($('family').value && j.family !== $('family').value) return false;
  if ($('age').value) { const d = ageDays(j); if (d === null || d > +$('age').value) return false; }
  if ($('mine').checked && !j.matches) return false;
  if ($('fresh').checked && !j.is_new) return false;
  if (showTrack()) {
    const tf = $('track').value, st = stOf(j);
    if (tf === '' && st === 'ignored') return false;
    if (tf === 'untracked' && st) return false;
    if (tf === 'progress' && !ACTIVE.includes(st)) return false;
    if (['saved', 'rejected', 'ignored'].includes(tf) && st !== tf) return false;
  }
  if (sel.wp.size && !sel.wp.has(j.workplace || '')) return false;
  if (sel.lvl.size && !sel.lvl.has(j.seniority)) return false;
  const terms = $('loc').value.split(',').map(t => t.trim().toLowerCase()).filter(Boolean);
  if (terms.length) {
    const hay = ((j.location || '') + ' ' + (j.workplace === 'remote' ? 'remote' : '')).toLowerCase();
    if (!terms.some(t => hay.includes(t))) return false;
  }
  return true;
}
function companyVisible(c, jobs) {
  if ($('round').value && !c.funding.some(f => f.round === $('round').value)) return false;
  if ($('origin').value && !c.origins.includes($('origin').value)) return false;
  if ($('country').value && c.country !== $('country').value) return false;
  if ($('hideunsure').checked && c.website_confidence === 'unsure') return false;
  const filtering = (showTrack() && !['', 'all'].includes($('track').value)) || $('maxexp').value !== '' || $('age').value || $('family').value || $('mine').checked || $('fresh').checked || sel.wp.size || sel.lvl.size || $('loc').value.trim();
  if (($('hiring').checked || filtering) && !jobs.length) return false;
  const q = $('q').value.trim().toLowerCase();
  if (q) {
    const hay = [c.name, c.description, c.country, c.city, ...c.funding.flatMap(f => f.investors), ...jobs.map(j => j.title)].join(' ').toLowerCase();
    if (!q.split(/\s+/).every(w => hay.includes(w))) return false;
  }
  return true;
}
function jobRows(jobs) {
  const rows = [], tk = showTrack();
  jobs.forEach(j => {
    const mine = T.apps.get(j.key);
    const jd = h('tr', { class: 'jd-row', style: UI.jd.has(j.key) ? '' : 'display:none' }, h('td', { colspan: tk ? 7 : 6 },
      h('div', { class: 'ev', text: j.exp_mentions.length ? 'Experience mentions: ' + j.exp_mentions.join('  |  ') : (j.exp_source === 'title' ? 'Experience inferred from the job title' : j.exp_source === 'level' ? 'Experience inferred from the ATS level' : 'No explicit experience requirement found') }),
      h('div', { class: 'jd', text: j.description || '(no description published)' }),
      j.excerpt ? h('div', { class: 'sub' }, 'Excerpt only. Full description: ', link('on the employer page', j.url)) : '',
      h('div', {}, link('Open posting →', j.url), ' ', j.apply_url && j.apply_url !== j.url ? link('Apply →', j.apply_url) : ''),
      T.live && mine ? noteBox(mine) : '',
      !D.public && !D.demo && !T.live ? h('div', { class: 'sub', style: 'margin-top:6px' }, 'Track it from a terminal: ', h('code', { class: 'cmd', text: 'raiseradar track set applied ' + j.code })) : ''));
    const tr = h('tr', { class: 'job' },
      h('td', {}, h('b', { text: j.title }), ' ', j.is_new ? badge('NEW', 'ok') : '', j.matches ? badge('fits you') : '', j.relisted ? badge('listed again', 'mut') : ''),
      h('td', { class: 'hide-s' }, j.location || '', ' ', j.workplace ? badge({ remote: 'Remote', hybrid: 'Hybrid', onsite: 'On-site' }[j.workplace], j.workplace === 'remote' ? 'ok' : 'mut') : ''),
      h('td', { text: j.exp_label }), h('td', { class: 'hide-s', text: j.seniority }),
      h('td', { class: 'hide-s', text: j.salary || '' }),
      h('td', { class: 'hide-s', text: ageText(j) || '-', title: j.posted_at ? 'Date from the job board' : (j.seen_at ? 'The job board gives no date; this is when this tool first noticed the role' : 'The job board gives no date, and the role was already open when this tool first looked') }));
    if (tk) tr.append(h('td', {}, T.live ? chooser(stOf(j), v => setJob(j, v), '- not tracked -') : stBadge(stOf(j))));
    tr.onclick = () => { const open = jd.style.display === 'none'; jd.style.display = open ? '' : 'none'; open ? UI.jd.add(j.key) : UI.jd.delete(j.key); };
    rows.push(tr, jd);
  });
  return rows;
}
function noteBox(a) {
  const i = h('input', { class: 'note', type: 'text', maxlength: 2000, placeholder: 'Note (saved when you leave the box)', value: a.note || '' });
  i.onclick = e => e.stopPropagation();
  i.onchange = () => api('api/tracker/set', { ...refOf(a), note: i.value });
  return h('div', { style: 'margin-top:6px' }, i);
}
function spark(c) {
  const t = c.trend || []; if (t.length < 2) return null;
  const d0 = Date.parse(t[0][0]), span = Date.parse(t[t.length - 1][0]) - d0; if (span <= 0) return null;
  const W = 96, H = 26, vs = t.map(x => x[1]), lo = Math.min(...vs), hi = Math.max(...vs);
  const pt = x => [((Date.parse(x[0]) - d0) / span * (W - 6) + 3).toFixed(1), (hi === lo ? H / 2 : H - 3 - (x[1] - lo) / (hi - lo) * (H - 6)).toFixed(1)];
  const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', 'spark'); svg.setAttribute('width', W); svg.setAttribute('height', H); svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const line = document.createElementNS(ns, 'polyline'); line.setAttribute('points', t.map(x => pt(x).join(',')).join(' '));
  line.setAttribute('fill', 'none'); line.setAttribute('stroke', 'currentColor'); line.setAttribute('stroke-width', '1.6'); svg.append(line);
  const last = pt(t[t.length - 1]), dot = document.createElementNS(ns, 'circle');
  dot.setAttribute('cx', last[0]); dot.setAttribute('cy', last[1]); dot.setAttribute('r', '2.4'); dot.setAttribute('fill', 'currentColor'); svg.append(dot);
  const tip = document.createElementNS(ns, 'title'); tip.textContent = `Open roles: ${t[0][1]} on ${t[0][0]} -> ${t[t.length - 1][1]} on ${t[t.length - 1][0]}`; svg.append(tip);
  return svg;
}
function closedBox(c) {
  if (!c.closed || !c.closed.length) return null;
  const dur = j => { if (!j.opened_at || !j.closed_at) return '-'; const d = Math.round((Date.parse(j.closed_at) - Date.parse(j.opened_at)) / 864e5); return d >= 0 ? d + 'd' : '-'; };
  const d = h('details', {}, h('summary', { text: `No longer listed (${c.closed.length})`, title: 'Roles that disappeared from the company page. That does not mean they were filled: they may be paused or re-posted.' }),
    h('table', {}, h('tr', {}, ...['Role', 'Location', 'Was open', 'Gone'].map((t, i) => h('th', { class: i === 1 ? 'hide-s' : '', text: t }))),
      ...c.closed.map(j => h('tr', {}, h('td', {}, link(j.title, j.url), ' ', showTrack() ? stBadge(stOf(j)) : ''), h('td', { class: 'hide-s', text: j.location }),
        h('td', { text: dur(j), title: 'Time between the posting date (or when this tool first saw it) and when it vanished' }), h('td', { text: ago(j.closed_at) })))));
  d.open = UI.det.get('closed' + c.id) === true; d.addEventListener('toggle', () => UI.det.set('closed' + c.id, d.open));
  return d;
}
function appsPanel() {
  if (!showTrack()) return null;
  const tf = $('track').value, rows = T.list.filter(a => a.status !== 'ignored' || tf === 'ignored' || tf === 'all');
  const order = ['interviewing', 'offer', 'applied', 'saved', 'rejected', 'ignored'];
  rows.sort((a, b) => order.indexOf(a.status) - order.indexOf(b.status) || b.updated_at.localeCompare(a.updated_at));
  const live = T.list.filter(a => ACTIVE.includes(a.status)), fu = live.filter(followUp).length;
  const sum = h('summary', { text: `My applications (${rows.length})` + (live.length ? ` · ${live.length} in progress` : '') + (fu ? ` · ${fu} to follow up` : '') });
  const body = rows.length ? h('table', {}, h('tr', {}, ...['Company', 'Role', 'Status', 'Applied', 'Updated', 'Note', ''].map((t, i) => h('th', { class: i > 2 ? 'hide-s' : '', text: t }))),
    ...rows.map(a => h('tr', {}, h('td', {}, h('b', { text: a.company })), h('td', {}, link(a.title, a.url)),
      h('td', {}, T.live ? chooser(a.status, v => api(v ? 'api/tracker/set' : 'api/tracker/delete', v ? { ...refOf(a), status: v } : refOf(a)), '- remove -') : stBadge(a.status)),
      h('td', { class: 'hide-s', text: a.applied_at ? a.applied_at + ' (' + ago(a.applied_at) + ')' : '' }), h('td', { class: 'hide-s', text: ago(a.updated_at) }),
      h('td', { class: 'hide-s' }, T.live ? noteBox(a).firstChild : a.note || ''),
      h('td', {}, a.listing === 'closed' ? badge('no longer listed', 'warn') : '', followUp(a) ? badge('follow up?', 'warn') : ''))))
    : h('div', { class: 'sub', text: T.live ? 'Nothing yet. Use the menu in the My status column of any role.' : 'Nothing tracked yet. From a terminal: raiseradar track set applied <words from the company or title>' });
  const d = h('details', { class: 'card' }, sum, body);
  d.open = UI.det.has('apps') ? UI.det.get('apps') : (live.length > 0); d.addEventListener('toggle', () => UI.det.set('apps', d.open));
  return d;
}
function card(c, jobs) {
  const f = c.funding[0];
  const badges = h('div', { class: 'badges' });
  c.funding.slice(0, 2).forEach(x => badges.append(badge([x.round, x.amount_text || (x.amount_usd_m ? '$' + x.amount_usd_m + 'M' : ''), ago(x.announced_at)].filter(Boolean).join(' · '))));
  if (c.yc) badges.append(badge('YC ' + (c.yc.batch || ''), 'warn'));
  if (c.is_new) badges.append(badge('NEW company', 'ok'));
  if (c.country) badges.append(badge([c.city, c.country].filter(Boolean).join(', '), 'mut'));
  const oc = c.open_change;
  if (oc && oc.change) badges.append(badge(`${oc.change > 0 ? '+' : '-'}${Math.abs(oc.change)} open role${Math.abs(oc.change) === 1 ? '' : 's'} in ${oc.days}d`, oc.change > 0 ? 'ok' : 'mut'));
  badges.append(c.open ? badge(`${jobs.length}/${c.open} roles shown` + (c.matched ? ` · ${c.matched} fit you` : ''), 'ok') : badge(c.jobs_status ? 'hiring page: ' + c.jobs_status : 'no jobs found', 'mut'));
  const meta = h('div', { class: 'meta' });
  if (f) {
    if (f.investors.length) meta.append(h('div', {}, h('b', { text: 'Investors: ' }), (f.lead_investors.length ? 'led by ' + f.lead_investors.join(', ') + (f.investors.length > f.lead_investors.length ? ' · with ' + f.investors.filter(i => !f.lead_investors.includes(i)).join(', ') : '') : f.investors.join(', '))));
    meta.append(h('div', {}, h('b', { text: 'Found via: ' }), f.sources.slice(0, 4).flatMap((s, i) => [i ? ' · ' : '', link(s.name || 'article', s.url)]), ' ', h('span', { text: '(' + (f.announced_at || '').slice(0, 10) + ')' })));
  }
  const links = [];
  if (c.website) links.push(link(c.website.replace(/^https?:\/\//, ''), c.website), c.website_confidence === 'unsure' ? h('span', { class: 'b warn', title: 'Matched from the company name only, with too little evidence that it is the right company. Check the site, or fix it with: raiseradar add "Name" --website ...', text: '⚠ unverified website' }) : (c.website_confidence === 'verified' ? h('span', { class: 'sub', title: 'Matched from the name and checked against what the news said the company does', text: ' (auto-detected)' }) : ''));
  const hire = c.board_url || c.careers_url;
  if (hire) links.push(' · ', link('Hiring page', hire), c.ats ? h('span', { text: ' (' + c.ats + (c.ats_confidence === 'guessed' ? ', guessed' : '') + ')' }) : '');
  if (c.yc && c.yc.url) links.push(' · ', link('YC profile', c.yc.url));
  meta.append(h('div', {}, links));
  const el = h('div', { class: 'card' },
    h('div', { class: 'top' }, h('div', {}, h('div', { class: 'name', text: c.name }), c.description ? h('div', { class: 'desc', text: c.description }) : ''), h('div', {}, h('div', { class: 'score', text: 'score ' + c.score }), spark(c))),
    badges, meta);
  if (jobs.length) {
    const d = h('details', {}, h('summary', { text: `Open roles (${jobs.length})` }),
      h('table', {}, h('tr', {}, ...[...['Role', 'Location', 'Experience', 'Level', 'Pay', 'Posted'], ...(showTrack() ? ['My status'] : [])].map((t, i) => h('th', { class: i === 1 || (i > 2 && i < 6) ? 'hide-s' : '', text: t }))), ...jobRows(jobs)));
    d.open = UI.det.has(c.id) ? UI.det.get(c.id) : jobs.length <= 6;
    d.addEventListener('toggle', () => UI.det.set(c.id, d.open));
    el.append(d);
  }
  const cb = closedBox(c); if (cb) el.append(cb);
  return el;
}
function render() {
  const sort = $('sort').value;
  let items = D.companies.map(c => ({ c, jobs: c.jobs.filter(jobVisible) })).filter(x => companyVisible(x.c, x.jobs));
  const newest = c => (c.funding[0] && c.funding[0].announced_at) || '';
  if (sort === 'funding') items.sort((a, b) => newest(b.c).localeCompare(newest(a.c)));
  else if (sort === 'jobs') items.sort((a, b) => b.jobs.length - a.jobs.length);
  else if (sort === 'momentum') { const m = c => c.open_change ? c.open_change.change : -1e9; items.sort((a, b) => m(b.c) - m(a.c) || b.c.score - a.c.score); }
  else items.sort((a, b) => b.c.score - a.c.score);
  const list = $('list'); list.replaceChildren();
  const panel = appsPanel(); if (panel) list.append(panel);
  if (!items.length) list.append(h('div', { class: 'empty', text: 'Nothing matches these filters.' }));
  items.slice(0, 300).forEach(x => list.append(card(x.c, x.jobs)));
}
[...IDS, ...CHK].forEach(id => $(id).addEventListener('input', () => { save(); render(); }));
restore();
sync();
render();
goLive();
</script></body></html>
"""
