"""Configuration loading and validation.

Secrets come from the environment (via a gitignored `.env`), never from source.
League rules come from `league_rules.yaml`, which is committed.

The point of validating up front is that a missing variable should fail here,
with a message naming what to do about it, rather than surfacing as an opaque
401 five calls deep into a sync.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
RULES_PATH = PACKAGE_ROOT / "league_rules.yaml"

DEFAULT_REDIRECT_URI = "https://localhost:8000"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or unusable."""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Runtime configuration assembled from the environment."""

    client_id: str | None
    client_secret: str | None
    redirect_uri: str
    league_id_2025: str | None
    league_id_2026: str | None
    db_path: Path
    enable_sleeper: bool
    enable_espn: bool
    enable_weather: bool
    rules: dict = field(repr=False, default_factory=dict)

    # -- credential helpers -------------------------------------------------

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def require_credentials(self) -> None:
        """Raise with actionable text if the Yahoo app credentials are absent."""
        missing = [
            name
            for name, value in (
                ("YAHOO_CLIENT_ID", self.client_id),
                ("YAHOO_CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}.\n"
                "Copy .env.example to .env and fill them in. See SETUP_YAHOO_APP.md "
                "for how to create the Yahoo app that issues them."
            )

    def league_id(self, season: int) -> str:
        """Return the configured league id for a season, or explain what's missing."""
        mapping = {2025: self.league_id_2025, 2026: self.league_id_2026}
        if season not in mapping:
            raise ConfigError(
                f"No league id configured for season {season}. "
                f"Known seasons: {sorted(mapping)}."
            )
        value = mapping[season]
        if not value:
            raise ConfigError(
                f"LEAGUE_ID_{season} is not set in .env.\n"
                f"Find it with: python scripts/fetch_league.py --discover --season {season}"
            )
        return value

    def league_key(self, season: int, game_key: str) -> str:
        """Build a Yahoo league key.

        Note the separator is a lowercase L, not the digit 1: `nfl.l.123456`.
        """
        return f"{game_key}.l.{self.league_id(season)}"

    # -- rules helpers ------------------------------------------------------

    @property
    def keeper_rules(self) -> dict:
        return self.rules.get("keepers", {})

    @property
    def scoring_rules(self) -> dict:
        return self.rules.get("scoring", {})

    @property
    def roster_rules(self) -> dict:
        return self.rules.get("roster", {})


def load_rules(path: Path | None = None) -> dict:
    """Load and lightly validate league_rules.yaml."""
    rules_path = path or RULES_PATH
    if not rules_path.exists():
        raise ConfigError(f"League rules file not found at {rules_path}")

    with rules_path.open() as fh:
        rules = yaml.safe_load(fh)

    if not isinstance(rules, dict):
        raise ConfigError(f"{rules_path} did not parse to a mapping.")

    for section in ("keepers", "scoring", "roster"):
        if section not in rules:
            raise ConfigError(f"{rules_path} is missing the '{section}' section.")

    return rules


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Load configuration once per process."""
    load_dotenv(PROJECT_ROOT / ".env")

    db_path = Path(os.environ.get("FFL_DB_PATH", "data/ffl.db"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    return Config(
        client_id=os.environ.get("YAHOO_CLIENT_ID") or None,
        client_secret=os.environ.get("YAHOO_CLIENT_SECRET") or None,
        redirect_uri=os.environ.get("YAHOO_REDIRECT_URI") or DEFAULT_REDIRECT_URI,
        league_id_2025=os.environ.get("LEAGUE_ID_2025") or None,
        league_id_2026=os.environ.get("LEAGUE_ID_2026") or None,
        db_path=db_path,
        enable_sleeper=_env_flag("ENABLE_SLEEPER", True),
        enable_espn=_env_flag("ENABLE_ESPN", False),
        enable_weather=_env_flag("ENABLE_WEATHER", True),
        rules=load_rules(),
    )


def mask(secret: str | None) -> str:
    """Render a secret safe for terminal output: `dj0yJm…a1b2` or `<not set>`."""
    if not secret:
        return "<not set>"
    if len(secret) <= 12:
        return f"{secret[:2]}{'…' * 3}"
    return f"{secret[:6]}…{secret[-4:]}"
