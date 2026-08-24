from __future__ import annotations

import sqlite3

import pytest

from jmap_eas.store import db, schema


def test_connect_creates_database_and_is_idempotent(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    conn = db.connect(path)
    try:
        assert path.exists()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()

    # Reopening an already-migrated database must not fail or reapply migrations.
    conn2 = db.connect(path)
    try:
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == len(schema.MIGRATIONS)
    finally:
        conn2.close()


def test_transaction_commits_on_success(tmp_path):
    conn = db.connect(tmp_path / "bridge.sqlite3")
    try:
        conn.execute("CREATE TABLE t (v INTEGER)")
        with db.transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")
        assert conn.execute("SELECT v FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_transaction_rolls_back_on_error(tmp_path):
    conn = db.connect(tmp_path / "bridge.sqlite3")
    try:
        conn.execute("CREATE TABLE t (v INTEGER)")
        with pytest.raises(ValueError), db.transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            raise ValueError("boom")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


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
