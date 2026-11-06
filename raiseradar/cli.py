"""Command line interface:  python -m raiseradar <command>"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import webbrowser
from pathlib import Path

from .config import Config, load_config
from .db import DB
from .http import Fetcher
from .llm import LLMClient, LLMError, list_free_models
from .onboarding import COLD_RUN, ask, interactive, setup_key
from .outreach import STATUSES as OUTREACH_STATUSES
from .pipeline import Pipeline
from .report import write_report
from .util import norm_name, now_iso


def build(cfg: Config, need_llm: bool = True) -> tuple[Pipeline, DB, Fetcher]:
    db = DB(cfg.path("general", "db_path"))
    h = cfg["http"]
    fetcher = Fetcher(cfg.user_agent, timeout=h["timeout"], min_delay=h["min_delay_per_host"], respect_robots=h["respect_robots"])
    llm = None
    if need_llm and cfg.openrouter_key:
        lc = cfg["llm"]
        llm = LLMClient(cfg.openrouter_key, lc["models"], cache=db, max_calls=lc["max_calls_per_run"], requests_per_minute=lc["requests_per_minute"],
                        max_tokens=lc["max_tokens"], reasoning_effort=lc["reasoning_effort"],
                        debug_log=cfg.path("general", "db_path").parent / "llm_failures.jsonl")
    return Pipeline(cfg, db, fetcher, llm), db, fetcher


def cmd_run(args, cfg):
    p, db, f = build(cfg)
    if p.llm is None and not args.skip_news:
        print("Note: no OPENROUTER_API_KEY, so funding news will be skipped (Y Combinator companies and ones you `add` still work).\n"
              "      Run `raiseradar setup` to add a free key.\n")
    run_id = db.start_run()
    t0 = time.time()
    try:
        stats = p.run_all(news=not args.skip_news, yc=not args.skip_yc, limit=args.limit)
    except KeyboardInterrupt:      # everything finished so far is saved; show it and let the next run continue
        print("\nInterrupted. Building the report from what is saved so far; the next `run` continues where this one stopped.")
        stats = {"interrupted": True}
    stats.update(write_report(p, public=True if getattr(args, "public", False) else None, tracker=getattr(args, "tracker", None)))
    stats["seconds"] = round(time.time() - t0)
    if p.llm:
        stats["llm_calls"], stats["llm_cache_hits"], stats["llm_models"] = p.llm.calls, p.llm.cache_hits, p.llm.model_used
        stats["llm_partial_replies"] = p.llm.partial_replies
    db.finish_run(run_id, stats)
    print(json.dumps(stats, indent=2))
    print(f"\nOpen: {stats['report']}")
    _open_if_asked(args, stats["report"])


def _open(path: Path) -> None:
    try:
        webbrowser.open(path.resolve().as_uri())
    except Exception:
        pass


def _open_if_asked(args, report) -> None:
    """--open on run/report: open the page, but only from a terminal, so cron and containers never try to start a browser."""
    if not getattr(args, "open", False) or not report:
        return
    if not interactive():
        print("(--open ignored: not running in a terminal.)")
        return
    _open(Path(report))


def cmd_demo(args, cfg):
    """Build a report from fictional sample companies: no key, no network, and nothing real is touched."""
    from .demo import write_demo
    out = Path(args.out) if args.out else cfg.path("general", "report_dir") / "demo"
    info = write_demo(out)
    page = Path(info["report"])
    print(f"Sample report ({info['companies']} fictional companies, {info['jobs']} roles): {page}")
    if args.open:
        _open(page)
    else:
        print("Add --open to open it in your browser. For real companies: `raiseradar setup`, then `raiseradar run`.")


def cmd_serve(args, cfg):
    """Serve the report on a web page and refresh it on a schedule (for Docker, or a machine that stays on)."""
    from .serve import serve
    sc = cfg["serve"]
    full = 0 if args.no_schedule else (sc["full_every_hours"] if args.full_every is None else args.full_every)
    jobs = 0 if args.no_schedule else (sc["jobs_every_hours"] if args.jobs_every is None else args.jobs_every)
    serve(cfg, args.host or sc["host"], args.port if args.port is not None else sc["port"], full, jobs,
          public=True if args.public else None, tracker=args.tracker)


def cmd_setup(args, cfg):
    """Check/store your OpenRouter key in .env."""
    if not interactive() and not args.check:
        sys.exit("`setup` asks for the key on the keyboard. Run it in a terminal, or put OPENROUTER_API_KEY=... in .env yourself.")
    if args.check:
        from .onboarding import check_api_key, mask
        if not cfg.openrouter_key:
            sys.exit("No OPENROUTER_API_KEY found (environment or .env).")
        status, detail = check_api_key(cfg.openrouter_key)
        print(f"{mask(cfg.openrouter_key)}: {status} ({detail})")
        sys.exit(0 if status == "ok" else 1)
    ok = setup_key(cfg.root / ".env", current=cfg.openrouter_key, replace=args.replace)
    if ok:
        print(f"\nNext: `raiseradar run`  (the first run takes {COLD_RUN}; later runs take a few minutes).")


def _has_real_data(cfg) -> bool:
    path = cfg.path("general", "db_path")
    if not path.is_file():
        return False
    db = DB(path)
    try:
        return bool(db.all_companies())
    finally:
        db.close()


def cmd_start(args, cfg):
    """Guided first use: show the sample report, then (optionally) add a key and start a real run."""
    page = cfg.path("general", "report_dir") / "index.html"
    if _has_real_data(cfg):
        print(f"You already have data. Your report: {page}\nRefresh it any time with: raiseradar run")
        if args.open:
            _open(page)
        return
    from .demo import write_demo
    info = write_demo(cfg.path("general", "report_dir") / "demo")
    demo_page = Path(info["report"])
    print(f"Here is a sample report with made-up companies so you can see what the tool shows:\n  {demo_page}\n")
    if args.open and interactive():
        _open(demo_page)
    if not interactive():
        print("For real companies: `raiseradar setup` (free OpenRouter key), then `raiseradar run`.\n"
              f"The first real run takes {COLD_RUN}; later runs take a few minutes.")
        return
    have_key = setup_key(cfg.root / ".env", current=cfg.openrouter_key)
    run_args = argparse.Namespace(skip_news=not have_key, skip_yc=False, limit=0)
    if have_key:
        question = f"\nStart the first real run now? It takes {COLD_RUN} (free AI models are slow); Ctrl-C is safe. [y/N] "
    else:
        question = "\nRun without a key instead? You get real Y Combinator companies and their open roles (no funding news). [y/N] "
    if args.yes or ask(question).lower().startswith("y"):
        cmd_run(run_args, cfg)
    else:
        print("\nWhenever you are ready: `raiseradar run`" + ("" if have_key else "   (add a key first with `raiseradar setup` for funding news)"))


def cmd_stage(args, cfg):
    p, db, f = build(cfg, need_llm=args.cmd == "discover")
    if args.cmd == "discover":
        out = {}
        if not args.skip_news:
            out.update(p.discover_news())
        if not args.skip_yc:
            out.update(p.discover_yc())
    elif args.cmd == "resolve":
        out = p.resolve_websites(args.limit, force=args.force, recheck=getattr(args, "recheck", False))
    elif args.cmd == "careers":
        out = p.discover_careers(args.limit, force=args.force)
    elif args.cmd == "jobs":
        out = p.fetch_jobs(args.limit)
    else:
        out = write_report(p, public=True if getattr(args, "public", False) else None)
    print(json.dumps(out, indent=2))
    if args.cmd == "report":
        _open_if_asked(args, out.get("report"))


def cmd_add(args, cfg):
    """Add (or fix) a company by hand: its website is trusted, and it is always in scope."""
    p, db, f = build(cfg, need_llm=False)
    cid, created = db.upsert_company(args.name, origin="manual")
    fields = {"website_confidence": "manual"}
    if args.website:
        site = args.website if "//" in args.website else "https://" + args.website
        from .util import domain_of
        fields.update(website=site, domain=domain_of(site), resolve_checked_at=now_iso())
    if args.ats and args.slug:
        fields.update(ats=args.ats, ats_slug=args.slug, ats_confidence="manual", careers_checked_at=now_iso())
    elif args.website:
        fields.update(careers_checked_at=None, ats=None, ats_slug=None)   # re-run careers discovery for the new site
    db.update_company(cid, **fields)
    if args.round:
        db.add_funding(cid, round=args.round, amount_usd_m=None, amount_text=args.amount, investors=[], lead_investors=[],
                       announced_at=now_iso(), source_name="manual", source_url=args.source or "", headline=f"{args.name} {args.round} (added manually)")
    print(("added " if created else "updated ") + args.name)


def cmd_list(args, cfg):
    p, db, f = build(cfg, need_llm=False)
    from .report import build_data
    data = build_data(p)
    for c in data["companies"][: args.top]:
        fr = c["funding"][0] if c["funding"] else {}
        print(f"{c['score']:5.1f}  {c['name'][:28]:28} {fr.get('round', 'YC' if c['yc'] else ''):9} {(fr.get('amount_text') or '')[:12]:12} "
              f"open={c['open']:<3} fit={c['matched']:<3} {c['ats'] or '-':15} {c['board_url'] or c['careers_url'] or c['jobs_status'] or ''}")


def _track_candidates(t, db, query: list[str], tracked_only: bool = False) -> list[tuple[str, dict]]:
    """Everything the words could mean: already tracked entries first, then roles in the database."""
    import re
    from .tracker import find_jobs, norm_name_words
    q = " ".join(query).strip()
    if not q:
        sys.exit("error: say which role (words from the company or title, an 8-character code, or #number for a tracked entry)")
    apps = t.list()
    if m := re.fullmatch(r"#(\d+)", q):
        return [("app", a) for a in apps if a["id"] == int(m.group(1))]
    words = norm_name_words(q)
    tracked = [a for a in apps if q == a["job_key"] or q.lower() == a["code"]
               or (words and all(w in (a["company"] + " " + a["title"]).lower() for w in words))]
    seen = {a["job_key"] for a in tracked if a["job_key"]}
    jobs = [] if (db is None or tracked_only) else [j for j in find_jobs(db, q) if j["key"] not in seen]
    return [("app", a) for a in tracked] + [("job", j) for j in jobs]


def _describe(kind: str, x: dict) -> str:
    if kind == "app":
        return f"  #{x['id']:<4} {x['status']:<12} {x['company']} - {x['title']}" + (f"  ({x['location']})" if x["location"] else "")
    return f"  {x['code']}  {'open  ' if x['active'] else 'closed'}       {x['company']} - {x['title']}" + (f"  ({x['location']})" if x["location"] else "")


def _track_pick(t, db, query: list[str], tracked_only: bool = False) -> tuple[str, dict]:
    cands = _track_candidates(t, db, query, tracked_only)
    if not cands:
        sys.exit("error: nothing matches " + repr(" ".join(query)) + (" among your tracked roles" if tracked_only else
                 ". Search by words from the company or title; the codes are shown in the expanded role in the report."))
    if len(cands) > 1:
        print("Several roles match. Repeat the command with one code (or #number), or more words:")
        for kind, x in cands[:15]:
            print(_describe(kind, x))
        if len(cands) > 15:
            print(f"  ... and {len(cands) - 15} more")
        sys.exit(2)
    return cands[0]


def cmd_track(args, cfg):
    """Keep track of roles you saved, applied to, or want to hide. Stored in data/applications.db, separate from the job cache."""
    from . import tracker as tr
    from .util import days_since
    t = tr.Tracker(cfg.path("tracker", "path"))
    path = cfg.path("general", "db_path")
    db = DB(path) if path.is_file() else None
    try:
        sub = args.track_cmd
        if sub == "set":
            status = tr.parse_status(args.status)
            kind, x = _track_pick(t, db, args.query)
            if kind == "app":
                a = t.set(app_id=x["id"], status=status, note=args.note, applied_at=args.date)
            else:
                a = t.set(job_key=x["key"], status=status, note=args.note, applied_at=args.date,
                          meta={"company": x["company"], "title": x["title"], "location": x["location"], "url": x["url"]})
            print(f"{tr.LABELS[a['status']]}: {a['company']} - {a['title']}  (#{a['id']})")
        elif sub == "add":
            a = t.add_manual(args.company, args.title, url=args.url or "", location=args.location or "", status=args.status,
                             note=args.note or "", applied_at=args.date)
            print(f"{tr.LABELS[a['status']]}: {a['company']} - {a['title']}  (#{a['id']})")
        elif sub == "note":
            kind, x = _track_pick(t, db, args.query, tracked_only=True)
            a = t.set(app_id=x["id"], note=args.text)
            print(f"Note saved for {a['company']} - {a['title']}")
        elif sub == "rm":
            kind, x = _track_pick(t, db, args.query, tracked_only=True)
            t.delete(app_id=x["id"])
            print(f"Removed {x['company']} - {x['title']} from your tracker")
        elif sub == "show":
            kind, x = _track_pick(t, db, args.query, tracked_only=True)
            print(f"#{x['id']}  {x['company']} - {x['title']}\n  status: {tr.LABELS[x['status']]}" + (f"   applied: {x['applied_at']}" if x["applied_at"] else ""))
            for k in ("location", "url", "note"):
                if x[k]:
                    print(f"  {k}: {x[k]}")
            print("  history: " + " -> ".join(f"{tr.LABELS[e['status']]} {e['at'][:10]}" for e in x["events"]))
        else:   # list
            apps = tr.app_views(t, db) if db else [dict(a, key=a["job_key"], listing="unknown") for a in t.list()]
            if args.status:
                apps = [a for a in apps if a["status"] == tr.parse_status(args.status)]
            elif not args.all:
                apps = [a for a in apps if a["status"] != "ignored"]
            if not apps:
                print("Nothing tracked yet. Mark a role in the report, or: raiseradar track set applied <words from the company or title>")
                return
            order = {k: i for i, k in enumerate(["interviewing", "offer", "applied", "saved", "rejected", "ignored"])}
            follow = float(cfg["tracker"]["follow_up_days"])
            for a in sorted(apps, key=lambda a: (order[a["status"]], a["updated_at"])):
                flags = []
                if a["listing"] == "closed":
                    flags.append("role no longer listed")
                if tr.needs_follow_up(a, follow):
                    flags.append(f"no news for {int(days_since(a['updated_at']))}d: follow up?")
                when = f" applied {a['applied_at']}" if a["applied_at"] else ""
                print(f"#{a['id']:<4} {tr.LABELS[a['status']]:<14} {a['company'][:26]:26} {a['title'][:44]:44}{when}" + (f"   [{'; '.join(flags)}]" if flags else ""))
    except tr.TrackerError as exc:
        sys.exit(f"error: {exc}")
    finally:
        if db:
            db.close()


def cmd_me(args, cfg):
    """Your profile for the outreach bot: data/me.toml (private, never in reports)."""
    from .me import init_me, load_me
    path = cfg.path("outreach", "me_path")
    if args.me_cmd == "init":
        if init_me(path):
            print(f"Wrote {path}\nEdit it (it is plain text), then run: raiseradar me check")
        else:
            print(f"{path} already exists; not overwritten. Edit it directly.")
        return
    me = load_me(path)
    if not path.is_file():
        sys.exit(f"No profile yet. Run: raiseradar me init   (creates {path})")
    if args.me_cmd == "show":
        print(me.facts())
        for p in me.projects:
            print(f"\nProject: {p.name}  [{', '.join(p.tags)}]\n  {p.summary}  {p.url}")
        print("\nRoles wanted: " + ", ".join(me.roles_wanted))
        if me.years_experience:
            print(f"Years of experience: {me.years_experience:g}")
    miss = me.missing()
    print("\nProfile is complete." if not miss else "\nStill to fill in: " + "; ".join(miss))
    if miss and args.me_cmd == "check":
        sys.exit(1)


def _env_set(path: Path, items: dict[str, str]) -> list[str]:
    """Add KEY=VALUE lines to .env for keys that are not there yet (never overwrites). Returns the keys written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    have = {ln.split("=", 1)[0].strip() for ln in text.splitlines() if "=" in ln and not ln.strip().startswith("#")}
    add = {k: v for k, v in items.items() if k not in have}
    if add:
        path.write_text(text + ("" if not text or text.endswith("\n") else "\n") + "".join(f"{k}={v}\n" for k, v in add.items()), encoding="utf-8")
        path.chmod(0o600)
    return list(add)


