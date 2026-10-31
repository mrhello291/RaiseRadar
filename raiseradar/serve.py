"""`raiseradar serve`: show the report on a small web page and, optionally, refresh it on a schedule.

Meant for a Docker container or a machine that stays on. No cron is needed: a background thread runs the full pipeline
every `full_every_hours` and a jobs-only refresh every `jobs_every_hours`, one at a time."""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
from urllib.parse import urlsplit

from .config import Config
from .tracker import LABELS, STATUSES, Tracker, TrackerError, app_views, job_meta
from .util import days_since

log = logging.getLogger("raiseradar.serve")
RETRY_AFTER = 3600          # seconds to wait after a scheduled run failed before trying again


LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]", "")
MAX_BODY = 20_000


def is_loopback(host: str) -> bool:
    return host in LOOPBACK


class TrackerAPI:
    """The small JSON interface the report page uses to save your applications.

    Writes are only offered when it is safe to: the server listens on this computer only (or you said so explicitly),
    and never for a public page. A page on another website cannot use it, because it needs a token that only a request
    from this same page can read, and because the Host header must be one of ours (this also stops DNS rebinding)."""

    def __init__(self, cfg: Config, host: str, writable: bool):
        self.cfg, self.writable = cfg, writable
        self.token = secrets.token_urlsafe(24)
        self.tracker = Tracker(cfg.path("tracker", "path"))
        extra = [h.strip().lower() for h in os.environ.get("RAISERADAR_ALLOWED_HOSTS", "").split(",") if h.strip()]
        self.hosts = {"localhost", "127.0.0.1", "::1"} | set(extra) | ({host.lower()} if host not in ("", "0.0.0.0", "::") else set())

    def host_ok(self, header: str | None) -> bool:
        if not header:
            return False
        h = header.strip().lower()
        name = h[1:h.index("]")] if h.startswith("[") and "]" in h else h.rsplit(":", 1)[0] if h.count(":") == 1 else h
        return name in self.hosts

    def _main_db(self):
        from .db import DB
        path = self.cfg.path("general", "db_path")
        return DB(path) if path.is_file() else None

    def snapshot(self) -> dict:
        db = self._main_db()
        try:
            apps = app_views(self.tracker, db) if db else [dict(a, key=a["job_key"], listing="unknown") for a in self.tracker.list()]
        finally:
            if db:
                db.close()
        out = {"writable": self.writable, "applications": apps, "statuses": [[k, LABELS[k]] for k in STATUSES],
               "follow_up_days": float(self.cfg["tracker"]["follow_up_days"])}
        if self.writable:
            out["token"] = self.token
        return out

    def apply(self, action: str, body: dict) -> dict:
        key = body.get("key") if isinstance(body.get("key"), str) else None
        app_id = body.get("id") if isinstance(body.get("id"), int) and not isinstance(body.get("id"), bool) else None
        if key is None and app_id is None:
            raise TrackerError("say which role: key or id")
        if action == "delete":
            if not self.tracker.delete(job_key=key, app_id=app_id):
                raise TrackerError("that role is not tracked")
            return self.snapshot()
        status = body.get("status")
        note, date = body.get("note"), body.get("date")
        if not all(v is None or isinstance(v, str) for v in (status, note, date)):
            raise TrackerError("status, note and date must be text")
        meta = None
        if app_id is None and key is not None:
            db = self._main_db()
            try:
                meta = job_meta(db, key) if db else None
            finally:
                if db:
                    db.close()
            if meta is None and not any(a["job_key"] == key for a in self.tracker.list()):
                raise TrackerError("that role is not in the database")
        self.tracker.set(job_key=key if app_id is None else None, app_id=app_id, status=status, note=note, applied_at=date, meta=meta)
        return self.snapshot()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, api: TrackerAPI | None = None, **kwargs):
        self.api = api
        super().__init__(*args, **kwargs)

    nostore = False

    def end_headers(self):
        if not self.nostore:
            self.send_header("Cache-Control", "no-cache")  # the page is rewritten after every run
        super().end_headers()

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # -- the tracker API ------------------------------------------------------------
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.nostore = True
        self.end_headers()
        self.wfile.write(body)

    def _is_api(self) -> bool:
        return urlsplit(self.path).path.startswith("/api/")

    def _api(self, method: str) -> None:
        api, path = self.api, urlsplit(self.path).path.rstrip("/")
        if api is None or path not in ("/api/tracker", "/api/tracker/set", "/api/tracker/delete"):
            return self._json(404, {"error": "not found"})
        if not api.host_ok(self.headers.get("Host")):
            return self._json(403, {"error": "unexpected Host header"})
        if method == "GET":
            return self._json(200, api.snapshot()) if path == "/api/tracker" else self._json(405, {"error": "use POST"})
        if path == "/api/tracker":
            return self._json(405, {"error": "use GET"})
        if not api.writable:
            return self._json(403, {"error": "saving is switched off on this server"})
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            return self._json(403, {"error": "cross-site request refused"})
        if not secrets.compare_digest(self.headers.get("X-RaiseRadar-Token", ""), api.token):
            return self._json(403, {"error": "bad token: reload the page"})
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            return self._json(415, {"error": "send JSON"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._json(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": "bad JSON"})
        try:
            self._json(200, api.apply(path.rsplit("/", 1)[1], body))
        except TrackerError as exc:
            self._json(400, {"error": str(exc)})

    def do_GET(self):
        if self._is_api():
            return self._api("GET")
        super().do_GET()

    def do_POST(self):
        if self._is_api():
            return self._api("POST")
        self._json(405, {"error": "not allowed"})


def make_server(directory, host: str, port: int, api: TrackerAPI | None = None) -> http.server.ThreadingHTTPServer:
    return http.server.ThreadingHTTPServer((host, port), functools.partial(Handler, directory=str(directory), api=api))


def ensure_page(cfg: Config, public: bool | None, tracker: bool | None = None) -> None:
    """Something to look at right away: the report of what is already saved, or the sample report on a fresh install."""
    from .cli import _has_real_data, build
    from .demo import write_demo
    from .report import write_report
    if _has_real_data(cfg):
        p, db, _fetcher = build(cfg, need_llm=False)
        try:
            write_report(p, public=public, tracker=tracker)
        finally:
            db.close()
    else:
        write_demo(cfg.path("general", "report_dir"))


class Scheduler(threading.Thread):
    """Runs the full pipeline and the jobs-only refresh when they are due. A failed run never stops the loop."""

    def __init__(self, cfg: Config, full_hours: float, jobs_hours: float, public: bool | None, stop: threading.Event,
                 tracker: bool | None = None):
        super().__init__(name="raiseradar-schedule", daemon=True)
        self.cfg, self.full_h, self.jobs_h, self.public, self.stop, self.tracker = cfg, full_hours, jobs_hours, public, stop, tracker
        now = time.time()
        age = self._last_full_age_days()
        self.last_full = now - age * 86400 if age is not None else 0.0       # 0 = never ran: due now
        self.last_jobs = self.last_full
        self.busy = False                                                     # True while a run is in progress

    def _last_full_age_days(self) -> float | None:
        from .db import DB
        path = self.cfg.path("general", "db_path")
        if not path.is_file():
            return None
        db = DB(path)
        try:
            return days_since(db.last_finished_run_started())
        finally:
            db.close()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.tick(time.time())
            except Exception:                                                 # keep serving and try again later
                log.exception("scheduled run failed")
            self.stop.wait(30)

    def tick(self, now: float) -> str | None:
        """Run whatever is due. Returns "full", "jobs" or None (used by the tests)."""
        if self.full_h and now - self.last_full >= self.full_h * 3600:
            self._attempt("full", self.run_full)
            return "full"
        if self.jobs_h and now - self.last_jobs >= self.jobs_h * 3600:
            self._attempt("jobs", self.run_jobs)
            return "jobs"
        return None

    def _attempt(self, kind: str, fn) -> None:
        self.busy = True
        try:
            fn()
        except Exception:
            # A failure usually means the network or a service is down: wait an hour before trying again, and
            # after a failed full run do not start a jobs refresh straight away either.
            if kind == "full":
                self.last_full = self._backoff(self.full_h)
            self.last_jobs = self._backoff(self.jobs_h)
            raise
        finally:
            self.busy = False
        now = time.time()
        self.last_jobs = now
        if kind == "full":
            self.last_full = now                                              # a full run refreshes the jobs too

    @staticmethod
    def _backoff(hours: float) -> float:
        """A 'last run' time that makes the job due again after RETRY_AFTER seconds."""
        return time.time() - max(0.0, hours * 3600 - RETRY_AFTER)

    def run_full(self) -> None:
        from .cli import cmd_run
        log.info("scheduled full run")
        args = argparse.Namespace(skip_news=not self.cfg.openrouter_key, skip_yc=False, limit=0, open=False, public=bool(self.public), tracker=self.tracker)
        cmd_run(args, self.cfg)

    def run_jobs(self) -> None:
        from .cli import build
        from .report import write_report
        log.info("scheduled jobs refresh")
        p, db, _fetcher = build(self.cfg, need_llm=False)
        try:
            p.fetch_jobs()
            write_report(p, public=self.public, tracker=self.tracker)
        finally:
            db.close()


def tracker_mode(host: str, public: bool, tracker: bool | None) -> bool:
    """Should this server let you save applications from the page? Yes on this computer only, unless you choose otherwise
    (--tracker / --no-tracker / RAISERADAR_TRACKER=1 for a container whose published port is limited to localhost).
    Never for a public page."""
    if public:
        return False
    if tracker is not None:
        return tracker
    return is_loopback(host) or os.environ.get("RAISERADAR_TRACKER", "").strip() == "1"


def serve(cfg: Config, host: str, port: int, full_hours: float, jobs_hours: float, public: bool | None = None,
          ready: threading.Event | None = None, tracker: bool | None = None) -> None:
    report_dir = cfg.path("general", "report_dir")
    report_dir.mkdir(parents=True, exist_ok=True)
    is_public = bool(public if public is not None else cfg["report"]["public"])
    tracking = tracker_mode(host, is_public, tracker)
    api = None if is_public else TrackerAPI(cfg, host, writable=tracking)
    ensure_page(cfg, public, tracking)
    server = make_server(report_dir, host, port, api)
    stop = threading.Event()
    scheduler = None
    if full_hours or jobs_hours:
        if full_hours and not cfg.openrouter_key:
            print("No OPENROUTER_API_KEY: scheduled runs skip the funding news (Y Combinator companies and ones you added still work).")
        scheduler = Scheduler(cfg, full_hours, jobs_hours, public, stop, tracking)
        scheduler.start()
        print(f"Schedule: full run every {full_hours:g} h, jobs refresh every {jobs_hours:g} h (0 = off). Logs below show each run.")
    else:
        print("No schedule: serving the report as it is. Refresh it with `raiseradar run`.")
    shown = "localhost" if host in ("", "0.0.0.0", "::") else host
    print(f"Serving {report_dir} at http://{shown}:{server.server_address[1]}/   (Ctrl-C to stop)")
    if not is_loopback(host):
        print("Note: there is no login. Anyone who can reach this port can read the report"
              + (" and your saved applications." if tracking else "."))
    if tracking:
        print("Application tracking is on: use the status menus on the page (data/applications.db).")
    elif not is_public:
        print("Application tracking is off here (the page is shown to others, or --no-tracker). Use `raiseradar track` in a terminal.")
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())   # `docker stop`
    if ready:
        ready.set()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        if scheduler is not None and scheduler.busy:
            # The run's worker threads would keep the process alive for minutes. Everything is saved step by step,
            # so stop now; the next start continues where this run stopped.
            print("Stopping while a run is in progress; the next start continues where it stopped.", flush=True)
            sys.stdout.flush()
            os._exit(0)
