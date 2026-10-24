"""SQLite connection, schema migration and small query helpers. Plain SQL, no ORM."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    sql = resources.files("tracker").joinpath("schema.sql").read_text()
    conn.executescript(sql)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def payload_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- raw_record -------------------------------------------------------------


def store_raw(
    conn: sqlite3.Connection,
    source: str,
    kind: str,
    external_id: str,
    payload: str,
    url: str | None = None,
    fetched_at: str | None = None,
) -> bool:
    """Insert or update a raw record. Returns True if the payload is new or changed."""
    h = payload_hash(payload)
    row = conn.execute(
        "SELECT payload_hash FROM raw_record WHERE source=? AND kind=? AND external_id=?",
        (source, kind, external_id),
    ).fetchone()
    fetched_at = fetched_at or utcnow()
    if row is None:
        conn.execute(
            "INSERT INTO raw_record (source, kind, external_id, url, fetched_at, payload, payload_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, kind, external_id, url, fetched_at, payload, h),
        )
        return True
    if row["payload_hash"] == h:
        conn.execute(
            "UPDATE raw_record SET fetched_at=? WHERE source=? AND kind=? AND external_id=?",
            (fetched_at, source, kind, external_id),
        )
        return False
    conn.execute(
        "UPDATE raw_record SET url=?, fetched_at=?, payload=?, payload_hash=?"
        " WHERE source=? AND kind=? AND external_id=?",
        (url, fetched_at, payload, h, source, kind, external_id),
    )
    return True


def get_raw(conn: sqlite3.Connection, source: str, kind: str, external_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM raw_record WHERE source=? AND kind=? AND external_id=?",
        (source, kind, external_id),
    ).fetchone()


def pending_raw(conn: sqlite3.Connection, source: str, kind: str, all_records: bool = False) -> list[sqlite3.Row]:
    """Raw records that have not been normalized at their current hash (or all of them)."""
    sql = "SELECT * FROM raw_record WHERE source=? AND kind=?"
    if not all_records:
        sql += " AND (normalized_hash IS NULL OR normalized_hash != payload_hash)"
    return conn.execute(sql + " ORDER BY external_id", (source, kind)).fetchall()


def mark_normalized(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    conn.execute(
        "UPDATE raw_record SET normalized_hash=? WHERE source=? AND kind=? AND external_id=?",
        (row["payload_hash"], row["source"], row["kind"], row["external_id"]),
    )


# --- sync_state -------------------------------------------------------------


def get_sync(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sync_state WHERE source=?", (source,)).fetchone()


def set_sync(conn: sqlite3.Connection, source: str, cursor: str | None, last_run: str | None = None) -> None:
    conn.execute(
        "INSERT INTO sync_state (source, cursor, last_run) VALUES (?, ?, ?)"
        " ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor, last_run=excluded.last_run",
        (source, cursor, last_run or utcnow()),
    )


# --- run_log ----------------------------------------------------------------


def start_run(conn: sqlite3.Connection, command: str) -> int:
    cur = conn.execute("INSERT INTO run_log (command, started_at) VALUES (?, ?)", (command, utcnow()))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, stats: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE run_log SET finished_at=?, status=?, stats_json=? WHERE id=?",
        (utcnow(), status, json.dumps(stats, sort_keys=True, default=str), run_id),
    )
    conn.commit()
