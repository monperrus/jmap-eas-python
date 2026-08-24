"""SQLite schema migrations, tracked via `PRAGMA user_version`.

`MIGRATIONS[i]` is the SQL script that advances the database from schema
version `i` to `i + 1`. `migrate()` applies every pending script in order,
inside one transaction. The blob and submission tables are added by the
milestones that need them (plan.md section 2), not before.
"""
from __future__ import annotations

import sqlite3

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE mailboxes (
        account_id TEXT NOT NULL,
        mailbox_id TEXT NOT NULL,
        parent_id TEXT NOT NULL,
        name TEXT NOT NULL,
        folder_type INTEGER NOT NULL,
        sync_key TEXT NOT NULL DEFAULT '0',
        PRIMARY KEY (account_id, mailbox_id)
    );

    CREATE TABLE threads (
        account_id TEXT NOT NULL,
        thread_key TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        PRIMARY KEY (account_id, thread_key)
    );
    CREATE UNIQUE INDEX threads_by_id ON threads (account_id, thread_id);

    CREATE TABLE emails (
        account_id TEXT NOT NULL,
        email_id TEXT NOT NULL,
        mailbox_id TEXT NOT NULL,
        server_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        subject TEXT,
        from_json TEXT NOT NULL DEFAULT '[]',
        to_json TEXT NOT NULL DEFAULT '[]',
        cc_json TEXT NOT NULL DEFAULT '[]',
        reply_to_json TEXT NOT NULL DEFAULT '[]',
        received_at TEXT,
        seen INTEGER NOT NULL DEFAULT 0,
        flagged INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (account_id, email_id)
    );
    CREATE UNIQUE INDEX emails_by_server_id ON emails (account_id, mailbox_id, server_id);
    CREATE INDEX emails_by_thread ON emails (account_id, thread_id);
    CREATE INDEX emails_by_received_at ON emails (account_id, received_at);

    CREATE TABLE change_log (
        account_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        type TEXT NOT NULL,
        object_id TEXT NOT NULL,
        change TEXT NOT NULL,
        PRIMARY KEY (account_id, seq)
    );
    CREATE INDEX change_log_by_type ON change_log (account_id, type, seq);
    """,
    """
    CREATE TABLE uploaded_blobs (
        account_id TEXT NOT NULL,
        blob_id TEXT NOT NULL,
        content_type TEXT NOT NULL,
        data BLOB NOT NULL,
        size INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (account_id, blob_id)
    );
    """,
)


def migrate(conn: sqlite3.Connection) -> None:
    """Apply every migration in `MIGRATIONS` newer than the database's current version."""
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in range(current_version, len(MIGRATIONS)):
        with conn:
            conn.executescript(MIGRATIONS[version])
            conn.execute(f"PRAGMA user_version = {version + 1}")
