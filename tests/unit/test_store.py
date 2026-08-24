from __future__ import annotations

import sqlite3

import pytest

from jmap_eas.store import db, schema


def test_connect_creates_database_and_is_idempotent(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    database = db.connect(path)
    try:
        assert path.exists()
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert database.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        database.close()

    # Reopening an already-migrated database must not fail or reapply migrations.
    database2 = db.connect(path)
    try:
        assert database2.execute("PRAGMA user_version").fetchone()[0] == len(schema.MIGRATIONS)
    finally:
        database2.close()


def test_transaction_commits_on_success(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    try:
        database.execute("CREATE TABLE t (v INTEGER)")
        with database.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
        assert database.execute("SELECT v FROM t").fetchone()[0] == 1
    finally:
        database.close()


def test_transaction_rolls_back_on_error(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    try:
        database.execute("CREATE TABLE t (v INTEGER)")
        with pytest.raises(ValueError), database.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise ValueError("boom")
        assert database.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        database.close()


def test_migrate_applies_pending_migrations_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(
        schema,
        "MIGRATIONS",
        (
            "CREATE TABLE a (id INTEGER PRIMARY KEY);",
            "CREATE TABLE b (id INTEGER PRIMARY KEY);",
        ),
    )
    conn = sqlite3.connect(tmp_path / "migrated.sqlite3")
    try:
        schema.migrate(conn)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"a", "b"} <= tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2

        # Running again must be a no-op, not re-execute CREATE TABLE.
        schema.migrate(conn)
    finally:
        conn.close()
