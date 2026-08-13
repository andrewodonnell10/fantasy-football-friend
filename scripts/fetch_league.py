#!/usr/bin/env python3
"""Pull league and NFL data into SQLite.

    # 1. Find your league ids and put them in .env
    python scripts/fetch_league.py --discover --season 2025

    # 2. Pull the season that determines keeper eligibility
    python scripts/fetch_league.py --sync --season 2025

    # 3. Pull 2026 for the draft date, and the nflverse analytics backbone
    python scripts/fetch_league.py --sync --season 2026
    python scripts/fetch_league.py --sync-nflverse --season 2025

    # Check Yahoo's live settings against docs/League_Rules_And_Settings.md
    python scripts/fetch_league.py --check-settings --season 2026
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffl import db  # noqa: E402
from ffl.config import ConfigError, get_config  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def cmd_discover(season: int) -> int:
    """List the authenticated user's leagues so the right id can be identified."""
    from ffl.sources import yahoo

    leagues = yahoo.discover_leagues(season)
    if not leagues:
        print(f"No leagues found for {season}. Is that the right season?")
        return 1

    print(f"\nLeagues for the {season} season:\n")
    for league in leagues:
        print(f"  {league['name']}")
        print(f"    league_id  : {league['league_id']}   <- put this in .env")
        print(f"    league_key : {league['league_key']}")
        print(f"    teams      : {league['num_teams']}   scoring: {league['scoring_type']}")
        print()

    print(f"Add to .env:  LEAGUE_ID_{season}={leagues[0]['league_id']}\n")
    return 0


def cmd_sync(season: int, save_fixtures: bool) -> int:
    """Pull one Yahoo season into SQLite."""
    from ffl.sources import yahoo

    config = get_config()
    league_id = config.league_id(season)

    with db.session() as conn:
        results = yahoo.sync_season(conn, season, league_id)

    print(f"\nSynced Yahoo {season} (league {league_id}):")
    for key, value in results.items():
        print(f"  {key:16s} {value}")

    if save_fixtures:
        _save_fixtures(season)
    print()
    return 0


def _save_fixtures(season: int) -> None:
    """Dump the yahoofantasy response cache into tests/fixtures/.

    The fixtures committed with this project are hand-written from documented
    response shapes. They verify our own logic, not our reading of Yahoo's
    format. Replacing them with real captures on first successful auth is what
    makes the parsing tests meaningful.
    """
    cache = Path.home() / ".yahoofantasy"
    if not cache.exists():
        print("\n  No yahoofantasy cache found; nothing to save as fixtures.")
        return

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    target = FIXTURE_DIR / f"yahoo_{season}"
    target.mkdir(exist_ok=True)

    count = 0
    for item in cache.rglob("*"):
        if item.is_file():
            (target / item.name).write_bytes(item.read_bytes())
            count += 1
    print(f"\n  Saved {count} real Yahoo responses to {target}")
    print("  Re-verify the parsing tests against these — they may reveal shape "
          "differences from the hand-written fixtures.")


def cmd_sync_nflverse(season: int) -> int:
    """Pull the nflverse analytics backbone. Needs no Yahoo credentials."""
    from ffl.sources import nflverse

    with db.session() as conn:
        results = nflverse.sync_all(conn, season)
        print(f"\nSynced nflverse {season}:")
        for key, value in results.items():
            print(f"  {key:16s} {value:>8,} rows")

        coverage = nflverse.link_yahoo_players(conn)
        if coverage["yahoo_players"]:
            print("\nYahoo -> nflverse identity coverage:")
            print(f"  matched      {coverage['matched']:>6,}")
            print(f"  unmatched    {coverage['unmatched']:>6,}")
            print(f"  defenses     {coverage['team_defenses']:>6,}  (no nflverse row expected)")
            print(f"  coverage     {coverage['coverage_pct']:>6}%")
            if coverage["unmatched"]:
                print("\n  Unmatched (first few):")
                for row in nflverse.unmatched_players(conn, limit=10):
                    print(f"    {row['name']} ({row['position']}, {row['nfl_team']})")
    print()
    return 0


