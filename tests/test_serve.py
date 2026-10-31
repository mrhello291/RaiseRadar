"""`raiseradar serve`: the web page, the built-in schedule, and how the command line turns into settings."""
import threading
import urllib.error
import urllib.request

import pytest

from raiseradar import cli, serve as serve_mod
from raiseradar.config import Config
from raiseradar.db import DB


def _cfg(tmp_path):
    return Config(root=tmp_path)


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, {}, b""


def test_server_serves_only_the_report_folder(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "index.html").write_text("<h1>hello</h1>")
    (tmp_path / "secret.txt").write_text("not for the web")
    server = serve_mod.make_server(tmp_path / "reports", "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, headers, body = _get(base + "/")
        assert status == 200 and b"hello" in body and headers["Cache-Control"] == "no-cache"
        assert _get(base + "/%2e%2e/secret.txt")[0] == 404
        assert _get(base + "/nothing.html")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_first_page_is_the_sample_report_then_the_real_one(tmp_path):
    cfg = _cfg(tmp_path)
    serve_mod.ensure_page(cfg, None)
    assert '"demo": {' in (tmp_path / "reports" / "index.html").read_text() or '"demo":{' in (tmp_path / "reports" / "index.html").read_text()
    db = DB(tmp_path / "data" / "companyfinder.db")
    db.upsert_company("Real Co", origin="manual")
    db.close()
    serve_mod.ensure_page(cfg, None)                                  # real data now: the real report replaces the sample
    page = (tmp_path / "reports" / "index.html").read_text()
    assert "Real Co" in page and "Sample data" in page and '"demo":false' in page.replace(" ", "")


def _scheduler(tmp_path, monkeypatch, full=12, jobs=4):
    calls = []
    monkeypatch.setattr(serve_mod.Scheduler, "run_full", lambda self: calls.append("full"))
    monkeypatch.setattr(serve_mod.Scheduler, "run_jobs", lambda self: calls.append("jobs"))
    return serve_mod.Scheduler(_cfg(tmp_path), full, jobs, None, threading.Event()), calls


def test_schedule_runs_each_thing_when_due(tmp_path, monkeypatch):
    s, calls = _scheduler(tmp_path, monkeypatch)
    t0 = 1_000_000.0
    monkeypatch.setattr(serve_mod.time, "time", lambda: t0)
    assert s.tick(t0) == "full"                                       # a fresh install has never run: due now
    assert s.tick(t0 + 60) is None
    monkeypatch.setattr(serve_mod.time, "time", lambda: t0 + 4 * 3600)
    assert s.tick(t0 + 4 * 3600) == "jobs"
    monkeypatch.setattr(serve_mod.time, "time", lambda: t0 + 8 * 3600)
    assert s.tick(t0 + 8 * 3600) == "jobs"
    monkeypatch.setattr(serve_mod.time, "time", lambda: t0 + 12 * 3600)
    assert s.tick(t0 + 12 * 3600) == "full"                           # the full run also refreshes jobs, so no extra jobs run
    assert calls == ["full", "jobs", "jobs", "full"]


def test_zero_switches_a_job_off(tmp_path, monkeypatch):
    s, calls = _scheduler(tmp_path, monkeypatch, full=0, jobs=0)
    assert s.tick(10**9) is None and calls == []
    s, calls = _scheduler(tmp_path, monkeypatch, full=0, jobs=4)
    assert s.tick(10**9) == "jobs"


def test_a_recent_run_in_the_database_is_not_repeated_after_a_restart(tmp_path, monkeypatch):
    db = DB(tmp_path / "data" / "companyfinder.db")
    db.finish_run(db.start_run(), {})
    db.close()
    s, calls = _scheduler(tmp_path, monkeypatch)
    assert s.tick(serve_mod.time.time()) is None and calls == []


def test_a_run_that_was_killed_midway_does_not_count(tmp_path, monkeypatch):
    db = DB(tmp_path / "data" / "companyfinder.db")
    db.start_run()                                                    # started, never finished: the container was stopped
    db.close()
    s, calls = _scheduler(tmp_path, monkeypatch)
    assert s.tick(serve_mod.time.time()) == "full"                    # so it picks up again straight away


def test_a_failed_run_is_retried_in_an_hour_not_every_tick(tmp_path, monkeypatch):
    s, calls = _scheduler(tmp_path, monkeypatch)
    def boom(self):
        calls.append("full")
        raise RuntimeError("network down")
    monkeypatch.setattr(serve_mod.Scheduler, "run_full", boom)
    t0 = 1_000_000.0
    monkeypatch.setattr(serve_mod.time, "time", lambda: t0)
    with pytest.raises(RuntimeError):
        s.tick(t0)
    assert s.tick(t0 + 60) is None                                    # not hammering the network
    monkeypatch.setattr(serve_mod.time, "time", lambda: t0 + 3600)
    with pytest.raises(RuntimeError):
        s.tick(t0 + 3601)
    assert calls == ["full", "full"]


def test_serve_command_line_settings(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(serve_mod, "serve", lambda cfg, host, port, full, jobs, public=None, ready=None: seen.append((host, port, full, jobs, public)))
    cfgp = tmp_path / "config.toml"
    cfgp.write_text("")
    cli.main(["-c", str(cfgp), "serve"])
    cli.main(["-c", str(cfgp), "serve", "--host", "0.0.0.0", "--port", "9000", "--full-every", "6", "--jobs-every", "0", "--public"])
    cli.main(["-c", str(cfgp), "serve", "--no-schedule"])
    cfgp.write_text("[serve]\nport = 7000\nfull_every_hours = 24\n")
    cli.main(["-c", str(cfgp), "serve"])
    assert seen == [("127.0.0.1", 8080, 12, 4, None), ("0.0.0.0", 9000, 6, 0, True), ("127.0.0.1", 8080, 0, 0, None), ("127.0.0.1", 7000, 24, 4, None)]
