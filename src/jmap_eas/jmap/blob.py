"""Blob ids and blob fetching (RFC 8620 section 6).

A blob id is locally assigned but, for a `"msg"`/`"att"` blob, carries its EAS
locator directly (plan.md section 3): a whole message or one attachment,
re-fetched live from EAS on every access -- nothing is cached. An uploaded
blob (`"up"`, M2's `POST /upload`) is different: it has no EAS locator until
used inside a draft, so its bytes are stored in `store/blobs.py` instead. The
HTTP routes live in `app.py`, which owns request auth and thread-pool
dispatch.
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from pyactivesync.models import BodyType

from ..backend.eas import EasAdapter
from ..store import blobs
from ..store.db import Database


@dataclass(frozen=True)
class BlobLocator:
    kind: str  # "msg" | "att" | "up"
    mailbox_id: str | None = None
    server_id: str | None = None
    file_reference: str | None = None
    upload_id: str | None = None


def encode_message_blob_id(mailbox_id: str, server_id: str) -> str:
    return _encode({"k": "msg", "m": mailbox_id, "s": server_id})


def encode_attachment_blob_id(file_reference: str) -> str:
    return _encode({"k": "att", "f": file_reference})


def encode_upload_blob_id(upload_id: str) -> str:
    return _encode({"k": "up", "u": upload_id})


def _encode(payload: dict[str, str]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_blob_id(blob_id: str) -> BlobLocator:
    padded = blob_id + "=" * (-len(blob_id) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid blobId: {blob_id!r}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid blobId: {blob_id!r}")
    kind = data.get("k")
    if kind == "msg" and isinstance(data.get("m"), str) and isinstance(data.get("s"), str):
        return BlobLocator(kind="msg", mailbox_id=data["m"], server_id=data["s"])
    if kind == "att" and isinstance(data.get("f"), str):
        return BlobLocator(kind="att", file_reference=data["f"])
    if kind == "up" and isinstance(data.get("u"), str):
        return BlobLocator(kind="up", upload_id=data["u"])
    raise ValueError(f"invalid blobId: {blob_id!r}")


def fetch_blob(locator: BlobLocator, adapter: EasAdapter) -> tuple[bytes, str]:
    """Fetch a `"msg"`/`"att"` blob's bytes and content type live from EAS."""
    if locator.kind == "msg":
        assert locator.mailbox_id is not None and locator.server_id is not None
        item = adapter.fetch_item(locator.mailbox_id, locator.server_id, body_type=BodyType.MIME)
        data = item.bodies[0].data if item.bodies else b""
        if isinstance(data, str):
            data = data.encode("utf-8")
        return data, "message/rfc822"
    assert locator.file_reference is not None
    return adapter.fetch_attachment(locator.file_reference), "application/octet-stream"


def resolve_blob(
    locator: BlobLocator, *, account_id: str, adapter: EasAdapter | None, database: Database | None
) -> tuple[bytes, str] | None:
    """Fetch any blob's bytes and content type: `"up"` from local storage, `"msg"`/`"att"` live from EAS."""
    if locator.kind == "up":
        assert database is not None
        assert locator.upload_id is not None
        with database.transaction() as conn:
            stored = blobs.get_blob(conn, account_id, locator.upload_id)
        return (stored.data, stored.content_type) if stored is not None else None
    assert adapter is not None
    return fetch_blob(locator, adapter)