def cmd_check_settings(season: int) -> int:
    """Compare Yahoo's configured scoring against the league rules document."""
    from ffl import settings_check
    from ffl.sources import yahoo

    config = get_config()
    ctx = yahoo.get_context()
    league_key = None

    with db.session() as conn:
        row = conn.execute(
            "SELECT league_key FROM leagues WHERE season = ?", (season,)
        ).fetchone()
        if row:
            league_key = row["league_key"]

    if not league_key:
        print(f"No {season} league in the database yet. Run --sync --season {season} first.")
        return 1

    catalogue = settings_check.parse_stat_catalogue(
        ctx._load_or_fetch("stat_categories.nfl", "game/nfl/stat_categories")
    )
    modifiers = settings_check.parse_stat_modifiers(
        yahoo.fetch_settings(ctx, league_key)
    )
    differences = settings_check.compare(config.scoring_rules, catalogue, modifiers)

    print(f"\nSettings check for {league_key}")
    print(f"  Yahoo stat catalogue : {len(catalogue)} stats")
    print(f"  Scored in this league: {len(modifiers)} stats\n")

    if not differences:
        print("  No drift — Yahoo matches docs/League_Rules_And_Settings.md.\n")
        return 0

    print(f"  {len(differences)} difference(s):\n")
    for difference in differences:
        print(f"    - {difference}")
    print("\n  Resolve these with your commissioner before the draft.\n")
    return 1


def cmd_keepers(season: int) -> int:
    """Print the keeper board without starting the web UI."""
    from ffl.engines import keepers

    config = get_config()
    with db.session() as conn:
        row = conn.execute(
            "SELECT league_key FROM leagues WHERE season = ?", (season,)
        ).fetchone()
        if not row:
            print(f"No {season} league data. Run --sync --season {season} first.")
            return 1

        grouped = keepers.evaluate_by_team(conn, row["league_key"], config.keeper_rules)
        max_keepers = config.keeper_rules.get("max_per_team", 2)

        for team_key, entries in grouped.items():
            team_name = entries[0].team_name or team_key
            eligible = [e for e in entries if e.eligible]
            print(f"\n{team_name}  ({len(eligible)} eligible, keep up to {max_keepers})")
            for entry in entries:
                if entry.eligible:
                    print(
                        f"   ✓ R{entry.draft_round:<2} -> R{entry.kdv_round:<2} "
                        f"{entry.player_name} ({entry.position})"
                    )
                else:
                    print(
                        f"   ✗ R{entry.draft_round:<2}       "
                        f"{entry.player_name} ({entry.position}) — {'; '.join(entry.reasons)}"
                    )
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pull Yahoo and nflverse data into SQLite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--season", type=int, required=True, help="Season year, e.g. 2025")
    parser.add_argument("--discover", action="store_true", help="List your Yahoo leagues")
    parser.add_argument("--sync", action="store_true", help="Sync Yahoo data for the season")
    parser.add_argument("--sync-nflverse", action="store_true", help="Sync nflverse data")
    parser.add_argument("--check-settings", action="store_true", help="Diff Yahoo vs rules doc")
    parser.add_argument("--keepers", action="store_true", help="Print the keeper board")
    parser.add_argument(
        "--save-fixtures",
        action="store_true",
        help="Save real API responses to tests/fixtures/ (use with --sync)",
    )
    args = parser.parse_args()

    actions = [args.discover, args.sync, args.sync_nflverse, args.check_settings, args.keepers]
    if not any(actions):
        parser.error("pick one of --discover, --sync, --sync-nflverse, --check-settings, --keepers")

    try:
        if args.discover:
            return cmd_discover(args.season)
        if args.sync:
            return cmd_sync(args.season, args.save_fixtures)
        if args.sync_nflverse:
            return cmd_sync_nflverse(args.season)
        if args.check_settings:
            return cmd_check_settings(args.season)
        if args.keepers:
            return cmd_keepers(args.season)
    except ConfigError as exc:
        print(f"\nConfiguration problem:\n  {exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n{type(exc).__name__}: {exc}\n", file=sys.stderr)
        print("Run `python scripts/preflight.py` to check credentials and connectivity.\n",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
