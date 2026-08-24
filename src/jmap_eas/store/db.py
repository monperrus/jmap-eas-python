"""SQLite connection setup and transaction helper."""
from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from . import schema


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the bridge's database and apply pending migrations.

    EAS calls run in worker threads (plan.md section 1), so the returned
    connection may be used from a thread other than the one that opened it;
    callers are responsible for serializing concurrent access.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    schema.migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """Run a block atomically: commits on success, rolls back on exception."""
    with conn:
        yield conn
