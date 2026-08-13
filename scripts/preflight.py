#!/usr/bin/env python3
"""Connectivity and credential diagnostic.

Run this first, and run it again whenever something stops working. Each check is
independent, so the output tells you exactly which link in the chain is broken
rather than just that something is.

    python scripts/preflight.py

Exit code is 0 if everything required for a keeper sync is working, 1 otherwise.
"""

from __future__ import annotations



import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffl.config import ConfigError, get_config, mask  # noqa: E402

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"

SYMBOLS = {PASS: "\033[32m✓\033[0m", FAIL: "\033[31m✗\033[0m", WARN: "\033[33m!\033[0m"}

# (label, host, required-for-keeper-sync)
HOSTS = [
    ("Yahoo OAuth", "api.login.yahoo.com", True),
    ("Yahoo Fantasy API", "fantasysports.yahooapis.com", True),
    ("nflverse (GitHub)", "github.com", True),
    ("nflverse (raw)", "raw.githubusercontent.com", True),
    ("Sleeper", "api.sleeper.app", False),
    ("ESPN", "site.api.espn.com", False),
    ("Open-Meteo", "api.open-meteo.com", False),
]

YAHOO_PROBE = "https://fantasysports.yahooapis.com/fantasy/v2/game/nfl"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.blocking = 0

    def add(self, status: str, label: str, detail: str = "", blocking: bool = False) -> None:
        self.rows.append((status, label, detail))
        print(f"  {SYMBOLS[status]} {label:<34} {detail}")
        if status == FAIL and blocking:
            self.blocking += 1


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def check_python(report: Report) -> None:
    section("Environment")
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 10)
    report.add(PASS if ok else FAIL, "Python version", version, blocking=True)

    in_venv = sys.prefix != sys.base_prefix
    report.add(
        PASS if in_venv else WARN,
        "Virtualenv active",
        sys.prefix if in_venv else "not in a venv (recommended: source venv/bin/activate)",
    )


def check_packages(report: Report) -> None:
    for label, module, blocking in [
        ("yahoofantasy installed", "yahoofantasy", True),
        ("nflreadpy installed", "nflreadpy", True),
        ("Flask installed", "flask", True),
    ]:
        try:
            __import__(module)
            report.add(PASS, label, "")
        except ImportError as exc:
            report.add(FAIL, label, str(exc), blocking=blocking)


def check_config(report: Report):
    section("Configuration")
    try:
        config = get_config()
    except ConfigError as exc:
        report.add(FAIL, ".env / league_rules.yaml", str(exc), blocking=True)
        return None

    env_file = Path(__file__).resolve().parent.parent / ".env"
    report.add(
        PASS if env_file.exists() else FAIL,
        ".env present",
        str(env_file) if env_file.exists() else "missing — run: cp .env.example .env",
        blocking=True,
    )
    report.add(
        PASS if config.client_id else FAIL,
        "YAHOO_CLIENT_ID",
        mask(config.client_id),
        blocking=True,
    )
    report.add(
        PASS if config.client_secret else FAIL,
        "YAHOO_CLIENT_SECRET",
        mask(config.client_secret),
        blocking=True,
    )
    report.add(PASS, "Redirect URI", config.redirect_uri)
    if not config.redirect_uri.startswith("https://"):
        report.add(
            WARN,
            "Redirect URI scheme",
            "not https — yahoofantasy defaults to HTTPS; pass --redirect-http if intended",
        )

    for season in (2025, 2026):
        try:
            report.add(PASS, f"LEAGUE_ID_{season}", config.league_id(season))
        except ConfigError:
            report.add(
                WARN,
                f"LEAGUE_ID_{season}",
                f"not set — find it with: python scripts/fetch_league.py --discover --season {season}",
            )

    report.add(PASS, "League rules loaded", f"{len(config.scoring_rules)} scoring sections")
    return config


def probe_host(url: str) -> tuple[bool, str]:
    """Probe a host over HTTPS the same way the real clients reach it.

    A raw TCP/TLS socket is the wrong test here: sockets ignore HTTPS_PROXY, so
    on a proxied network they succeed against hosts that every actual HTTP client
    is blocked from — a false PASS, which is worse than no check at all.

    Any HTTP status back means the host is reachable; 401/403/404 from the
    destination still prove the path works. Only a transport failure counts as
    unreachable, and a proxy CONNECT rejection is called out by name since that
    means network policy, not an outage.
    """
    try:
        urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15)
        return True, "reachable"
    except HTTPError as exc:
        return True, f"reachable (HTTP {exc.code})"
    except URLError as exc:
        reason = str(exc.reason)
        if "tunnel connection failed" in reason.lower():
            return False, "blocked by network egress policy"
        return False, f"unreachable ({reason[:48]})"
    except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
        return False, f"unreachable ({type(exc).__name__})"


def check_hosts(report: Report) -> None:
    section("Network reachability")
    for label, host, blocking in HOSTS:
        reachable, detail = probe_host(f"https://{host}")
        report.add(
            PASS if reachable else (FAIL if blocking else WARN),
            label,
            f"{host} — {detail}",
            blocking=blocking,
        )


def check_yahoo_endpoint(report: Report) -> None:
    """An unauthenticated 401 is the healthy answer here.

    It proves the endpoint is live and the network path works, without needing
    any credentials — which is exactly what you want to know while a Yahoo app
    is still pending approval.
    """
    section("Yahoo API")
    try:
        urlopen(Request(YAHOO_PROBE, headers={"User-Agent": "Mozilla/5.0"}), timeout=15)
        report.add(WARN, "Unauthenticated probe", "returned 200 — unexpected but harmless")
    except HTTPError as exc:
        if exc.code == 401:
            report.add(PASS, "Unauthenticated probe", "401 as expected — endpoint reachable")
        else:
            report.add(WARN, "Unauthenticated probe", f"HTTP {exc.code}")
    except URLError as exc:
        report.add(FAIL, "Unauthenticated probe", f"unreachable ({exc.reason})", blocking=True)


def check_token(report: Report) -> None:
    try:
        from yahoofantasy import Context
    except ImportError:
        return

    try:
        ctx = Context()
    except Exception as exc:
        report.add(
            FAIL,
            "Stored OAuth token",
            f"none found ({type(exc).__name__}) — run: yahoofantasy login",
            blocking=True,
        )
        return

    report.add(PASS, "Stored OAuth token", "found")

    try:
        ctx.make_request("game/nfl")
        report.add(PASS, "Authenticated request", "200 — fully wired")
    except Exception as exc:
        report.add(
            FAIL,
            "Authenticated request",
            f"{type(exc).__name__}: {str(exc)[:70]}",
            blocking=True,
        )


def main() -> int:
    print("\n\033[1mFantasy Football Friend — preflight\033[0m")

    report = Report()
    check_python(report)
    check_packages(report)
    check_config(report)
    check_hosts(report)
    check_yahoo_endpoint(report)
    check_token(report)

    print()
    if report.blocking:
        print(
            f"\033[31m{report.blocking} blocking issue(s).\033[0m "
            "Fix the ✗ rows above, then re-run.\n"
        )
        return 1

    print("\033[32mAll required checks passed.\033[0m Next: "
          "python scripts/fetch_league.py --discover --season 2025\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
