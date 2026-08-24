"""Blob ids and blob fetching (plan.md section 5's M1 read surface).

A blob id is locally assigned but carries its EAS locator directly (plan.md
section 3): a whole message (`kind="msg"`) or one attachment (`kind="att"`).
Nothing is cached -- a download re-fetches from EAS through the account's
adapter, so there is no blob storage table to keep in sync in M1. The HTTP
route lives in `app.py`, which owns request auth and thread-pool dispatch.
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from pyactivesync.models import BodyType

from ..backend.eas import EasAdapter


@dataclass(frozen=True)
class BlobLocator:
    kind: str  # "msg" | "att"
    mailbox_id: str | None = None
    server_id: str | None = None
    file_reference: str | None = None


def encode_message_blob_id(mailbox_id: str, server_id: str) -> str:
    return _encode({"k": "msg", "m": mailbox_id, "s": server_id})


def encode_attachment_blob_id(file_reference: str) -> str:
    return _encode({"k": "att", "f": file_reference})


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
    raise ValueError(f"invalid blobId: {blob_id!r}")


def fetch_blob(locator: BlobLocator, adapter: EasAdapter) -> tuple[bytes, str]:
    """Fetch a blob's bytes and content type live from EAS. Never cached (plan.md section 3)."""
    if locator.kind == "msg":
        assert locator.mailbox_id is not None and locator.server_id is not None
        item = adapter.fetch_item(locator.mailbox_id, locator.server_id, body_type=BodyType.MIME)
        data = item.bodies[0].data if item.bodies else b""
        if isinstance(data, str):
            data = data.encode("utf-8")
        return data, "message/rfc822"
    assert locator.file_reference is not None
    return adapter.fetch_attachment(locator.file_reference), "application/octet-stream"