def _outreach_parts(cfg, need_llm=True, need_fetch=True):
    import os
    from . import tracker as tr
    from .me import load_me
    from .outreach import Outreach, Store
    path = cfg.path("general", "db_path")
    p, db, f = build(cfg, need_llm=need_llm)
    me = load_me(cfg.path("outreach", "me_path"))
    store = Store(cfg.path("outreach", "db_path"))
    t = tr.Tracker(cfg.path("tracker", "path"))
    return Outreach(cfg, me, store, db, fetcher=f if need_fetch else None, llm=p.llm, tracker=t, env=os.environ), db, f


def _show_draft(d: dict, facts: dict | None = None) -> None:
    print(f"#{d['id']}  {d['status']}  {d['company']} - {d['role_title']}  (fit {d['score']})\n  to: {d['to_email']}\n  subject: {d['subject']}\n")
    print("  " + d["body"].replace("\n", "\n  ") + "\n")
    if facts is not None:
        if d.get("job_url"):
            print(f"  job post: {d['job_url']}")
        if facts.get("skills"):
            print("  your skills it asks for: " + ", ".join(facts["skills"]))
        if facts.get("exp_min") is not None:
            print(f"  asks for {facts['exp_min']:g}+ years")
        if facts.get("jd"):
            print("  from the job post:\n    " + facts["jd"].replace("\n", "\n    "))
        print()


