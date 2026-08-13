"""Yahoo Fantasy Sports adapter.

Read-only. Yahoo's standard developer access does not permit roster moves,
waiver claims, or trades, and this project deliberately does not attempt them —
it recommends, and the manager acts in Yahoo.

Authentication is handled by the `yahoofantasy` package: `yahoofantasy login`
runs a browser OAuth flow once and persists a refresh token. That flow needs a
browser and a reachable localhost, so it must be run on a real machine.

Note on keys: `nfl.l.123456` uses a lowercase L as the separator, not a digit 1.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from yahoofantasy import Context

from ffl import db

SOURCE = "yahoo"

# Yahoo publishes no documented rate limit, so requests are spaced rather than
# fired in a loop. Syncs are infrequent; politeness costs nothing here.
REQUEST_DELAY_SECONDS = 0.4


class YahooAuthError(RuntimeError):
    """Raised when no usable Yahoo token is available."""


def get_context() -> Context:
    """Build a yahoofantasy Context, with an actionable error when unauthenticated."""
    try:
        return Context()
    except Exception as exc:
        raise YahooAuthError(
            f"Could not initialise the Yahoo API context ({exc}).\n"
            "Run `yahoofantasy login` first — see SETUP_YAHOO_APP.md. "
            "That flow needs a browser, so it must be run on your own machine."
        ) from exc


def _value(obj: Any, *path: str, default: Any = None) -> Any:
    """Walk an attribute path, returning `default` if any hop is missing.

    yahoofantasy builds objects dynamically from XML, so which attributes exist
    varies by league configuration. Reaching through this keeps a sync from
    dying on an optional field.
    """
    current = obj
    for name in path:
        if current is None:
            return default
        current = getattr(current, name, None)
    return default if current is None else current


def discover_leagues(season: int, game: str = "nfl") -> list[dict]:
    """List the authenticated user's leagues for a season.

    This is how the correct league_id gets identified for `.env`.
    """
    ctx = get_context()
    return [
        {
            "league_key": league.id,
            "league_id": _value(league, "league_id"),
            "name": _value(league, "name"),
            "season": _value(league, "season"),
            "num_teams": _value(league, "num_teams"),
            "scoring_type": _value(league, "scoring_type"),
        }
        for league in ctx.get_leagues(game, season)
    ]


def _find_league(ctx: Context, season: int, league_id: str, game: str = "nfl"):
    """Locate a League object by its numeric id within a season."""
    for league in ctx.get_leagues(game, season):
        if str(_value(league, "league_id")) == str(league_id) or league.id.endswith(
            f".l.{league_id}"
        ):
            return league
    raise ValueError(
        f"League id {league_id} not found for the {season} season. "
        f"Run `python scripts/fetch_league.py --discover --season {season}` to list yours."
    )


# ---------------------------------------------------------------------------
# Individual resource syncs
# ---------------------------------------------------------------------------


def sync_league(conn: sqlite3.Connection, league, season: int) -> str:
    """Persist league metadata. Returns the league key."""
    with db.sync_run(conn, SOURCE, "league", season) as run:
        league_key = league.id
        row = {
            "league_key": league_key,
            "league_id": str(_value(league, "league_id", default="")),
            "game_key": league_key.split(".")[0],
            "season": int(_value(league, "season", default=season)),
            "name": _value(league, "name"),
            "num_teams": _value(league, "num_teams"),
            "scoring_type": _value(league, "scoring_type"),
            "draft_status": _value(league, "draft_status"),
            "draft_time": str(_value(league, "draft_time", default="") or ""),
            "is_keeper": None,
            "current_week": _value(league, "current_week"),
            "raw_json": None,
        }
        run["rows"] = db.upsert(conn, "leagues", [row], ["league_key"])
    return league.id


def sync_teams(conn: sqlite3.Connection, league, season: int) -> int:
    with db.sync_run(conn, SOURCE, "teams", season) as run:
        rows = []
        for team in league.teams():
            rows.append(
                {
                    "team_key": team.id,
                    "league_key": league.id,
                    "team_id": int(_value(team, "team_id", default=0) or 0),
                    "name": _value(team, "name"),
                    "manager_name": _value(team, "manager", "nickname"),
                    "logo_url": _value(team, "team_logos", "team_logo", "url"),
                    "raw_json": None,
                }
            )
        run["rows"] = db.upsert(conn, "teams", rows, ["team_key"])
    return run["rows"]


def sync_standings(conn: sqlite3.Connection, league, season: int) -> int:
    with db.sync_run(conn, SOURCE, "standings", season) as run:
        captured_at = db.utcnow()
        rows = []
        for team in league.standings():
            standings = _value(team, "team_standings")
            outcome = _value(standings, "outcome_totals")
            rows.append(
                {
                    "league_key": league.id,
                    "team_key": team.id,
                    "captured_at": captured_at,
                    "rank": _value(standings, "rank"),
                    "wins": _value(outcome, "wins"),
                    "losses": _value(outcome, "losses"),
                    "ties": _value(outcome, "ties"),
                    "points_for": _value(standings, "points_for"),
                    "points_against": _value(standings, "points_against"),
                    "raw_json": None,
                }
            )
        run["rows"] = db.upsert(
            conn, "standings", rows, ["league_key", "team_key", "captured_at"]
        )
    return run["rows"]


def _player_row(player) -> dict:
    return {
        "player_key": _value(player, "player_key"),
        "player_id": str(_value(player, "player_id", default="")),
        "name": _value(player, "name", "full"),
        "position": _value(player, "display_position"),
        "nfl_team": _value(player, "editorial_team_abbr"),
        "bye_week": _value(player, "bye_weeks", "week"),
        "raw_json": None,
    }


def sync_draft(conn: sqlite3.Connection, league, season: int) -> int:
    """Draft results. `round` is what determines Keeper Draft Value."""
    with db.sync_run(conn, SOURCE, "draft_picks", season) as run:
        picks, players = [], {}
        for result in league.draft_results():
            player = result.player
            player_key = _value(player, "player_key")
            if not player_key:
                continue
            players[player_key] = _player_row(player)
            picks.append(
                {
                    "league_key": league.id,
                    "pick": int(result.pick),
                    "round": int(result.round),
                    "team_key": _value(result, "team_key")
                    or _value(result, "team", "id"),
                    "player_key": player_key,
                    "cost": _value(result, "cost"),
                    "raw_json": None,
                }
            )

        db.upsert(conn, "players", list(players.values()), ["player_key"])
        run["rows"] = db.upsert(conn, "draft_picks", picks, ["league_key", "pick"])
    return run["rows"]


def sync_transactions(conn: sqlite3.Connection, league, season: int) -> int:
    """Every add, drop, and trade — the source of all keeper-voiding events."""
    with db.sync_run(conn, SOURCE, "transactions", season) as run:
        transactions, movements, players = [], [], {}

        for txn in league.transactions():
            txn_key = _value(txn, "transaction_key")
            if not txn_key:
                continue

            transactions.append(
                {
                    "transaction_key": txn_key,
                    "league_key": league.id,
                    "type": _value(txn, "type"),
                    "status": _value(txn, "status"),
                    "timestamp": int(_value(txn, "timestamp", default=0) or 0),
                    "raw_json": None,
                }
            )

            for involved in txn.involved_players:
                player_key = _value(involved, "player_key")
                data = _value(involved, "transaction_data")
                if not player_key or data is None:
                    continue

                players[player_key] = _player_row(involved)
                movements.append(
                    {
                        "transaction_key": txn_key,
                        "player_key": player_key,
                        "type": _value(data, "type", default="unknown"),
                        "source_type": _value(data, "source_type"),
                        "source_team_key": _value(data, "source_team_key"),
                        "destination_type": _value(data, "destination_type"),
                        "destination_team_key": _value(data, "destination_team_key"),
                    }
                )

        db.upsert(conn, "transactions", transactions, ["transaction_key"])
        # Only fill in players we don't already have richer draft data for.
        db.upsert(conn, "players", list(players.values()), ["player_key"])
        run["rows"] = db.upsert(
            conn, "transaction_players", movements,
            ["transaction_key", "player_key", "type"],
        )
    return run["rows"]


def sync_rosters(conn: sqlite3.Connection, league, season: int, week: int | None = None) -> int:
    """Roster for each team. Defaults to the live roster when `week` is None."""
    with db.sync_run(conn, SOURCE, "rosters", season) as run:
        rows, players = [], {}

        for team in league.teams():
            roster = team.roster(week)
            week_num = int(week or _value(league, "current_week", default=0) or 0)

            for player in roster.players:
                player_key = _value(player, "player_key")
                if not player_key:
                    continue
                players[player_key] = _player_row(player)
                position = _value(player, "selected_position", "position")
                rows.append(
                    {
                        "league_key": league.id,
                        "team_key": team.id,
                        "week": week_num,
                        "player_key": player_key,
                        "selected_position": position,
                        "is_starting": 0 if position in ("BN", "IR") else 1,
                        "raw_json": None,
                    }
                )
            time.sleep(REQUEST_DELAY_SECONDS)

        db.upsert(conn, "players", list(players.values()), ["player_key"])
        run["rows"] = db.upsert(
            conn, "rosters", rows, ["league_key", "team_key", "week", "player_key"]
        )
    return run["rows"]


# ---------------------------------------------------------------------------
# Raw endpoints not wrapped by yahoofantasy
# ---------------------------------------------------------------------------


def fetch_settings(ctx: Context, league_key: str) -> dict:
    """League settings, including the full stat_modifiers scoring table."""
    return ctx._load_or_fetch(f"settings.{league_key}", "settings", league=league_key)


def fetch_draft_analysis(ctx: Context, player_key: str) -> dict:
    """A player's ADP for the season embedded in `player_key`.

    Not needed for 2026 — this league's first keeper year means nobody can be on
    a second consecutive keep, which is the only rule that uses ADP. Wired up now
    because `yahoofantasy` does not expose it and next season will need it.
    """
    return ctx._load_or_fetch(
        f"draft_analysis.{player_key}", f"player/{player_key}/draft_analysis"
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def sync_season(
    conn: sqlite3.Connection,
    season: int,
    league_id: str,
    game: str = "nfl",
    include_rosters: bool = True,
) -> dict[str, Any]:
    """Full Yahoo pull for one season.

    For 2025 this supplies the keeper inputs (draft + transactions + final
    rosters). For 2026 the league row alone carries the draft date that drives
    the declaration deadline.
    """
    ctx = get_context()
    league = _find_league(ctx, season, league_id, game)

    results: dict[str, Any] = {"league_key": sync_league(conn, league, season)}
    results["teams"] = sync_teams(conn, league, season)

    draft_status = str(_value(league, "draft_status", default="")).lower()
    if draft_status == "postdraft":
        results["draft_picks"] = sync_draft(conn, league, season)
        results["transactions"] = sync_transactions(conn, league, season)
        results["standings"] = sync_standings(conn, league, season)
        if include_rosters:
            results["rosters"] = sync_rosters(conn, league, season)
    else:
        # Pre-draft: there is nothing to pull but the schedule and settings.
        results["note"] = (
            f"{season} draft has not happened yet (status: {draft_status or 'unknown'}); "
            "skipped draft, transactions, standings and rosters."
        )

    return results
