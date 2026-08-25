"""Local `EmailSubmission` bookkeeping (plan.md section 5's M3 note).

`client_id` is persisted *before* `Client.send_mail()` is called, so a
submission's outcome is recorded even if the process crashes or the HTTP
response never reaches the caller -- sending is not automatically safe to
retry, and this record is the correlation a caller (or an operator) needs to
find out what happened without guessing.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

STATUS_PENDING = "pending"
STATUS_FINAL = "final"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class SubmissionRecord:
    account_id: str
    submission_id: str
    email_id: str
    identity_id: str
    client_id: str
    status: str
    created_at: str


def _from_row(row: sqlite3.Row) -> SubmissionRecord:
    return SubmissionRecord(
        account_id=row["account_id"], submission_id=row["submission_id"], email_id=row["email_id"],
        identity_id=row["identity_id"], client_id=row["client_id"], status=row["status"],
        created_at=row["created_at"],
    )


def insert_submission(
    conn: sqlite3.Connection, account_id: str, submission_id: str, email_id: str, identity_id: str,
    client_id: str, status: str = STATUS_PENDING,
) -> None:
    conn.execute(
        "INSERT INTO submissions (account_id, submission_id, email_id, identity_id, client_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, email_id, identity_id, client_id, status, datetime.now(UTC).isoformat()),
    )


def set_status(conn: sqlite3.Connection, account_id: str, submission_id: str, status: str) -> None:
    conn.execute(
        "UPDATE submissions SET status = ? WHERE account_id = ? AND submission_id = ?",
        (status, account_id, submission_id),
    )


def get_submission(conn: sqlite3.Connection, account_id: str, submission_id: str) -> SubmissionRecord | None:
    row = conn.execute(
        "SELECT * FROM submissions WHERE account_id = ? AND submission_id = ?", (account_id, submission_id)
    ).fetchone()
    return _from_row(row) if row is not None else None


def list_submissions(conn: sqlite3.Connection, account_id: str) -> list[SubmissionRecord]:
    rows = conn.execute(
        "SELECT * FROM submissions WHERE account_id = ? ORDER BY created_at, submission_id", (account_id,)
    ).fetchall()
    return [_from_row(row) for row in rows]


def delete_submission(conn: sqlite3.Connection, account_id: str, submission_id: str) -> None:
    conn.execute(
        "DELETE FROM submissions WHERE account_id = ? AND submission_id = ?", (account_id, submission_id)
    )
