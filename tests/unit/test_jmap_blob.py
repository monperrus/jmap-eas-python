from __future__ import annotations

import pytest
from pyactivesync.models import BodyType, FetchedItem, ItemBody

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.jmap import blob


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