def _outreach_schedule(args, cfg):
    from . import schedule as sch
    if args.off:
        gone = sch.remove() if sch.has_systemd() else []
        print("Removed: " + ", ".join(gone) if gone else "No RaiseRadar timers were installed. (If you used cron, remove the lines with `crontab -e`.)")
        return
    try:
        plan = sch.jobs(cfg.root, sys.executable, args.every, args.refresh)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    if args.print or not sch.has_systemd():
        if not sch.has_systemd():
            print("No systemd here. Add these lines with `crontab -e`:\n")
        else:
            for n, (d, c, w) in plan.items():
                print(f"# ~/.config/systemd/user/{n}.service and .timer ({w})\n" + "\n".join(sch.units(n, d, c, w, cfg.root)))
            print("Or, with cron:\n")
        print("\n".join(sch.cron_lines(plan, args.every, cfg.root)))
        return
    names = sch.install(cfg.root, sys.executable, args.every, args.refresh)
    print(f"Installed and started: {', '.join(names)}")
    print(f"`outreach run` now runs every {args.every} minutes while you are logged in; a run missed during sleep happens on wake-up.")
    if not cfg["outreach"]["enabled"]:
        print("Sending is still OFF: it drafts and asks you, approved emails wait. Set [outreach] enabled = true in config.toml to send.")
    print("\nSee runs:   journalctl --user -u raiseradar-outreach -n 50"
          "\nNext run:   systemctl --user list-timers 'raiseradar-*'"
          "\nStop:       raiseradar outreach schedule --off"
          "\nTo keep running while logged out: loginctl enable-linger $USER")


