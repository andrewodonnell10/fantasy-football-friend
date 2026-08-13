"""SQLite access layer.

Everything is upsert-based on Yahoo's natural keys, so syncing twice is a no-op
rather than a duplication. `sync_run` records what happened for the dashboard.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ffl.config import PACKAGE_ROOT, get_config

SCHEMA_PATH = PACKAGE_ROOT / "schema.sql"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with sane defaults and the schema applied."""
    path = Path(db_path) if db_path else get_config().db_path
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Apply schema.sql. Idempotent — every statement is CREATE ... IF NOT EXISTS."""
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


@contextmanager
def session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Connection context manager that initialises schema and commits on success."""
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Upserts
# ---------------------------------------------------------------------------


def upsert(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[Mapping[str, Any]],
    key_columns: Sequence[str],
) -> int:
    """Insert rows, updating the non-key columns when the key already exists.

    Returns the number of rows submitted. Any value that is a dict or list is
    JSON-encoded, which is how `raw_json` columns get populated without every
    caller remembering to serialise.
    """
    rows = [dict(r) for r in rows]
    if not rows:
        return 0

    columns = list(rows[0])
    missing = [k for k in key_columns if k not in columns]
    if missing:
        raise ValueError(f"upsert into {table}: key column(s) {missing} not in row")

    placeholders = ", ".join("?" for _ in columns)
    updatable = [c for c in columns if c not in key_columns]
    conflict = (
        f"UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in updatable)}"
        if updatable
        else "NOTHING"
    )

    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({', '.join(key_columns)}) DO {conflict}"
    )

    payload = [
        tuple(
            json.dumps(row.get(c), default=str)
            if isinstance(row.get(c), (dict, list))
            else row.get(c)
            for c in columns
        )
        for row in rows
    ]

    conn.executemany(sql, payload)
    return len(payload)


# ---------------------------------------------------------------------------
# Sync audit
# ---------------------------------------------------------------------------


@contextmanager
def sync_run(
    conn: sqlite3.Connection,
    source: str,
    resource: str,
    season: int | None = None,
) -> Iterator[dict]:
    """Record a sync attempt, capturing failures rather than swallowing them.

    Yield a dict; set `result["rows"]` inside the block to log a row count.
    """
    cur = conn.execute(
        "INSERT INTO sync_runs (source, resource, season, started_at, status) "
        "VALUES (?, ?, ?, ?, 'running')",
        (source, resource, season, utcnow()),
    )
    run_id = cur.lastrowid
    conn.commit()

    result: dict[str, Any] = {"rows": 0}
    try:
        yield result
    except Exception as exc:
        conn.execute(
            "UPDATE sync_runs SET finished_at=?, status='error', error=? WHERE id=?",
            (utcnow(), f"{type(exc).__name__}: {exc}", run_id),
        )
        conn.commit()
        raise
    else:
        conn.execute(
            "UPDATE sync_runs SET finished_at=?, status='ok', rows_written=? WHERE id=?",
            (utcnow(), result.get("rows", 0), run_id),
        )
        conn.commit()


def last_sync(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Most recent run per (source, resource), for the dashboard."""
    return conn.execute(
        """
        SELECT source, resource, season, started_at, finished_at,
               status, rows_written, error
        FROM sync_runs
        WHERE id IN (
            SELECT MAX(id) FROM sync_runs GROUP BY source, resource
        )
        ORDER BY source, resource
        """
    ).fetchall()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row count per table, for the dashboard's at-a-glance view."""
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables
    }
