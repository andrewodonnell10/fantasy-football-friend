"""nflverse data adapter.

nflverse is the analytics backbone: play-by-play derived weekly stats, snap
counts, injuries, schedules, and the cross-platform player ID crosswalk. No API
key, CC-BY 4.0, distributed as parquet/csv on GitHub releases.

Everything lands in SQLite so the rest of the project reads one store, and so a
sync can be re-run without re-downloading.
"""

from __future__ import annotations

import io
import sqlite3
from typing import Any

import nflreadpy as nfl
import polars as pl
import requests

from ffl import db
from ffl.scoring import get_scorer

SOURCE = "nflverse"

# nflreadpy fetches the DynastyProcess ID map through a github.com/<org>/<repo>/raw/…
# URL. That path is filtered in some sandboxed networks while the canonical
# raw.githubusercontent.com host — which is where GitHub redirects it anyway — is
# reachable. We try the library first and fall back to the canonical host, so this
# works both in a restricted environment and on a normal machine.
PLAYERIDS_RAW_URL = (
    "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"
)


def _rows(frame: pl.DataFrame) -> list[dict[str, Any]]:
    return frame.to_dicts()


def _get(row: dict, *names: str) -> Any:
    """First present, non-null value among `names`.

    nflverse occasionally renames columns between releases (`team` vs
    `recent_team`); accepting either keeps a sync from breaking on a rename.
    """
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


# ---------------------------------------------------------------------------
# Player ID crosswalk — the keystone
# ---------------------------------------------------------------------------


def fetch_player_ids() -> pl.DataFrame:
    """Load the DynastyProcess player ID map, falling back to the canonical host."""
    try:
        return nfl.load_ff_playerids()
    except Exception:
        response = requests.get(PLAYERIDS_RAW_URL, timeout=90)
        response.raise_for_status()
        # infer_schema_length=0 reads everything as strings: these are identifiers,
        # not numbers, and the file uses "NA" for missing values.
        return pl.read_csv(
            io.BytesIO(response.content), infer_schema_length=0, null_values=["NA", ""]
        )


def sync_player_ids(conn: sqlite3.Connection) -> int:
    """Populate `player_ids`, the join between Yahoo and every other source."""
    with db.sync_run(conn, SOURCE, "player_ids") as run:
        frame = fetch_player_ids()
        rows = [
            {
                "gsis_id": str(r["gsis_id"]),
                "name": _get(r, "name"),
                "position": _get(r, "position"),
                "team": _get(r, "team"),
                "yahoo_id": _get(r, "yahoo_id"),
                "sleeper_id": _get(r, "sleeper_id"),
                "espn_id": _get(r, "espn_id"),
                "pfr_id": _get(r, "pfr_id"),
                "fantasypros_id": _get(r, "fantasypros_id"),
            }
            for r in _rows(frame)
            if r.get("gsis_id")
        ]
        run["rows"] = db.upsert(conn, "player_ids", rows, ["gsis_id"])
    return run["rows"]


# ---------------------------------------------------------------------------
# Weekly stats, scored through this league's rules
# ---------------------------------------------------------------------------


def sync_weekly_stats(conn: sqlite3.Connection, season: int) -> int:
    """Load weekly player stats and price each line with the league's scoring."""
    scorer = get_scorer()

    with db.sync_run(conn, SOURCE, "weekly_stats", season) as run:
        frame = nfl.load_player_stats(seasons=[season])
        rows = []
        for r in _rows(frame):
            # A handful of rows carry no player id (aggregate/placeholder rows).
            # They cannot be joined to anything, so they are dropped rather than
            # stored under a synthetic key.
            if not r.get("player_id"):
                continue
            stats = scorer.from_nflverse(r)
            position = _get(r, "position")
            rows.append(
                {
                    "season": r["season"],
                    "week": r["week"],
                    "gsis_id": r["player_id"],
                    "season_type": _get(r, "season_type") or "REG",
                    "player_name": _get(r, "player_display_name", "player_name"),
                    "position": position,
                    "team": _get(r, "team", "recent_team"),
                    "opponent": _get(r, "opponent_team"),
                    "stats_json": stats,
                    "league_points": scorer.score(stats, position),
                }
            )
        run["rows"] = db.upsert(
            conn, "weekly_stats", rows, ["season", "week", "gsis_id", "season_type"]
        )
    return run["rows"]