def cmd_outreach(args, cfg):
    import secrets
    from . import mailer, notify
    from .outreach import OutreachError
    sub = args.out_cmd
    o = cfg["outreach"]
    try:
        if sub == "setup":
            added = _env_set(cfg.root / ".env", {"OUTREACH_NTFY_TOPIC": "rr-" + secrets.token_urlsafe(18),
                                                 "OUTREACH_NTFY_REPLY_TOPIC": "rr-" + secrets.token_urlsafe(18),
                                                 "OUTREACH_APPROVAL_SECRET": secrets.token_urlsafe(32)})
            import os
            from .config import load_dotenv
            load_dotenv(cfg.root / ".env")
            print("Created approval keys in .env: " + ", ".join(added) if added else "Approval keys already exist in .env (kept).")
            print(f"\nOn your phone: install the ntfy app, add server {o['ntfy_server']} and subscribe to topic:\n  {os.environ['OUTREACH_NTFY_TOPIC']}"
                  "\n(The topic name is the only thing protecting who can read the drafts, so keep it private.)"
                  "\n\nStill to add to .env yourself (Gmail: Google Account > Security > 2-Step Verification > App passwords):"
                  "\n  OUTREACH_SMTP_USER=you@gmail.com\n  OUTREACH_SMTP_PASSWORD=<16-character app password>"
                  "\nThen: raiseradar me init && raiseradar outreach check")
            return
        if sub == "schedule":
            _outreach_schedule(args, cfg)
            return
        if sub == "block":
            from .outreach import Store
            print("Blocked:", Store(cfg.path("outreach", "db_path")).block(args.pattern, args.reason or ""))
            return
        ou, db, f = _outreach_parts(cfg, need_llm=sub == "run", need_fetch=sub in ("run", "contact"))
        try:
            if sub == "check":
                import os
                print("profile: " + ("complete" if not ou.me.missing() else "missing " + "; ".join(ou.me.missing())))
                try:
                    mailer.check_login(o["smtp_host"], int(o["smtp_port"]), os.environ.get("OUTREACH_SMTP_USER", ""), os.environ.get("OUTREACH_SMTP_PASSWORD", ""))
                    print("mail login: ok (nothing was sent)")
                except mailer.MailError as exc:
                    print(f"mail login: FAILED - {exc}")
                if ou.gmail_on():
                    from .gmailbox import MailboxError, check_login as imap_check
                    try:
                        folder = imap_check(o["imap_host"], int(o["imap_port"]), ou.smtp_user, os.environ.get("OUTREACH_SMTP_PASSWORD", ""))
                        print(f"gmail drafts: ok (editing copies go to {folder}; Sent is checked before sending)")
                    except MailboxError as exc:
                        print(f"gmail drafts: FAILED - {exc}. Turn on IMAP in Gmail settings, or set [outreach] gmail_drafts = false")
                else:
                    print("gmail drafts: off (needs the mail login; [outreach] gmail_drafts)")
                if ou.phone_ready():
                    ok = notify.publish(o["ntfy_server"], ou.topic, ou.reply_topic, ou.secret,
                                        {"id": 0, "company": "Test", "role_title": "Test notification", "score": 1.0, "to_email": "nobody@example.com",
                                         "subject": "Test", "body": "This is a test of the approval notification. The buttons do nothing.",
                                         "job_url": "https://ntfy.sh"},
                                        facts={"skills": ["python"], "exp_min": 1, "years": 1, "jd": "Requirements: (the job post's own words appear here)"},
                                        edit_url=ou.gmail_edit_url() if ou.gmail_on() else None)
                    print("phone notification: " + ("sent (check your phone)" if ok else "FAILED (is the ntfy server reachable?)"))
                else:
                    print("phone notification: not set up. Run: raiseradar outreach setup")
                print(f"sending is {'ON' if o['enabled'] else 'OFF'} ([outreach] enabled in config.toml); cap {o['daily_cap']}/day")
            elif sub == "run":
                res = ou.run(dry_run=args.dry_run, limit=args.limit)
                if args.dry_run:
                    for d in res["drafts"]:
                        _show_draft({**d, "id": "-", "status": "dry run"})
                    for l in res["leads"]:
                        print(f"LEAD (no public email) {l['company']}: {l['role_title']}\n  apply: {l['apply_url']}\n  LinkedIn note ({len(l['linkedin_note'])} chars): {l['linkedin_note']}\n")
                    print(f"{len(res['drafts'])} draft(s) and {len(res['leads'])} apply-directly lead(s) (dry run: nothing stored, asked or sent).")
                else:
                    print(json.dumps(res, indent=2))
                    if not o["enabled"]:
                        print("\nNote: sending is OFF. Approved drafts wait. Set [outreach] enabled = true in config.toml when you are ready.")
                    if not ou.phone_ready():
                        print("Note: no phone approval set up (`raiseradar outreach setup`); review in the terminal with `raiseradar outreach review`.")
            elif sub == "status":
                rows = ou.store.list(args.status)
                for d in rows[:40]:
                    print(f"#{d['id']:<4} {d['status']:<9} {d['company'][:24]:24} {d['role_title'][:36]:36} -> {d['to_email']}" + (f"   ({d['error']})" if d["error"] else ""))
                print(f"\n{len(rows)} draft(s); sent in the last 24h: {len(ou.store.sent_since(24))}/{o['daily_cap']}; blocked: {len(ou.store.blocked())}")
            elif sub == "leads":
                if args.done:
                    if not ou.store.get_lead(args.done):
                        sys.exit(f"no lead #{args.done}")
                    ou.store.set_lead(args.done, status="done")
                    print(f"Lead #{args.done} marked done.")
                elif args.show:
                    l = ou.store.get_lead(args.show)
                    if not l:
                        sys.exit(f"no lead #{args.show}")
                    print(f"#{l['id']}  {l['company']} - {l['role_title']}  ({l['status']})\n  apply: {l['apply_url'] or l['job_url']}\n  best project: {l['project']}\n")
                    print(f"LinkedIn note ({len(l['linkedin_note'])} chars):\n  {l['linkedin_note']}\n")
                    print("Message to paste into an application or a longer LinkedIn message:\n  " + (l["message"] or "(none: no draft could be written)").replace("\n", "\n  "))
                else:
                    rows = ou.store.leads(None if args.all else "new")
                    for l in rows:
                        print(f"#{l['id']:<4} {l['status']:<8} {l['company'][:24]:24} {l['role_title'][:36]:36} {l['apply_url'] or l['job_url']}")
                    print(f"\n{len(rows)} lead(s). See the texts: raiseradar outreach leads --show ID   Done: --done ID")
            elif sub == "show":
                d = ou.store.get(args.id)
                if not d:
                    sys.exit(f"no draft #{args.id}")
                _show_draft(d, ou.facts(d))
            elif sub in ("approve", "skip"):
                d = ou.store.get(args.id)
                if not d:
                    sys.exit(f"no draft #{args.id}")
                if not ou.store.decide(args.id, sub, "terminal"):
                    sys.exit(f"#{args.id} is {d['status']}, not pending")
                print(f"#{args.id} {'approved' if sub == 'approve' else 'skipped'}." + (" It is sent by the next `outreach run` (or `outreach send`)." if sub == "approve" else ""))
            elif sub == "send":
                print(json.dumps(ou.send_approved(), indent=2))
            elif sub == "review":
                pend = list(reversed(ou.store.list("pending")))
                if not pend:
                    print("Nothing waiting for your approval.")
                for d in pend:
                    _show_draft(d, ou.facts(d))
                    ans = ask("[s]end  [k] skip  [b]lock this company  [enter] later > ").strip().lower()
                    if ans.startswith("s"):
                        ou.store.decide(d["id"], "approve", "terminal")
                    elif ans.startswith("k"):
                        ou.store.decide(d["id"], "skip", "terminal")
                    elif ans.startswith("b"):
                        ou.store.block(d["domain"], "blocked in review")
                if pend:
                    print(json.dumps(ou.send_approved(), indent=2))
            elif sub == "contact":
                if args.contact_cmd == "add":
                    ou.store.add_contact(args.domain, args.email)
                    print(f"Added {args.email} for {args.domain}")
                else:
                    for k in ou.store.contacts_for(args.domain):
                        print(f"{k['email']:40} {k['source_url']}")
        finally:
            db.close()
    except OutreachError as exc:
        sys.exit(f"error: {exc}")


