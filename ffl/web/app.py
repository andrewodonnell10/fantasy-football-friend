"""Local web UI.

Bound to 127.0.0.1 only. This holds a private league's data and there is no
authentication in front of it — it is a personal tool, not a service.

    python -m ffl.web.app

Then open http://127.0.0.1:5001
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from ffl import db
from ffl.config import get_config
from ffl.engines import keepers

# Port 5001 rather than 5000: macOS binds 5000 to the AirPlay receiver, which
# produces a confusing "connection refused" for anyone on a Mac.
DEFAULT_PORT = 5001

# The season keepers are being chosen FOR. Eligibility is computed from the
# season before it, since that is where the draft and transactions live.
TARGET_SEASON = 2026
SOURCE_SEASON = 2025


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    # Only used to flash messages between redirects on a localhost-only app.
    app.secret_key = "fantasy-football-friend-local"

    def conn() -> sqlite3.Connection:
        connection = db.connect(app.config["DB_PATH"])
        db.init_db(connection)
        return connection

    def league_key(connection: sqlite3.Connection, season: int) -> str | None:
        row = connection.execute(
            "SELECT league_key FROM leagues WHERE season = ?", (season,)
        ).fetchone()
        return row["league_key"] if row else None

    # -- dashboard ----------------------------------------------------------

    @app.route("/")
    def index():
        config = get_config()
        with conn() as connection:
            source_key = league_key(connection, SOURCE_SEASON)
            target_key = league_key(connection, TARGET_SEASON)

            leagues = connection.execute(
                "SELECT * FROM leagues ORDER BY season DESC"
            ).fetchall()

            deadline = days_left = None
            if target_key:
                deadline = keepers.declaration_deadline(
                    connection, target_key, config.keeper_rules
                )
                if deadline:
                    days_left = (deadline - datetime.now(timezone.utc)).days

            eligible_count = 0
            if source_key:
                evaluations = keepers.evaluate_league(
                    connection, source_key, config.keeper_rules
                )
                eligible_count = sum(1 for e in evaluations if e.eligible)

            return render_template(
                "index.html",
                leagues=leagues,
                counts=db.table_counts(connection),
                syncs=db.last_sync(connection),
                deadline=deadline,
                days_left=days_left,
                eligible_count=eligible_count,
                has_data=bool(source_key),
                source_season=SOURCE_SEASON,
                target_season=TARGET_SEASON,
            )

    # -- keeper board -------------------------------------------------------

    @app.route("/keepers")
    def keeper_board():
        config = get_config()
        with conn() as connection:
            source_key = league_key(connection, SOURCE_SEASON)
            if not source_key:
                return render_template(
                    "empty.html",
                    what=f"{SOURCE_SEASON} league data",
                    how=f"python scripts/fetch_league.py --sync --season {SOURCE_SEASON}",
                )

            grouped = keepers.evaluate_by_team(connection, source_key, config.keeper_rules)
            selections = _selections_by_team(connection)

            teams = []
            for team_key, entries in grouped.items():
                teams.append(
                    {
                        "team_key": team_key,
                        "team_name": entries[0].team_name or team_key,
                        "eligible": [e for e in entries if e.eligible],
                        "ineligible": [e for e in entries if not e.eligible],
                        "selected": selections.get(team_key, []),
                    }
                )
            teams.sort(key=lambda t: t["team_name"])

            return render_template(
                "keepers.html",
                teams=teams,
                max_keepers=config.keeper_rules.get("max_per_team", 2),
                target_season=TARGET_SEASON,
                source_season=SOURCE_SEASON,
            )

    @app.route("/keepers/<path:team_key>", methods=["GET", "POST"])
    def team_keepers(team_key: str):
        config = get_config()
        max_keepers = int(config.keeper_rules.get("max_per_team", 2))

        with conn() as connection:
            source_key = league_key(connection, SOURCE_SEASON)
            if not source_key:
                abort(404)

            grouped = keepers.evaluate_by_team(connection, source_key, config.keeper_rules)
            entries = grouped.get(team_key)
            if not entries:
                abort(404)

            if request.method == "POST":
                chosen = request.form.getlist("player_key")
                if len(chosen) > max_keepers:
                    flash(
                        f"You selected {len(chosen)} players; the league allows "
                        f"at most {max_keepers}.",
                        "error",
                    )
                else:
                    by_key = {e.player_key: e for e in entries}
                    invalid = [k for k in chosen if not by_key.get(k) or not by_key[k].eligible]
                    if invalid:
                        flash("One or more selections are not keeper-eligible.", "error")
                    else:
                        keepers.save_selections(
                            connection,
                            source_key,
                            TARGET_SEASON,
                            team_key,
                            [
                                {
                                    "player_key": key,
                                    "kdv_round": by_key[key].kdv_round,
                                    "note": request.form.get("note") or None,
                                }
                                for key in chosen
                            ],
                            config.keeper_rules,
                        )
                        flash(f"Saved {len(chosen)} keeper(s).", "success")
                        return redirect(url_for("team_keepers", team_key=team_key))

            selected = {
                row["player_key"]
                for row in connection.execute(
                    "SELECT player_key FROM keeper_selections "
                    "WHERE league_key=? AND season=? AND team_key=?",
                    (source_key, TARGET_SEASON, team_key),
                )
            }
            note_row = connection.execute(
                "SELECT note FROM keeper_selections "
                "WHERE league_key=? AND season=? AND team_key=? AND note IS NOT NULL LIMIT 1",
                (source_key, TARGET_SEASON, team_key),
            ).fetchone()

            return render_template(
                "team_keepers.html",
                team_key=team_key,
                team_name=entries[0].team_name or team_key,
                eligible=[e for e in entries if e.eligible],
                ineligible=[e for e in entries if not e.eligible],
                selected=selected,
                note=note_row["note"] if note_row else "",
                max_keepers=max_keepers,
                target_season=TARGET_SEASON,
            )

    # -- supporting views ---------------------------------------------------

    @app.route("/draft")
    def draft():
        with conn() as connection:
            source_key = league_key(connection, SOURCE_SEASON)
            if not source_key:
                return render_template(
                    "empty.html",
                    what=f"{SOURCE_SEASON} draft results",
                    how=f"python scripts/fetch_league.py --sync --season {SOURCE_SEASON}",
                )
            picks = connection.execute(
                """
                SELECT d.pick, d.round, d.player_key, p.name AS player_name,
                       p.position, p.nfl_team, t.name AS team_name
                FROM draft_picks d
                LEFT JOIN players p ON p.player_key = d.player_key
                LEFT JOIN teams   t ON t.team_key   = d.team_key
                WHERE d.league_key = ?
                ORDER BY d.pick
                """,
                (source_key,),
            ).fetchall()
            return render_template("draft.html", picks=picks, season=SOURCE_SEASON)

    @app.route("/transactions")
    def transactions():
        kind = request.args.get("type", "")
        with conn() as connection:
            source_key = league_key(connection, SOURCE_SEASON)
            if not source_key:
                return render_template(
                    "empty.html",
                    what=f"{SOURCE_SEASON} transactions",
                    how=f"python scripts/fetch_league.py --sync --season {SOURCE_SEASON}",
                )

            sql = """
                SELECT t.transaction_key, t.type, t.status, t.timestamp,
                       tp.player_key, tp.type AS move, tp.source_type,
                       tp.destination_type, p.name AS player_name, p.position
                FROM transactions t
                JOIN transaction_players tp ON tp.transaction_key = t.transaction_key
                LEFT JOIN players p ON p.player_key = tp.player_key
                WHERE t.league_key = ?
            """
            params: list = [source_key]
            if kind:
                sql += " AND t.type = ?"
                params.append(kind)
            sql += " ORDER BY t.timestamp DESC"

            return render_template(
                "transactions.html",
                rows=connection.execute(sql, params).fetchall(),
                kind=kind,
                season=SOURCE_SEASON,
            )

    @app.route("/players/<path:player_key>")
    def player_detail(player_key: str):
        config = get_config()
        with conn() as connection:
            source_key = league_key(connection, SOURCE_SEASON)
            player = connection.execute(
                "SELECT * FROM players WHERE player_key = ?", (player_key,)
            ).fetchone()
            if not player:
                abort(404)

            pick = connection.execute(
                """
                SELECT d.pick, d.round, t.name AS team_name
                FROM draft_picks d LEFT JOIN teams t ON t.team_key = d.team_key
                WHERE d.league_key = ? AND d.player_key = ?
                """,
                (source_key, player_key),
            ).fetchone()

            timeline = connection.execute(
                """
                SELECT t.type AS txn_type, t.timestamp, tp.type AS move,
                       tp.source_type, tp.destination_type
                FROM transaction_players tp
                JOIN transactions t ON t.transaction_key = tp.transaction_key
                WHERE t.league_key = ? AND tp.player_key = ?
                ORDER BY t.timestamp
                """,
                (source_key, player_key),
            ).fetchall()

            verdict = next(
                (
                    e
                    for e in keepers.evaluate_league(
                        connection, source_key, config.keeper_rules
                    )
                    if e.player_key == player_key
                ),
                None,
            )

            return render_template(
                "player.html",
                player=player,
                pick=pick,
                timeline=timeline,
                verdict=verdict,
            )

    @app.route("/standings")
    def standings():
        with conn() as connection:
            source_key = league_key(connection, SOURCE_SEASON)
            rows = connection.execute(
                """
                SELECT s.*, t.name AS team_name, t.manager_name
                FROM standings s LEFT JOIN teams t ON t.team_key = s.team_key
                WHERE s.league_key = ?
                  AND s.captured_at = (
                      SELECT MAX(captured_at) FROM standings WHERE league_key = ?
                  )
                ORDER BY s.rank
                """,
                (source_key, source_key),
            ).fetchall() if source_key else []
            return render_template("standings.html", rows=rows, season=SOURCE_SEASON)

    @app.template_filter("ts")
    def format_timestamp(value) -> str:
        if not value:
            return "—"
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%b %d, %Y")
        except (ValueError, OSError, TypeError):
            return str(value)

    return app


def _selections_by_team(connection: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in connection.execute(
        """
        SELECT k.team_key, k.player_key, k.kdv_round, p.name AS player_name, p.position
        FROM keeper_selections k
        LEFT JOIN players p ON p.player_key = k.player_key
        WHERE k.season = ?
        """,
        (TARGET_SEASON,),
    ):
        grouped.setdefault(row["team_key"], []).append(row)
    return grouped


def main() -> None:
    app = create_app()
    print(f"\n  Fantasy Football Friend — http://127.0.0.1:{DEFAULT_PORT}\n")
    app.run(host="127.0.0.1", port=DEFAULT_PORT, debug=False)


if __name__ == "__main__":
    main()
