from __future__ import annotations

import pytest
from pyactivesync.exceptions import StatusError
from pyactivesync.models import BodyType, FetchedItem, Folder, FolderType, ItemBody, SyncItem, SyncResult

import jmap_eas.backend.eas as eas_module
from jmap_eas.backend.eas import EasAdapter
from jmap_eas.config import AccountConfig
from jmap_eas.errors import BackendError


class FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False
        self.provisioned = False

    def provision(self) -> str:
        if self.fail:
            raise StatusError("Provision", "165", "DeviceInformationRequired")
        self.provisioned = True
        return "policy-key"

    def list_folders(self) -> list[Folder]:
        if self.fail:
            raise StatusError("FolderSync", "12")
        return [Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")]

    def sync_folder(self, folder_id, sync_key="0", *, window_size=100, filter_type=None):
        if self.fail:
            raise StatusError("Sync", "12")
        return SyncResult(
            sync_key="1",
            added=[SyncItem(server_id="9:1", fields={"Email.Subject": "Hello"})],
            changed=[],
            deleted=[],
            more_available=False,
        )

    def fetch_item(self, folder_id, item_id, *, body_type=BodyType.HTML):
        if self.fail:
            raise StatusError("ItemOperations", "12")
        return FetchedItem(
            fields={"Email.Subject": "Hello"},
            bodies=[ItemBody(type=body_type, data="<p>hi</p>")],
            attachments=[],
        )

    def fetch_attachment(self, file_reference):
        if self.fail:
            raise StatusError("ItemOperations", "12")
        return b"attachment-bytes"

    def close(self) -> None:
        self.closed = True


def test_provision_returns_policy_key():
    adapter = EasAdapter(FakeClient())
    assert adapter.provision() == "policy-key"


def test_list_folders_returns_folders():
    adapter = EasAdapter(FakeClient())
    folders = adapter.list_folders()
    assert [f.name for f in folders] == ["Inbox"]


def test_non_eas_exception_is_also_mapped_to_backend_error():
    """A library bug (e.g. an unhandled edge case in WBXML parsing) must be redacted
    exactly like a normal EAS failure, not crash the request with a raw traceback."""

    class BuggyClient(FakeClient):
        def list_folders(self):
            raise IndexError("index out of range")

    adapter = EasAdapter(BuggyClient())
    with pytest.raises(BackendError) as exc_info:
        adapter.list_folders()
    assert isinstance(exc_info.value.cause, IndexError)
    assert "index out of range" not in str(exc_info.value)


def test_provision_maps_eas_exception_to_backend_error():
    adapter = EasAdapter(FakeClient(fail=True))
    with pytest.raises(BackendError) as exc_info:
        adapter.provision()
    assert "Provision" not in str(exc_info.value)
    assert isinstance(exc_info.value.cause, StatusError)


def test_list_folders_maps_eas_exception_to_backend_error():
    adapter = EasAdapter(FakeClient(fail=True))
    with pytest.raises(BackendError) as exc_info:
        adapter.list_folders()
    # The client-visible message must not leak the raw EAS exception text.
    assert "FolderSync" not in str(exc_info.value)
    assert isinstance(exc_info.value.cause, StatusError)


def test_sync_folder_returns_result():
    adapter = EasAdapter(FakeClient())
    result = adapter.sync_folder("1", "0")
    assert result.sync_key == "1"
    assert result.added[0].server_id == "9:1"


def test_sync_folder_maps_eas_exception_to_backend_error():
    adapter = EasAdapter(FakeClient(fail=True))
    with pytest.raises(BackendError) as exc_info:
        adapter.sync_folder("1", "0")
    assert "Sync" not in str(exc_info.value)


def test_fetch_item_returns_fetched_item():
    adapter = EasAdapter(FakeClient())
    item = adapter.fetch_item("1", "9:1")
    assert item.fields["Email.Subject"] == "Hello"
    assert item.bodies[0].data == "<p>hi</p>"


def test_fetch_item_maps_eas_exception_to_backend_error():
    adapter = EasAdapter(FakeClient(fail=True))
    with pytest.raises(BackendError):
        adapter.fetch_item("1", "9:1")


def test_fetch_attachment_returns_bytes():
    adapter = EasAdapter(FakeClient())
    assert adapter.fetch_attachment("ref") == b"attachment-bytes"


def test_fetch_attachment_maps_eas_exception_to_backend_error():
    adapter = EasAdapter(FakeClient(fail=True))
    with pytest.raises(BackendError):
        adapter.fetch_attachment("ref")


def test_close_closes_underlying_client():
    client = FakeClient()
    adapter = EasAdapter(client)
    adapter.close()
    assert client.closed is True


def test_connect_builds_client_from_account_config(monkeypatch):
    captured: dict[str, object] = {}

    class RecordingClient(FakeClient):
        def __init__(self, server, username, password, **kwargs):
            captured["args"] = (server, username, password)
            captured["kwargs"] = kwargs
            super().__init__()

    monkeypatch.setattr(eas_module, "Client", RecordingClient)
    config = AccountConfig(
        eas_server="https://mail.example.com/Microsoft-Server-ActiveSync",
        username="alice@example.com",
        device_id="dev1",
        password="secret",
        api_token="token",
    )
    adapter = EasAdapter.connect(config)
    assert captured["args"] == ("https://mail.example.com/Microsoft-Server-ActiveSync", "alice@example.com", "secret")
    assert captured["kwargs"]["device_id"] == "dev1"
    assert adapter.provision() == "policy-key"
