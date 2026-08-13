"""Database layer: upsert idempotency and sync auditing.

Idempotency is the property that makes syncing safe to re-run, which matters
because a partial sync (rate limit, dropped connection) is recovered by simply
running it again.
"""

from __future__ import annotations

import pytest

from ffl import db
from tests.conftest import seed_season


def test_upsert_is_idempotent(conn):
    row = {
        "league_key": "nfl.l.1",
        "league_id": "1",
        "game_key": "nfl",
        "season": 2025,
        "name": "Test",
    }
    for _ in range(3):
        db.upsert(conn, "leagues", [row], ["league_key"])

    assert conn.execute("SELECT COUNT(*) FROM leagues").fetchone()[0] == 1


def test_upsert_updates_changed_columns(conn):
    base = {
        "league_key": "nfl.l.1",
        "league_id": "1",
        "game_key": "nfl",
        "season": 2025,
        "name": "Old Name",
    }
    db.upsert(conn, "leagues", [base], ["league_key"])
    db.upsert(conn, "leagues", [{**base, "name": "New Name"}], ["league_key"])

    assert conn.execute("SELECT name FROM leagues").fetchone()["name"] == "New Name"


def test_upsert_serialises_dicts_to_json(conn):
    """`raw_json` columns get populated without every caller remembering to encode."""
    db.upsert(
        conn,
        "leagues",
        [{
            "league_key": "nfl.l.1", "league_id": "1", "game_key": "nfl",
            "season": 2025, "raw_json": {"a": 1},
        }],
        ["league_key"],
    )
    assert conn.execute("SELECT raw_json FROM leagues").fetchone()["raw_json"] == '{"a": 1}'


def test_upsert_of_empty_list_is_a_noop(conn):
    assert db.upsert(conn, "leagues", [], ["league_key"]) == 0


def test_upsert_rejects_missing_key_column(conn):
    with pytest.raises(ValueError, match="key column"):
        db.upsert(conn, "leagues", [{"name": "no key"}], ["league_key"])


def test_full_reseed_does_not_duplicate(conn):
    """Re-running a whole sync must leave row counts unchanged."""
    seed_season(conn)
    before = db.table_counts(conn)

    for table, keys in [
        ("draft_picks", ["league_key", "pick"]),
        ("transaction_players", ["transaction_key", "player_key", "type"]),
        ("rosters", ["league_key", "team_key", "week", "player_key"]),
    ]:
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
        db.upsert(conn, table, rows, keys)

    assert db.table_counts(conn) == before


def test_sync_run_records_success(conn):
    with db.sync_run(conn, "yahoo", "draft_picks", 2025) as run:
        run["rows"] = 120

    row = conn.execute("SELECT * FROM sync_runs").fetchone()
    assert row["status"] == "ok"
    assert row["rows_written"] == 120
    assert row["finished_at"] is not None


def test_sync_run_records_failure_and_reraises(conn):
    with pytest.raises(RuntimeError, match="boom"):
        with db.sync_run(conn, "yahoo", "transactions", 2025):
            raise RuntimeError("boom")

    row = conn.execute("SELECT * FROM sync_runs").fetchone()
    assert row["status"] == "error"
    assert "boom" in row["error"]


def test_last_sync_returns_most_recent_per_resource(conn):
    for rows in (1, 2, 3):
        with db.sync_run(conn, "yahoo", "teams", 2025) as run:
            run["rows"] = rows

    latest = db.last_sync(conn)
    assert len(latest) == 1
    assert latest[0]["rows_written"] == 3
