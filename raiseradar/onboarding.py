"""First-run helpers: check an OpenRouter key, store it in .env, and the guided `start` flow."""
from __future__ import annotations

import getpass
import os
import stat
import sys
from pathlib import Path

import httpx

KEY_URL = "https://openrouter.ai/api/v1/auth/key"
KEY_PAGE = "https://openrouter.ai/keys"
COLD_RUN = "about 30 minutes"      # what a first real run costs, said the same way everywhere


def mask(key: str) -> str:
    return key[:8] + "..." + key[-4:] if len(key) > 14 else "***"


def check_api_key(key: str, transport: httpx.BaseTransport | None = None) -> tuple[str, str]:
    """Ask OpenRouter whether a key is valid (this call is free and uses no model).
    Returns (status, detail) with status "ok", "invalid" (rejected) or "unreachable" (could not ask)."""
    try:
        with httpx.Client(timeout=15, transport=transport) as client:
            r = client.get(KEY_URL, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        return "unreachable", f"could not reach OpenRouter ({exc.__class__.__name__})"
    if r.status_code in (401, 403):
        return "invalid", "OpenRouter rejected this key"
    if r.status_code != 200:
        return "unreachable", f"OpenRouter answered HTTP {r.status_code}"
    try:
        info = (r.json() or {}).get("data") or {}
    except ValueError:
        info = {}
    return "ok", "free account" if info.get("is_free_tier") else "account with credits"


def save_env_key(env_path: Path, key: str) -> None:
    """Write OPENROUTER_API_KEY into .env, keeping every other line, and make the file private (owner only)."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == "OPENROUTER_API_KEY":
            if not done:
                out.append(f"OPENROUTER_API_KEY={key}")
                done = True
            continue                      # drop duplicates of the same setting
        out.append(line)
    if not done:
        out.append(f"OPENROUTER_API_KEY={key}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.environ["OPENROUTER_API_KEY"] = key


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_secret(prompt: str) -> str:
    try:
        return getpass.getpass(prompt).strip()
    except EOFError:
        return ""


def setup_key(env_path: Path, *, current: str = "", replace: bool = False, transport: httpx.BaseTransport | None = None,
              say=print, key_reader=ask_secret) -> bool:
    """Guided key setup. Returns True when a usable key is in place afterwards."""
    if current and not replace:
        status, detail = check_api_key(current, transport)
        if status == "ok":
            say(f"Your OpenRouter key ({mask(current)}) works ({detail}).")
            return True
        say(f"The key in your environment ({mask(current)}) does not work: {detail}.")
    say(f"\nYou need a free OpenRouter key: sign in at {KEY_PAGE}, click 'Create key', copy it.")
    say("It is only used to read funding headlines with free AI models. It is stored in .env on this computer and sent only to openrouter.ai.")
    for attempt in range(3):
        key = key_reader("Paste the key here (input is hidden; press Enter to skip): ")
        if not key:
            say("Skipped. Run `raiseradar setup` whenever you have a key.")
            return False
        status, detail = check_api_key(key, transport)
        if status == "invalid":
            say(f"{detail}. Check that you copied the whole key (it starts with sk-or-).")
            continue
        if status == "unreachable":
            say(f"Could not verify the key ({detail}). Saving it anyway; the first run will show whether it works.")
        else:
            say(f"Key accepted ({detail}).")
        save_env_key(env_path, key)
        say(f"Saved to {env_path} (readable only by you).")
        return True
    say("Giving up after 3 tries.")
    return False
