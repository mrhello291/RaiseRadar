"""`raiseradar serve`: show the report on a small web page and, optionally, refresh it on a schedule.

Meant for a Docker container or a machine that stays on. No cron is needed: a background thread runs the full pipeline
every `full_every_hours` and a jobs-only refresh every `jobs_every_hours`, one at a time."""
from __future__ import annotations

import argparse
import functools
import http.server
import logging
import os
import signal
import sys
import threading
import time

from .config import Config
from .util import days_since

log = logging.getLogger("raiseradar.serve")
RETRY_AFTER = 3600          # seconds to wait after a scheduled run failed before trying again


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")      # the page is rewritten after every run
        super().end_headers()

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)


def make_server(directory, host: str, port: int) -> http.server.ThreadingHTTPServer:
    return http.server.ThreadingHTTPServer((host, port), functools.partial(Handler, directory=str(directory)))


def ensure_page(cfg: Config, public: bool | None) -> None:
    """Something to look at right away: the report of what is already saved, or the sample report on a fresh install."""
    from .cli import _has_real_data, build
    from .demo import write_demo
    from .report import write_report
    if _has_real_data(cfg):
        p, db, _fetcher = build(cfg, need_llm=False)
        try:
            write_report(p, public=public)
        finally:
            db.close()
    else:
        write_demo(cfg.path("general", "report_dir"))


class Scheduler(threading.Thread):
    """Runs the full pipeline and the jobs-only refresh when they are due. A failed run never stops the loop."""

    def __init__(self, cfg: Config, full_hours: float, jobs_hours: float, public: bool | None, stop: threading.Event):
        super().__init__(name="raiseradar-schedule", daemon=True)
        self.cfg, self.full_h, self.jobs_h, self.public, self.stop = cfg, full_hours, jobs_hours, public, stop
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
        args = argparse.Namespace(skip_news=not self.cfg.openrouter_key, skip_yc=False, limit=0, open=False, public=bool(self.public))
        cmd_run(args, self.cfg)

    def run_jobs(self) -> None:
        from .cli import build
        from .report import write_report
        log.info("scheduled jobs refresh")
        p, db, _fetcher = build(self.cfg, need_llm=False)
        try:
            p.fetch_jobs()
            write_report(p, public=self.public)
        finally:
            db.close()


def serve(cfg: Config, host: str, port: int, full_hours: float, jobs_hours: float, public: bool | None = None,
          ready: threading.Event | None = None) -> None:
    report_dir = cfg.path("general", "report_dir")
    report_dir.mkdir(parents=True, exist_ok=True)
    ensure_page(cfg, public)
    server = make_server(report_dir, host, port)
    stop = threading.Event()
    scheduler = None
    if full_hours or jobs_hours:
        if full_hours and not cfg.openrouter_key:
            print("No OPENROUTER_API_KEY: scheduled runs skip the funding news (Y Combinator companies and ones you added still work).")
        scheduler = Scheduler(cfg, full_hours, jobs_hours, public, stop)
        scheduler.start()
        print(f"Schedule: full run every {full_hours:g} h, jobs refresh every {jobs_hours:g} h (0 = off). Logs below show each run.")
    else:
        print("No schedule: serving the report as it is. Refresh it with `raiseradar run`.")
    shown = "localhost" if host in ("", "0.0.0.0", "::") else host
    print(f"Serving {report_dir} at http://{shown}:{server.server_address[1]}/   (Ctrl-C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Note: there is no login. Anyone who can reach this port can read the report.")
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
