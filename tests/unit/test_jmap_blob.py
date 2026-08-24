from __future__ import annotations

import pytest
from pyactivesync.models import BodyType, FetchedItem, ItemBody

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.jmap import blob
from jmap_eas.store import blobs as store_blobs
from jmap_eas.store import db


class FakeClient:
    def provision(self):
        return "policy-key"

    def list_folders(self):
        return []

    def sync_folder(self, *a, **k):
        raise NotImplementedError

    def fetch_item(self, folder_id, item_id, *, body_type=BodyType.HTML):
        return FetchedItem(fields={}, bodies=[ItemBody(type=body_type, data="raw mime bytes")], attachments=[])

    def fetch_attachment(self, file_reference):
        return b"attachment-bytes:" + file_reference.encode()

    def close(self):
        pass


def test_message_blob_id_round_trips():
    blob_id = blob.encode_message_blob_id("1", "9:1")
    locator = blob.decode_blob_id(blob_id)
    assert locator == blob.BlobLocator(kind="msg", mailbox_id="1", server_id="9:1")


def test_attachment_blob_id_round_trips():
    blob_id = blob.encode_attachment_blob_id("attach-ref-123")
    locator = blob.decode_blob_id(blob_id)
    assert locator == blob.BlobLocator(kind="att", file_reference="attach-ref-123")


def test_decode_blob_id_rejects_garbage():
    with pytest.raises(ValueError):
        blob.decode_blob_id("not-valid-base64!!!")


def test_decode_blob_id_rejects_unknown_kind():
    bad = blob._encode({"k": "other"})
    with pytest.raises(ValueError):
        blob.decode_blob_id(bad)


def test_fetch_blob_message_uses_mime_body_type():
    adapter = EasAdapter(FakeClient())
    data, content_type = blob.fetch_blob(blob.BlobLocator(kind="msg", mailbox_id="1", server_id="9:1"), adapter)
    assert data == b"raw mime bytes"
    assert content_type == "message/rfc822"


def test_fetch_blob_attachment():
    adapter = EasAdapter(FakeClient())
    data, content_type = blob.fetch_blob(blob.BlobLocator(kind="att", file_reference="ref1"), adapter)
    assert data == b"attachment-bytes:ref1"
    assert content_type == "application/octet-stream"


def test_upload_blob_id_round_trips():
    blob_id = blob.encode_upload_blob_id("u1")
    assert blob.decode_blob_id(blob_id) == blob.BlobLocator(kind="up", upload_id="u1")


def test_resolve_blob_msg_uses_eas_adapter():
    adapter = EasAdapter(FakeClient())
    result = blob.resolve_blob(
        blob.BlobLocator(kind="msg", mailbox_id="1", server_id="9:1"), account_id="alice", adapter=adapter,
        database=None,
    )
    assert result == (b"raw mime bytes", "message/rfc822")


def test_resolve_blob_up_reads_from_store(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    with database.transaction() as conn:
        store_blobs.insert_blob(conn, "alice", "u1", "text/plain", b"hello")
    result = blob.resolve_blob(
        blob.BlobLocator(kind="up", upload_id="u1"), account_id="alice", adapter=None, database=database
    )
    assert result == (b"hello", "text/plain")


def test_resolve_blob_up_missing_returns_none(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    result = blob.resolve_blob(
        blob.BlobLocator(kind="up", upload_id="missing"), account_id="alice", adapter=None, database=database
    )
    assert result is None
