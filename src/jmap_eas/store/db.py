"""SQLite connection setup, serialized under one lock per database.

EAS calls run in worker threads (plan.md section 1), so several threads can
reach the same `Database` concurrently. One `sqlite3.Connection` is not safe
for interleaved multi-statement transactions across threads even with
`check_same_thread=False`, so every read and write here takes `_lock`.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from . import schema


class Database:
    """A SQLite connection plus the lock that serializes all access to it."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._lock = threading.RLock()

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """Run a block atomically under the lock: commits on success, rolls back on exception."""
        with self._lock, self.conn:
            yield self.conn

    def close(self) -> None:
        with self._lock:
            self.conn.close()


def connect(path: str | Path) -> Database:
    """Open (creating if needed) the bridge's database and apply pending migrations."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    schema.migrate(conn)
    return Database(conn)
