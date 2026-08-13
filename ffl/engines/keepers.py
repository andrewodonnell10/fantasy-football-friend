"""Keeper eligibility engine.

A pure function over SQLite — no network, no I/O beyond the database — so the
whole rule set is testable against a synthetic season.

The rules, from docs/League_Rules_And_Settings.md:

    * Up to TWO players may be kept, at a Keeper Draft Value (KDV) of the round
      ahead of where they were drafted (round 7 -> kept as a round 6 pick).
    * Players drafted in the first round are NOT eligible.
    * Dropping a player at any point in the season makes them NOT eligible.
    * Players picked up from waivers in season are NOT eligible.
    * Trading a player makes them NOT eligible for the new manager.
    * At minimum, you must have originally drafted them.

The engine reports *why* a player failed, never just that they did. These rules
are unforgiving and a manager will reasonably ask why a given name is missing;
"drafted round 1" is an answer, an empty row is not.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ffl.config import get_config

# Yahoo `source_type` / `destination_type` values that mean "the open player pool".
POOL_SOURCES = {"waivers", "freeagents"}


@dataclass
class KeeperEvaluation:
    """One drafted player's keeper verdict for the following season."""

    player_key: str
    player_name: str
    position: str | None
    nfl_team: str | None
    team_key: str
    team_name: str | None
    draft_round: int
    draft_pick: int
    eligible: bool
    kdv_round: int | None
    reasons: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "Eligible" if self.eligible else "Not eligible"

    @property
    def costs_first_rounder(self) -> bool:
        """True when keeping this player consumes a first-round pick.

        A round-2 draftee has a KDV of round 1. The rules exclude players
        *drafted* in round 1, not keepers *valued* at round 1, so this is
        allowed on a literal reading — but it is expensive and contentious
        enough to be worth calling out rather than presenting as routine.
        """
        return self.eligible and self.kdv_round == 1

    def as_dict(self) -> dict:
        return {
            "player_key": self.player_key,
            "player_name": self.player_name,
            "position": self.position,
            "nfl_team": self.nfl_team,
            "team_key": self.team_key,
            "team_name": self.team_name,
            "draft_round": self.draft_round,
            "draft_pick": self.draft_pick,
            "eligible": self.eligible,
            "kdv_round": self.kdv_round,
            "reasons": list(self.reasons),
            "cautions": list(self.cautions),
        }


def _fmt_ts(timestamp: int | None) -> str:
    if not timestamp:
        return "unknown date"
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return "unknown date"


def _player_events(conn: sqlite3.Connection, league_key: str) -> dict[str, list[sqlite3.Row]]:
    """Every transaction touching each player in this league, oldest first."""
    rows = conn.execute(
        """
        SELECT tp.player_key, tp.type, tp.source_type, tp.destination_type,
               t.type AS txn_type, t.timestamp
        FROM transaction_players tp
        JOIN transactions t ON t.transaction_key = tp.transaction_key
        WHERE t.league_key = ?
        ORDER BY t.timestamp ASC
        """,
        (league_key,),
    ).fetchall()

    events: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        events.setdefault(row["player_key"], []).append(row)
    return events


def _final_rosters(conn: sqlite3.Connection, league_key: str) -> dict[str, str]:
    """player_key -> team_key on the last week we have roster data for."""
    row = conn.execute(
        "SELECT MAX(week) AS wk FROM rosters WHERE league_key = ?", (league_key,)
    ).fetchone()
    if not row or row["wk"] is None:
        return {}

    return {
        r["player_key"]: r["team_key"]
        for r in conn.execute(
            "SELECT player_key, team_key FROM rosters WHERE league_key = ? AND week = ?",
            (league_key, row["wk"]),
        )
    }


