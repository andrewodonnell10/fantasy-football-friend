"""Shared fixtures: an in-memory database and a synthetic 2025 season.

The synthetic season is built to exercise every branch of the keeper rules, so
the eligibility engine can be verified without touching Yahoo.
"""

from __future__ import annotations

import sqlite3

import pytest

from ffl import db
from ffl.config import load_rules

LEAGUE_KEY = "461.l.999999"

# Each tuple: (pick, round, team, player_key, name, position)
DRAFT = [
    (1, 1, "t.1", "p.jefferson", "Justin Jefferson", "WR"),
    (2, 1, "t.2", "p.mccaffrey", "Christian McCaffrey", "RB"),
    (13, 2, "t.1", "p.hill", "Tyreek Hill", "WR"),
    (25, 3, "t.2", "p.kelce", "Travis Kelce", "TE"),
    (37, 4, "t.1", "p.dropped", "Dropped Guy", "RB"),
    (49, 5, "t.2", "p.traded", "Traded Guy", "WR"),
    (61, 6, "t.1", "p.rejoined", "Drop Then Readd", "TE"),
    (73, 7, "t.2", "p.clean", "Clean Keeper", "RB"),
    (85, 8, "t.1", "p.vanished", "Vanished Guy", "WR"),
]


@pytest.fixture
def rules() -> dict:
    return load_rules()


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def season(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A 2025 season with one player per keeper-rule branch."""
    return seed_season(conn)


@pytest.fixture
def seeded_db_path(tmp_path):
    """The same synthetic season, on disk, for tests that reopen the database."""
    path = tmp_path / "ffl_test.db"
    connection = db.connect(path)
    db.init_db(connection)
    seed_season(connection)
    connection.close()
    return str(path)


def seed_season(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Populate a connection with the synthetic 2025 season."""
    conn.execute(
        "INSERT INTO leagues (league_key, league_id, game_key, season, name, num_teams, draft_time) "
        "VALUES (?, '999999', '461', 2025, 'Test League', 2, '1756400000')",
        (LEAGUE_KEY,),
    )
    # The 2026 league carries the draft date that drives the declaration deadline.
    conn.execute(
        "INSERT INTO leagues (league_key, league_id, game_key, season, name, num_teams, draft_time) "
        "VALUES ('999.l.999999', '999999', '999', 2026, 'Test League', 2, '1756400000')"
    )
    conn.executemany(
        "INSERT INTO teams (team_key, league_key, team_id, name) VALUES (?, ?, ?, ?)",
        [("t.1", LEAGUE_KEY, 1, "Team One"), ("t.2", LEAGUE_KEY, 2, "Team Two")],
    )
    conn.executemany(
        "INSERT INTO players (player_key, player_id, name, position, nfl_team) "
        "VALUES (?, ?, ?, ?, 'NFL')",
        [(pk, pk.split(".")[-1], name, pos) for _, _, _, pk, name, pos in DRAFT],
    )
    # An undrafted player, to prove the engine never offers them as a keeper.
    conn.execute(
        "INSERT INTO players (player_key, player_id, name, position, nfl_team) "
        "VALUES ('p.undrafted', 'undrafted', 'Waiver Pickup', 'WR', 'NFL')"
    )
    conn.executemany(
        "INSERT INTO draft_picks (league_key, pick, round, team_key, player_key) "
        "VALUES (?, ?, ?, ?, ?)",
        [(LEAGUE_KEY, pick, rnd, team, pk) for pick, rnd, team, pk, _, _ in DRAFT],
    )

    # Timestamps sit inside the 2025 regular season (Sept-Nov 2025).
    transactions = [
        # (txn_key, type, ts, player_key, tp_type, source, dest)
        ("x.1", "drop", 1_757_600_000, "p.dropped", "drop", "team", "waivers"),   # 2025-09-11
        ("x.2", "trade", 1_759_000_000, "p.traded", "add", "team", "team"),       # 2025-09-27
        ("x.3", "drop", 1_760_400_000, "p.rejoined", "drop", "team", "waivers"),  # 2025-10-14
        ("x.4", "add", 1_760_900_000, "p.rejoined", "add", "waivers", "team"),    # 2025-10-19
        ("x.5", "add", 1_762_000_000, "p.undrafted", "add", "freeagents", "team"),# 2025-11-01
    ]
    for key, txn_type, ts, player, tp_type, src, dest in transactions:
        conn.execute(
            "INSERT INTO transactions (transaction_key, league_key, type, status, timestamp) "
            "VALUES (?, ?, ?, 'successful', ?)",
            (key, LEAGUE_KEY, txn_type, ts),
        )
        conn.execute(
            "INSERT INTO transaction_players "
            "(transaction_key, player_key, type, source_type, destination_type) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, player, tp_type, src, dest),
        )

    # Final-week rosters. `p.vanished` is deliberately absent with no transaction
    # explaining it, and `p.traded` sits on the team that acquired them.
    final = [
        ("t.1", "p.jefferson"),
        ("t.1", "p.hill"),
        ("t.1", "p.rejoined"),
        ("t.2", "p.mccaffrey"),
        ("t.2", "p.kelce"),
        ("t.2", "p.clean"),
        ("t.1", "p.traded"),
        ("t.2", "p.undrafted"),
    ]
    conn.executemany(
        "INSERT INTO rosters (league_key, team_key, week, player_key, selected_position) "
        "VALUES (?, ?, 17, ?, 'BN')",
        [(LEAGUE_KEY, team, pk) for team, pk in final],
    )
    conn.commit()
    return conn


@pytest.fixture
def app(seeded_db_path):
    """Flask test client backed by the synthetic season."""
    from ffl.web.app import create_app

    application = create_app(seeded_db_path)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()