def cmd_models(args, cfg):
    for m in list_free_models():
        print(f"{m['id']:55} ctx={m['context']:>8} json={'Y' if m['json'] else '-'} tools={'Y' if m['tools'] else '-'}")
    print("\nConfigured order:", ", ".join(cfg["llm"]["models"]))


BENCH = [
    ("Infisical Raises $40 Million Series B at $600 Million Valuation - Unite.AI", True, "Infisical", "Series B"),
    ("Serval Raises $150M Series C at $3B Valuation to Hire Over 500 - Unite.AI", True, "Serval", "Series C"),
    ("GM Leads $108M Series B Funding Round For Peak Energy Battery Startup - GM Authority", True, "Peak Energy", "Series B"),
    ("Series B Funding Is Up, But Fewer Deals Are Getting Done - Crunchbase News", False, None, None),
    ("Sequoia closes new $950M early-stage fund - TechCrunch", False, None, None),
    ("Acme Corp acquired by BigCo in $2B deal after Series C - Reuters", False, None, None),
    ("Bengaluru-based fintech Zeta raises Rs 400 crore in Series C led by Tiger Global - Inc42", True, "Zeta", "Series C"),
    ("Berlin's Nuri lands EUR 30M Series B from Balderton - Sifted", True, "Nuri", "Series B"),
]