def sync_snap_counts(conn: sqlite3.Connection, season: int) -> int:
    """Snap share drives the opportunity signal for waivers and start/sit."""
    with db.sync_run(conn, SOURCE, "snap_counts", season) as run:
        frame = nfl.load_snap_counts(seasons=[season])
        rows = [
            {
                "season": r["season"],
                "week": r["week"],
                "pfr_id": r["pfr_player_id"],
                "player_name": _get(r, "player"),
                "position": _get(r, "position"),
                "team": _get(r, "team"),
                "offense_snaps": _get(r, "offense_snaps"),
                "offense_pct": _get(r, "offense_pct"),
            }
            for r in _rows(frame)
            if r.get("pfr_player_id")
        ]
        run["rows"] = db.upsert(conn, "snap_counts", rows, ["season", "week", "pfr_id"])
    return run["rows"]


def sync_injuries(conn: sqlite3.Connection, season: int) -> int:
    with db.sync_run(conn, SOURCE, "injuries", season) as run:
        frame = nfl.load_injuries(seasons=[season])
        rows = [
            {
                "season": r["season"],
                "week": r["week"],
                "gsis_id": r["gsis_id"],
                "player_name": _get(r, "full_name"),
                "team": _get(r, "team"),
                "report_status": _get(r, "report_status"),
                "practice_status": _get(r, "practice_status"),
            }
            for r in _rows(frame)
            if r.get("gsis_id")
        ]
        run["rows"] = db.upsert(conn, "injuries", rows, ["season", "week", "gsis_id"])
    return run["rows"]


def sync_schedules(conn: sqlite3.Connection, season: int) -> int:
    """Schedules carry roof type (for weather) and Vegas lines (for game script)."""
    with db.sync_run(conn, SOURCE, "schedules", season) as run:
        frame = nfl.load_schedules(seasons=[season])
        rows = [
            {
                "game_id": r["game_id"],
                "season": r["season"],
                "week": _get(r, "week"),
                "gameday": str(_get(r, "gameday") or ""),
                "home_team": _get(r, "home_team"),
                "away_team": _get(r, "away_team"),
                "roof": _get(r, "roof"),
                "stadium": _get(r, "stadium"),
                "spread_line": _get(r, "spread_line"),
                "total_line": _get(r, "total_line"),
                "raw_json": None,
            }
            for r in _rows(frame)
            if r.get("game_id")
        ]
        run["rows"] = db.upsert(conn, "schedules", rows, ["game_id"])
    return run["rows"]


def sync_all(conn: sqlite3.Connection, season: int) -> dict[str, int]:
    """Full nflverse pull for one season."""
    return {
        "player_ids": sync_player_ids(conn),
        "weekly_stats": sync_weekly_stats(conn, season),
        "snap_counts": sync_snap_counts(conn, season),
        "injuries": sync_injuries(conn, season),
        "schedules": sync_schedules(conn, season),
    }


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


def link_yahoo_players(conn: sqlite3.Connection) -> dict[str, int]:
    """Report how many Yahoo players resolve to an nflverse identity.

    Coverage is reported rather than assumed: an unmatched player silently
    scoring zero would be worse than knowing the join is incomplete. Team
    defenses are counted separately since they have no nflverse player row and
    are expected not to match.
    """
    total = conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
    defenses = conn.execute(
        "SELECT COUNT(*) AS n FROM players WHERE position IN ('DEF','DST','D/ST')"
    ).fetchone()["n"]
    matched = conn.execute(
        """
        SELECT COUNT(*) AS n FROM players p
        JOIN player_ids i ON i.yahoo_id = p.player_id
        """
    ).fetchone()["n"]

    joinable = total - defenses
    return {
        "yahoo_players": total,
        "team_defenses": defenses,
        "matched": matched,
        "unmatched": max(0, joinable - matched),
        "coverage_pct": round(100 * matched / joinable, 1) if joinable else 0.0,
    }


def unmatched_players(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Yahoo players with no nflverse counterpart, so gaps can be eyeballed."""
    return conn.execute(
        """
        SELECT p.player_key, p.player_id, p.name, p.position, p.nfl_team
        FROM players p
        LEFT JOIN player_ids i ON i.yahoo_id = p.player_id
        WHERE i.gsis_id IS NULL
          AND p.position NOT IN ('DEF','DST','D/ST')
        ORDER BY p.name
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
