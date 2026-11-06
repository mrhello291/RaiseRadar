"""`raiseradar outreach schedule`: run the bot by itself every N minutes with a systemd user timer (Linux).

A timer, not cron, because a laptop sleeps: with `Persistent=true` a run missed while it slept happens on wake-up.
Without systemd (macOS, containers) it prints the equivalent crontab lines instead.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


def calendar_every(minutes: int) -> str:
    """systemd OnCalendar for 'every N minutes', N dividing an hour or a day."""
    if minutes < 60 and 60 % minutes == 0:
        return f"*:0/{minutes}"
    if minutes % 60 == 0 and 24 % (minutes // 60) == 0:
        return f"0/{minutes // 60}:00"
    raise ValueError("--every must divide an hour (5, 10, 15, 20, 30) or a day in whole hours (60, 120, 180, 240, ...)")


def jobs(root: Path, python: str, every: int, refresh: bool) -> dict[str, tuple[str, str, str]]:
    """name -> (description, command, OnCalendar)."""
    cfg = root / "config.toml"
    scripts = Path(__file__).resolve().parent.parent / "scripts"     # they cd to the repository and use its config
    out = {"raiseradar-outreach": ("RaiseRadar outreach: read your taps, draft, ask on the phone, send what you approved",
                                   f'"{python}" -m raiseradar -c "{cfg}" outreach run', calendar_every(every))}
    if refresh:   # new roles for the bot to find: the full run twice a day, a jobs-only refresh every 4 hours
        out["raiseradar-run"] = ("RaiseRadar full run (news, companies, jobs)", f'"{scripts / "run_daily.sh"}"', "*-*-* 07:30,19:30")
        out["raiseradar-jobs"] = ("RaiseRadar jobs refresh", f'"{scripts / "refresh_jobs.sh"}"', "0/4:00")
    return out


def units(name: str, desc: str, cmd: str, when: str, root: Path) -> tuple[str, str]:
    service = (f"[Unit]\nDescription={desc}\nAfter=network-online.target\nWants=network-online.target\n\n"
               f"[Service]\nType=oneshot\nWorkingDirectory={root}\nExecStart=/bin/sh -c '{cmd}'\n")
    timer = (f"[Unit]\nDescription={desc} (timer)\n\n[Timer]\nOnCalendar={when}\nPersistent=true\nRandomizedDelaySec=60\n\n"
             f"[Install]\nWantedBy=timers.target\n")
    return service, timer


def cron_lines(plan: dict[str, tuple[str, str, str]], every: int, root: Path) -> list[str]:
    spec = {"raiseradar-outreach": f"*/{every} * * * *" if every < 60 else f"0 */{every // 60} * * *",
            "raiseradar-run": "30 7,19 * * *", "raiseradar-jobs": "0 */4 * * *"}
    return [f"{spec[n]}  cd \"{root}\" && {cmd} >> \"{root / 'data' / (n + '.log')}\" 2>&1" for n, (_d, cmd, _w) in plan.items()]


def has_systemd() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()


def install(root: Path, python: str, every: int, refresh: bool, run=subprocess.run, unit_dir: Path = UNIT_DIR) -> list[str]:
    """Write and start the timers. Returns the timer names."""
    plan = jobs(root, python, every, refresh)
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, (desc, cmd, when) in plan.items():
        service, timer = units(name, desc, cmd, when, root)
        (unit_dir / f"{name}.service").write_text(service, encoding="utf-8")
        (unit_dir / f"{name}.timer").write_text(timer, encoding="utf-8")
    run(["systemctl", "--user", "daemon-reload"], check=True)
    names = [f"{n}.timer" for n in plan]
    run(["systemctl", "--user", "enable", "--now", *names], check=True)
    return names


def remove(run=subprocess.run, unit_dir: Path = UNIT_DIR) -> list[str]:
    gone = []
    for name in ("raiseradar-outreach", "raiseradar-run", "raiseradar-jobs"):
        if (unit_dir / f"{name}.timer").exists():
            run(["systemctl", "--user", "disable", "--now", f"{name}.timer"], check=False)
            for ext in ("timer", "service"):
                (unit_dir / f"{name}.{ext}").unlink(missing_ok=True)
            gone.append(f"{name}.timer")
    run(["systemctl", "--user", "daemon-reload"], check=False)
    return gone
