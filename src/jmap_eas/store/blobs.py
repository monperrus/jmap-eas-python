"""Uploaded blob storage (RFC 8620 section 6.1's `POST /upload/{accountId}`).

Unlike a download blob id (`jmap/blob.py`, which always re-fetches from EAS
live), an uploaded blob has no EAS locator until it's used inside a draft --
its bytes are stored here until then.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class UploadedBlob:
    account_id: str
    blob_id: str
    content_type: str
    data: bytes
    size: int


def insert_blob(conn: sqlite3.Connection, account_id: str, blob_id: str, content_type: str, data: bytes) -> None:
    conn.execute(
        "INSERT INTO uploaded_blobs (account_id, blob_id, content_type, data, size, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, blob_id, content_type, data, len(data), datetime.now(UTC).isoformat()),
    )


def get_blob(conn: sqlite3.Connection, account_id: str, blob_id: str) -> UploadedBlob | None:
    row = conn.execute(
        "SELECT * FROM uploaded_blobs WHERE account_id = ? AND blob_id = ?", (account_id, blob_id)
    ).fetchone()
    if row is None:
        return None
    return UploadedBlob(
        account_id=row["account_id"], blob_id=row["blob_id"], content_type=row["content_type"],
        data=row["data"], size=row["size"],
    )