def cmd_bench(args, cfg):
    """Score each configured/free model on a tiny labelled set so you can pick the best one for extraction."""
    from .extract import extract_funding
    from .models import Article
    key = cfg.openrouter_key
    if not key:
        sys.exit("Set OPENROUTER_API_KEY in .env first.")
    models = args.models or cfg["llm"]["models"]
    arts = [Article(id=str(i), title=t, summary="", url="", publisher="", published_at="2026-10-30", feed="") for i, (t, *_rest) in enumerate(BENCH)]
    for m in models:
        lc = cfg["llm"]
        client = LLMClient(key, [m], max_calls=4, requests_per_minute=10, max_tokens=lc["max_tokens"], reasoning_effort=lc["reasoning_effort"])
        t0 = time.time()
        try:
            items, answered = extract_funding(client, arts)
        except LLMError as exc:
            print(f"{m:55} FAILED  {str(exc)[:90]}")
            continue
        got = {it.article.id: it for it in items}
        ok = 0
        for i, (_t, is_f, comp, rnd) in enumerate(BENCH):
            it = got.get(str(i))
            if is_f is False and it is None:
                ok += 1
            elif is_f and it and it.company.lower().startswith(comp.lower()) and it.round == rnd:
                ok += 1
        print(f"{m:55} {ok}/{len(BENCH)} correct   {time.time() - t0:5.1f}s")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="raiseradar", description="Find recently funded startups and what they are hiring for.")
    ap.add_argument("-c", "--config", help="path to config.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    st = sub.add_parser("start", help="guided first use (this is what plain `raiseradar` does): sample report, then add a key and run")
    st.add_argument("--yes", action="store_true", help="start the real run without asking")
    st.add_argument("--no-open", dest="open", action="store_false", help="do not open the report in a browser")
    st.set_defaults(fn=cmd_start)
    dm = sub.add_parser("demo", help="build a report from fictional sample companies (no key needed)")
    dm.add_argument("--out", help="folder for the sample report (default: <report_dir>/demo)")
    dm.add_argument("--open", action="store_true", help="open it in your browser")
    dm.set_defaults(fn=cmd_demo)
    sv = sub.add_parser("serve", help="show the report on a web page and refresh it on a schedule (for Docker or a machine that stays on)")
    sv.add_argument("--host", help="address to listen on (default 127.0.0.1, this computer only; 0.0.0.0 = everyone who can reach it)")
    sv.add_argument("--port", type=int, help="port (default 8080)")
    sv.add_argument("--full-every", type=float, metavar="HOURS", help="full run (news + companies + jobs) every N hours (default 12; 0 = never)")
    sv.add_argument("--jobs-every", type=float, metavar="HOURS", help="jobs-only refresh every N hours (default 4; 0 = never)")
    sv.add_argument("--no-schedule", action="store_true", help="only show the report, never run anything")
    sv.add_argument("--public", action="store_true", help="shorten job descriptions to excerpts and mark the page noindex (application tracking is always off)")
    sv.add_argument("--tracker", dest="tracker", action="store_true", default=None,
                    help="let the page save your applications even though the server is not limited to this computer (default: on only for localhost)")
    sv.add_argument("--no-tracker", dest="tracker", action="store_false", help="never let the page save applications")
    sv.set_defaults(fn=cmd_serve)
    su = sub.add_parser("setup", help="check and store your OpenRouter API key in .env")
    su.add_argument("--replace", action="store_true", help="enter a new key even if one works")
    su.add_argument("--check", action="store_true", help="only check the key that is configured")
    su.set_defaults(fn=cmd_setup)

    r = sub.add_parser("run", help="full pipeline: discover -> websites -> careers -> jobs -> report")
    r.add_argument("--skip-news", action="store_true"); r.add_argument("--skip-yc", action="store_true")
    r.add_argument("--limit", type=int, default=0, help="max companies per stage (for quick tests)")
    r.add_argument("--open", action="store_true", help="open the report in your browser when done (terminal only; ignored under cron)")
    r.add_argument("--public", action="store_true", help="write the report for a public website: job descriptions shortened to an excerpt, page marked noindex")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("discover", help="stage 1-2: funding news (LLM) and YC")
    d.add_argument("--skip-news", action="store_true"); d.add_argument("--skip-yc", action="store_true")
    d.set_defaults(fn=cmd_stage)
    for name, helptext in (("resolve", "stage 3: find company websites"), ("careers", "stage 4: find careers pages / ATS"),
                           ("jobs", "stage 5: fetch open jobs"), ("report", "stage 6: build the report")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--limit", type=int, default=0)
        if name in ("resolve", "careers"):
            s.add_argument("--force", action="store_true", help="redo even if checked recently")
        if name == "report":
            s.add_argument("--open", action="store_true", help="open the report in your browser (terminal only)")
            s.add_argument("--public", action="store_true", help="write the report for a public website: job descriptions shortened to an excerpt, page marked noindex")
        if name == "resolve":
            s.add_argument("--recheck", action="store_true", help="also re-grade websites that were auto-matched earlier")
        s.set_defaults(fn=cmd_stage)

    a = sub.add_parser("add", help="add/fix a company by hand")
    a.add_argument("name"); a.add_argument("--website"); a.add_argument("--ats", help="greenhouse|lever|ashby|workable|smartrecruiters|recruitee")
    a.add_argument("--slug", help="board slug for --ats"); a.add_argument("--round"); a.add_argument("--amount"); a.add_argument("--source")
    a.set_defaults(fn=cmd_add)

    tk = sub.add_parser("track", help="keep track of roles you saved, applied to, or want to hide (stored in data/applications.db)")
    ts = tk.add_subparsers(dest="track_cmd", required=True)
    x = ts.add_parser("set", help="set the status of a role: saved, applied, interviewing, offer, rejected, ignored")
    x.add_argument("status"); x.add_argument("query", nargs="+", help="words from the company or title, an 8-character code, or #number")
    x.add_argument("--note"); x.add_argument("--date", help="when you applied (YYYY-MM-DD); default today")
    x = ts.add_parser("add", help="track a role that is not in the report (a referral, another job board)")
    x.add_argument("--company", required=True); x.add_argument("--title", required=True)
    x.add_argument("--url"); x.add_argument("--location"); x.add_argument("--note"); x.add_argument("--date")
    x.add_argument("--status", default="applied")
    x = ts.add_parser("list", help="show what you track")
    x.add_argument("--status"); x.add_argument("--all", action="store_true", help="include roles marked not interested")
    x = ts.add_parser("show", help="details and history of one tracked role"); x.add_argument("query", nargs="+")
    x = ts.add_parser("note", help="set the note of a tracked role"); x.add_argument("--text", required=True); x.add_argument("query", nargs="+")
    x = ts.add_parser("rm", help="stop tracking a role"); x.add_argument("query", nargs="+")
    tk.set_defaults(fn=cmd_track)

    me_p = sub.add_parser("me", help="your profile for the outreach bot (data/me.toml)")
    ms = me_p.add_subparsers(dest="me_cmd", required=True)
    ms.add_parser("init", help="create data/me.toml from a template (never overwrites)")
    ms.add_parser("show", help="print what the bot knows about you")
    ms.add_parser("check", help="say what is still missing (exit 1 if anything)")
    me_p.set_defaults(fn=cmd_me)

    ot = sub.add_parser("outreach", help="draft cold emails to founders about roles that fit you; you approve from your phone")
    os_ = ot.add_subparsers(dest="out_cmd", required=True)
    os_.add_parser("setup", help="create the private approval topics and secret in .env and explain the phone and Gmail steps")
    os_.add_parser("check", help="test the mail login (sends nothing), the phone notification and your profile")
    x = os_.add_parser("run", help="collect your taps, draft new emails, ask you, send what is approved (cron-friendly)")
    x.add_argument("--dry-run", action="store_true", help="print the drafts it would make; store, ask and send nothing")
    x.add_argument("--limit", type=int, help="max new drafts")
    x = os_.add_parser("status", help="list drafts"); x.add_argument("--status", choices=list(OUTREACH_STATUSES))
    x = os_.add_parser("schedule", help="run `outreach run` by itself every N minutes (systemd user timer; prints cron lines otherwise)")
    x.add_argument("--every", type=int, default=30, metavar="MIN", help="minutes between runs (default 30)")
    x.add_argument("--refresh", action="store_true", help="also refresh the job data: full run twice a day, jobs every 4 hours")
    x.add_argument("--off", action="store_true", help="remove the timers")
    x.add_argument("--print", action="store_true", help="only print what would be installed")
    x = os_.add_parser("leads", help="companies with no public email: apply link, LinkedIn note and message to paste")
    x.add_argument("--show", type=int, metavar="ID", help="print the texts for one lead")
    x.add_argument("--done", type=int, metavar="ID", help="mark a lead as handled")
    x.add_argument("--all", action="store_true", help="include handled leads")
    x = os_.add_parser("show", help="print one draft"); x.add_argument("id", type=int)
    x = os_.add_parser("approve", help="approve a pending draft from the terminal"); x.add_argument("id", type=int)
    x = os_.add_parser("skip", help="reject a pending draft"); x.add_argument("id", type=int)
    os_.add_parser("review", help="go through pending drafts in the terminal (send / skip / block)")
    os_.add_parser("send", help="send approved drafts now (respects the daily cap and the on/off switch)")
    x = os_.add_parser("block", help="never email this domain or address (also skips its pending drafts)")
    x.add_argument("pattern"); x.add_argument("--reason")
    cx = os_.add_parser("contact", help="addresses the bot may use for a company")
    cs = cx.add_subparsers(dest="contact_cmd", required=True)
    x = cs.add_parser("add", help="add a founder or hiring address you know"); x.add_argument("domain"); x.add_argument("email")
    x = cs.add_parser("list", help="show known addresses for a domain"); x.add_argument("domain")
    ot.set_defaults(fn=cmd_outreach)

    l = sub.add_parser("list", help="print the ranked companies"); l.add_argument("--top", type=int, default=40); l.set_defaults(fn=cmd_list)
    sub.add_parser("models", help="show currently free OpenRouter models").set_defaults(fn=cmd_models)
    b = sub.add_parser("bench", help="test models on a small labelled funding-extraction set")
    b.add_argument("models", nargs="*"); b.set_defaults(fn=cmd_bench)

    args = ap.parse_args(argv)
    if args.cmd is None:    # plain `raiseradar`: the guided start
        args = ap.parse_args((["-c", args.config] if args.config else []) + (["-v"] if args.verbose else []) + ["start"])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config(args.config)
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
