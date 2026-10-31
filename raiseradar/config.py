"""Configuration loading: defaults < config.toml < environment (.env)."""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {
        "db_path": "data/companyfinder.db",
        "report_dir": "reports",
        "recent_days": 150,
        "new_days": 3,
        "contact": "",
    },
    "report": {
        "public": False,
        "jd_excerpt_chars": 700,
    },
    "funding": {
        "target_rounds": ["Series B", "Series C"],
        "min_amount_usd_m": 0,
        "max_amount_usd_m": 0,
    },
    "news": {
        "lookback_days": 14,
        "editions": ["US:US:en", "GB:GB:en", "IN:IN:en", "SG:SG:en"],
        "extra_feeds": [],
        "require_round_keyword": True,
    },
    "yc": {
        "enabled": True,
        "min_team_size": 25,
        "max_team_size": 800,
        "max_batch_year": 2023,
        "max_companies": 120,
    },
    "llm": {
        "models": ["google/gemma-4-31b-it:free", "openrouter/free"],
        "batch_size": 10,
        "max_calls_per_run": 30,
        "requests_per_minute": 12,
        "max_tokens": 8000,
        "reasoning_effort": "low",
    },
    "http": {"timeout": 15, "min_delay_per_host": 0.3, "respect_robots": True, "workers": 8},
    "jobs": {"max_jobs_per_company": 300, "rediscover_days": 14},
    "profile": {
        "roles": [],
        "exclude_titles": [],
        "locations": [],
    },
}


@dataclass
class Config:
    data: dict[str, dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    root: Path = field(default_factory=Path.cwd)

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.data[section]

    def path(self, section: str, key: str) -> Path:
        p = Path(self.data[section][key])
        return p if p.is_absolute() else self.root / p

    @property
    def openrouter_key(self) -> str:
        return os.environ.get("OPENROUTER_API_KEY", "").strip()

    @property
    def user_agent(self) -> str:
        contact = os.environ.get("RAISERADAR_CONTACT", "").strip() or self.data["general"].get("contact") or "personal-use"
        return f"Mozilla/5.0 (compatible; RaiseRadar/0.1; +{contact})"


def load_dotenv(path: Path) -> None:
    """Tiny .env reader (KEY=VALUE per line); does not override real environment variables."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg_path = Path(path) if path else Path.cwd() / "config.toml"
    root = cfg_path.resolve().parent
    load_dotenv(root / ".env")
    cfg = Config(root=root)
    if cfg_path.is_file():
        with open(cfg_path, "rb") as fh:
            user = tomllib.load(fh)
        for section, values in user.items():
            if isinstance(values, dict):
                cfg.data.setdefault(section, {}).update(values)
    env_models = os.environ.get("OPENROUTER_MODEL", "").strip()
    if env_models:   # .env wins over config.toml: comma-separated, first = preferred, rest = fallbacks
        cfg.data["llm"]["models"] = [m.strip() for m in env_models.split(",") if m.strip()]
    return cfg
