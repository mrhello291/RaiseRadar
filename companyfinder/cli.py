"""Command line interface:  python -m companyfinder <command>"""
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
              "      Run `companyfinder setup` to add a free key.\n")
    run_id = db.start_run()
    t0 = time.time()
    try:
        stats = p.run_all(news=not args.skip_news, yc=not args.skip_yc, limit=args.limit)
    except KeyboardInterrupt:      # everything finished so far is saved; show it and let the next run continue
        print("\nInterrupted. Building the report from what is saved so far; the next `run` continues where this one stopped.")
        stats = {"interrupted": True}
    stats.update(write_report(p))
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
        print("Add --open to open it in your browser. For real companies: `companyfinder setup`, then `companyfinder run`.")


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
        print(f"\nNext: `companyfinder run`  (the first run takes {COLD_RUN}; later runs take a few minutes).")


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
        print(f"You already have data. Your report: {page}\nRefresh it any time with: companyfinder run")
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
        print("For real companies: `companyfinder setup` (free OpenRouter key), then `companyfinder run`.\n"
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
        print("\nWhenever you are ready: `companyfinder run`" + ("" if have_key else "   (add a key first with `companyfinder setup` for funding news)"))


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
        out = write_report(p)
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
    ap = argparse.ArgumentParser(prog="companyfinder", description="Find recently funded startups and what they are hiring for.")
    ap.add_argument("-c", "--config", help="path to config.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    st = sub.add_parser("start", help="guided first use (this is what plain `companyfinder` does): sample report, then add a key and run")
    st.add_argument("--yes", action="store_true", help="start the real run without asking")
    st.add_argument("--no-open", dest="open", action="store_false", help="do not open the report in a browser")
    st.set_defaults(fn=cmd_start)
    dm = sub.add_parser("demo", help="build a report from fictional sample companies (no key needed)")
    dm.add_argument("--out", help="folder for the sample report (default: <report_dir>/demo)")
    dm.add_argument("--open", action="store_true", help="open it in your browser")
    dm.set_defaults(fn=cmd_demo)
    su = sub.add_parser("setup", help="check and store your OpenRouter API key in .env")
    su.add_argument("--replace", action="store_true", help="enter a new key even if one works")
    su.add_argument("--check", action="store_true", help="only check the key that is configured")
    su.set_defaults(fn=cmd_setup)

    r = sub.add_parser("run", help="full pipeline: discover -> websites -> careers -> jobs -> report")
    r.add_argument("--skip-news", action="store_true"); r.add_argument("--skip-yc", action="store_true")
    r.add_argument("--limit", type=int, default=0, help="max companies per stage (for quick tests)")
    r.add_argument("--open", action="store_true", help="open the report in your browser when done (terminal only; ignored under cron)")
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
        if name == "resolve":
            s.add_argument("--recheck", action="store_true", help="also re-grade websites that were auto-matched earlier")
        s.set_defaults(fn=cmd_stage)

    a = sub.add_parser("add", help="add/fix a company by hand")
    a.add_argument("name"); a.add_argument("--website"); a.add_argument("--ats", help="greenhouse|lever|ashby|workable|smartrecruiters|recruitee")
    a.add_argument("--slug", help="board slug for --ats"); a.add_argument("--round"); a.add_argument("--amount"); a.add_argument("--source")
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser("list", help="print the ranked companies"); l.add_argument("--top", type=int, default=40); l.set_defaults(fn=cmd_list)
    sub.add_parser("models", help="show currently free OpenRouter models").set_defaults(fn=cmd_models)
    b = sub.add_parser("bench", help="test models on a small labelled funding-extraction set")
    b.add_argument("models", nargs="*"); b.set_defaults(fn=cmd_bench)

    args = ap.parse_args(argv)
    if args.cmd is None:    # plain `companyfinder`: the guided start
        args = ap.parse_args((["-c", args.config] if args.config else []) + (["-v"] if args.verbose else []) + ["start"])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config(args.config)
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
