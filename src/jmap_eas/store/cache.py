"""The normalized local cache: mailboxes (folders), threads, and emails.

Every write here happens inside the caller's `Database.transaction()` block
so cache updates and their `state.append_change()` change-log entries commit
atomically (plan.md section 4).
"""
from __future__ import annotations

import json
import sqlite3

from ..models import EmailAddress, EmailRecord, MailboxRecord


def _addresses_to_json(addresses: list[EmailAddress]) -> str:
    return json.dumps([a.to_jmap() for a in addresses])


def _addresses_from_json(value: str) -> list[EmailAddress]:
    return [EmailAddress(email=a["email"], name=a["name"]) for a in json.loads(value)]


def _mailbox_from_row(row: sqlite3.Row) -> MailboxRecord:
    return MailboxRecord(
        account_id=row["account_id"],
        mailbox_id=row["mailbox_id"],
        parent_id=row["parent_id"],
        name=row["name"],
        folder_type=row["folder_type"],
        sync_key=row["sync_key"],
    )


def _email_from_row(row: sqlite3.Row) -> EmailRecord:
    return EmailRecord(
        account_id=row["account_id"],
        email_id=row["email_id"],
        mailbox_id=row["mailbox_id"],
        server_id=row["server_id"],
        thread_id=row["thread_id"],
        subject=row["subject"],
        from_addresses=_addresses_from_json(row["from_json"]),
        to_addresses=_addresses_from_json(row["to_json"]),
        cc_addresses=_addresses_from_json(row["cc_json"]),
        reply_to_addresses=_addresses_from_json(row["reply_to_json"]),
        received_at=row["received_at"],
        seen=bool(row["seen"]),
        flagged=bool(row["flagged"]),
    )


# -- Mailboxes ----------------------------------------------------------------


def upsert_mailbox(conn: sqlite3.Connection, mailbox: MailboxRecord) -> None:
    conn.execute(
        """
        INSERT INTO mailboxes (account_id, mailbox_id, parent_id, name, folder_type, sync_key)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (account_id, mailbox_id) DO UPDATE SET
            parent_id = excluded.parent_id,
            name = excluded.name,
            folder_type = excluded.folder_type
        """,
        (mailbox.account_id, mailbox.mailbox_id, mailbox.parent_id, mailbox.name, mailbox.folder_type,
         mailbox.sync_key),
    )


def set_mailbox_sync_key(conn: sqlite3.Connection, account_id: str, mailbox_id: str, sync_key: str) -> None:
    conn.execute(
        "UPDATE mailboxes SET sync_key = ? WHERE account_id = ? AND mailbox_id = ?",
        (sync_key, account_id, mailbox_id),
    )


def delete_mailbox(conn: sqlite3.Connection, account_id: str, mailbox_id: str) -> None:
    conn.execute("DELETE FROM mailboxes WHERE account_id = ? AND mailbox_id = ?", (account_id, mailbox_id))


def get_mailbox(conn: sqlite3.Connection, account_id: str, mailbox_id: str) -> MailboxRecord | None:
    row = conn.execute(
        "SELECT * FROM mailboxes WHERE account_id = ? AND mailbox_id = ?", (account_id, mailbox_id)
    ).fetchone()
    return _mailbox_from_row(row) if row is not None else None


def list_mailboxes(conn: sqlite3.Connection, account_id: str) -> list[MailboxRecord]:
    rows = conn.execute(
        "SELECT * FROM mailboxes WHERE account_id = ? ORDER BY mailbox_id", (account_id,)
    ).fetchall()
    return [_mailbox_from_row(row) for row in rows]


# -- Threads --------------------------------------------------------------------


def get_or_create_thread_id(conn: sqlite3.Connection, account_id: str, thread_key: str, new_id: str) -> str:
    """Look up the thread id for `thread_key`, creating one from `new_id` if none exists yet.

    `new_id` is only used the first time this `thread_key` is seen; every call
    with the same `(account_id, thread_key)` returns the same, previously
    assigned id.
    """
    conn.execute(
        "INSERT INTO threads (account_id, thread_key, thread_id) VALUES (?, ?, ?) "
        "ON CONFLICT (account_id, thread_key) DO NOTHING",
        (account_id, thread_key, new_id),
    )
    row = conn.execute(
        "SELECT thread_id FROM threads WHERE account_id = ? AND thread_key = ?", (account_id, thread_key)
    ).fetchone()
    assert row is not None
    return str(row["thread_id"])


def list_thread_email_ids(conn: sqlite3.Connection, account_id: str, thread_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT email_id FROM emails WHERE account_id = ? AND thread_id = ? ORDER BY received_at, email_id",
        (account_id, thread_id),
    ).fetchall()
    return [str(row["email_id"]) for row in rows]


# -- Emails -----------------------------------------------------------------------


def upsert_email(conn: sqlite3.Connection, email: EmailRecord) -> None:
    conn.execute(
        """
        INSERT INTO emails (
            account_id, email_id, mailbox_id, server_id, thread_id, subject,
            from_json, to_json, cc_json, reply_to_json, received_at, seen, flagged
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (account_id, email_id) DO UPDATE SET
            mailbox_id = excluded.mailbox_id,
            server_id = excluded.server_id,
            thread_id = excluded.thread_id,
            subject = excluded.subject,
            from_json = excluded.from_json,
            to_json = excluded.to_json,
            cc_json = excluded.cc_json,
            reply_to_json = excluded.reply_to_json,
            received_at = excluded.received_at,
            seen = excluded.seen,
            flagged = excluded.flagged
        """,
        (
            email.account_id, email.email_id, email.mailbox_id, email.server_id, email.thread_id, email.subject,
            _addresses_to_json(email.from_addresses), _addresses_to_json(email.to_addresses),
            _addresses_to_json(email.cc_addresses), _addresses_to_json(email.reply_to_addresses),
            email.received_at, int(email.seen), int(email.flagged),
        ),
    )


def delete_email(conn: sqlite3.Connection, account_id: str, email_id: str) -> None:
    conn.execute("DELETE FROM emails WHERE account_id = ? AND email_id = ?", (account_id, email_id))


def get_email(conn: sqlite3.Connection, account_id: str, email_id: str) -> EmailRecord | None:
    row = conn.execute(
        "SELECT * FROM emails WHERE account_id = ? AND email_id = ?", (account_id, email_id)
    ).fetchone()
    return _email_from_row(row) if row is not None else None


def find_email_id_by_server_id(
    conn: sqlite3.Connection, account_id: str, mailbox_id: str, server_id: str
) -> str | None:
    row = conn.execute(
        "SELECT email_id FROM emails WHERE account_id = ? AND mailbox_id = ? AND server_id = ?",
        (account_id, mailbox_id, server_id),
    ).fetchone()
    return str(row["email_id"]) if row is not None else None


def list_emails_in_mailbox(conn: sqlite3.Connection, account_id: str, mailbox_id: str) -> list[EmailRecord]:
    rows = conn.execute(
        "SELECT * FROM emails WHERE account_id = ? AND mailbox_id = ? ORDER BY received_at, email_id",
        (account_id, mailbox_id),
    ).fetchall()
    return [_email_from_row(row) for row in rows]


def list_emails_for_account(conn: sqlite3.Connection, account_id: str) -> list[EmailRecord]:
    """All cached emails for `account_id`, across every mailbox. `Email/query` filters and sorts these itself."""
    rows = conn.execute("SELECT * FROM emails WHERE account_id = ?", (account_id,)).fetchall()
    return [_email_from_row(row) for row in rows]