def evaluate_league(
    conn: sqlite3.Connection,
    league_key: str,
    rules: dict | None = None,
) -> list[KeeperEvaluation]:
    """Evaluate every drafted player in `league_key` for next-season keeper status.

    `league_key` is the season the players were DRAFTED in (2025), not the season
    they would be kept for (2026).
    """
    keeper_rules = dict(rules or get_config().keeper_rules)
    excluded_rounds = set(keeper_rules.get("excluded_draft_rounds", [1]))
    kdv_delta = int(keeper_rules.get("kdv_round_delta", -1))
    voiding = keeper_rules.get("voiding_events", {})

    events = _player_events(conn, league_key)
    final_roster = _final_rosters(conn, league_key)
    have_rosters = bool(final_roster)

    picks = conn.execute(
        """
        SELECT d.player_key, d.team_key, d.round, d.pick,
               p.name AS player_name, p.position, p.nfl_team,
               t.name AS team_name
        FROM draft_picks d
        LEFT JOIN players p ON p.player_key = d.player_key
        LEFT JOIN teams   t ON t.team_key   = d.team_key
        WHERE d.league_key = ?
        ORDER BY d.pick ASC
        """,
        (league_key,),
    ).fetchall()

    results: list[KeeperEvaluation] = []

    for pick in picks:
        reasons: list[str] = []
        player_key = pick["player_key"]

        # Rule: players drafted in the first round are never eligible.
        if pick["round"] in excluded_rounds:
            reasons.append(f"Drafted in round {pick['round']} (round 1 picks cannot be kept)")

        for event in events.get(player_key, []):
            when = _fmt_ts(event["timestamp"])

            if voiding.get("dropped", True) and event["type"] == "drop":
                reasons.append(f"Dropped on {when}")

            if (
                voiding.get("waiver_or_fa_add", True)
                and event["type"] == "add"
                and (event["source_type"] or "").lower() in POOL_SOURCES
            ):
                source = (event["source_type"] or "").lower()
                label = "waivers" if source == "waivers" else "free agency"
                reasons.append(f"Acquired from {label} on {when}")

            if voiding.get("traded", True) and (event["txn_type"] or "").lower() == "trade":
                reasons.append(f"Traded on {when}")

        # Consistency check: a player never dropped or traded should still be on
        # the drafting team's roster. If not, something happened that the
        # transaction log did not explain — surface it rather than assume.
        if have_rosters:
            current_team = final_roster.get(player_key)
            if current_team is None:
                reasons.append("Not on any end-of-season roster")
            elif current_team != pick["team_key"]:
                reasons.append("Finished the season on a different team")

        # Deduplicate while preserving order — a player dropped twice reads
        # better as one line than two identical ones.
        seen: set[str] = set()
        reasons = [r for r in reasons if not (r in seen or seen.add(r))]

        eligible = not reasons
        kdv = max(1, pick["round"] + kdv_delta) if eligible else None

        cautions: list[str] = []
        if eligible and kdv == 1:
            cautions.append(
                "Keeping this player costs your first-round pick — worth confirming "
                "with the commissioner, since the rules exclude round 1 draftees but "
                "are silent on round 1 keeper values"
            )

        results.append(
            KeeperEvaluation(
                player_key=player_key,
                player_name=pick["player_name"] or player_key,
                position=pick["position"],
                nfl_team=pick["nfl_team"],
                team_key=pick["team_key"],
                team_name=pick["team_name"],
                draft_round=pick["round"],
                draft_pick=pick["pick"],
                eligible=eligible,
                kdv_round=kdv,
                reasons=reasons,
                cautions=cautions,
            )
        )

    return results


def evaluate_by_team(
    conn: sqlite3.Connection,
    league_key: str,
    rules: dict | None = None,
) -> dict[str, list[KeeperEvaluation]]:
    """Same evaluation, grouped by team, eligible first then by draft round."""
    grouped: dict[str, list[KeeperEvaluation]] = {}
    for evaluation in evaluate_league(conn, league_key, rules):
        grouped.setdefault(evaluation.team_key, []).append(evaluation)

    for entries in grouped.values():
        entries.sort(key=lambda e: (not e.eligible, e.draft_round, e.draft_pick))
    return grouped


def declaration_deadline(
    conn: sqlite3.Connection,
    league_key: str,
    rules: dict | None = None,
) -> datetime | None:
    """When keepers must be declared: draft time minus the rule's lead days.

    Returns None if the target season's draft time is not known yet.
    """
    keeper_rules = dict(rules or get_config().keeper_rules)
    lead_days = int(keeper_rules.get("declaration_deadline_days_before_draft", 7))

    row = conn.execute(
        "SELECT draft_time FROM leagues WHERE league_key = ?", (league_key,)
    ).fetchone()
    if not row or not row["draft_time"]:
        return None

    raw = str(row["draft_time"])
    # Yahoo returns a unix timestamp for draft_time; tolerate ISO too.
    try:
        draft_at = datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        try:
            draft_at = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if draft_at.tzinfo is None:
            draft_at = draft_at.replace(tzinfo=timezone.utc)

    return draft_at - timedelta(days=lead_days)


def save_selections(
    conn: sqlite3.Connection,
    league_key: str,
    season: int,
    team_key: str,
    picks: list[dict],
    rules: dict | None = None,
) -> None:
    """Persist a team's keeper choices, replacing any previous set.

    `picks` items need `player_key` and `kdv_round`; `note` is optional.

    Recording these is not bookkeeping for its own sake. Yahoo never marks who
    was a keeper — a kept player appears as an ordinary pick in their KDV round —
    so this table is the only thing that will make the "kept twice consecutively"
    rule computable next season.
    """
    keeper_rules = dict(rules or get_config().keeper_rules)
    max_keepers = int(keeper_rules.get("max_per_team", 2))

    if len(picks) > max_keepers:
        raise ValueError(
            f"{len(picks)} keepers selected but the league allows at most {max_keepers}."
        )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn.execute(
        "DELETE FROM keeper_selections WHERE league_key=? AND season=? AND team_key=?",
        (league_key, season, team_key),
    )
    conn.executemany(
        """
        INSERT INTO keeper_selections
            (league_key, season, team_key, player_key, kdv_round,
             kdv_source, keep_number, declared_at, note)
        VALUES (?, ?, ?, ?, ?, 'round_minus_one', 1, ?, ?)
        """,
        [
            (
                league_key,
                season,
                team_key,
                pick["player_key"],
                pick.get("kdv_round"),
                now,
                pick.get("note"),
            )
            for pick in picks
        ],
    )
    conn.commit()
