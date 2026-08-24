"""SQLite schema migrations, tracked via `PRAGMA user_version`.

`MIGRATIONS[i]` is the SQL script that advances the database from schema
version `i` to `i + 1`. `migrate()` applies every pending script in order,
inside one transaction. Empty for now: the normalized cache, state, blob, and
submission tables are added by the milestones that need them (plan.md
section 2), not before.
"""
from __future__ import annotations

import sqlite3

MIGRATIONS: tuple[str, ...] = ()


def migrate(conn: sqlite3.Connection) -> None:
    """Apply every migration in `MIGRATIONS` newer than the database's current version."""
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in range(current_version, len(MIGRATIONS)):
        with conn:
            conn.executescript(MIGRATIONS[version])
            conn.execute(f"PRAGMA user_version = {version + 1}")
