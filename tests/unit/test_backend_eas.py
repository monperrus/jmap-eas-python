from __future__ import annotations

import pytest
from pyactivesync.exceptions import StatusError
from pyactivesync.models import (
    BodyType,
    EmailAddResult,
    EmailChangesResult,
    FetchedItem,
    Folder,
    FolderType,
    ItemBody,
    SyncItem,
    SyncResult,
)

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

    def create_folder(self, display_name, parent_id="0", type=FolderType.USER_MAIL):
        if self.fail:
            raise StatusError("FolderCreate", "2")
        return Folder(id="new-folder", parent_id=parent_id, type=type, name=display_name)

    def update_folder(self, folder_id, display_name, parent_id="0"):
        if self.fail:
            raise StatusError("FolderUpdate", "2")

    def delete_folder(self, folder_id):
        if self.fail:
            raise StatusError("FolderDelete", "2")

    def apply_email_changes(self, folder_id, sync_key, changes, *, deletes_as_moves=True):
        if self.fail:
            raise StatusError("Sync", "12")
        return EmailChangesResult(sync_key="2", statuses={c.server_id: "1" for c in changes})

    def create_email_draft(self, folder_id, sync_key, message, *, read=False, flagged=False, client_id=None):
        if self.fail:
            raise StatusError("Sync", "12")
        return EmailAddResult(sync_key="2", client_id=client_id or "cid", status="1", server_id="9:new")

    def move_item(self, item_id, src_folder_id, dst_folder_id):
        if self.fail:
            raise StatusError("MoveItems", "1")
        return "10:new"

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


def test_create_folder_returns_folder():
    adapter = EasAdapter(FakeClient())
    folder = adapter.create_folder("New Folder", "0")
    assert folder.id == "new-folder"
    assert folder.name == "New Folder"


def test_create_folder_maps_eas_exception():
    adapter = EasAdapter(FakeClient(fail=True))
    with pytest.raises(BackendError):
        adapter.create_folder("New Folder")


def test_update_folder_maps_eas_exception():
    adapter = EasAdapter(FakeClient(fail=True))
    with pytest.raises(BackendError):
        adapter.update_folder("1", "Renamed")


def test_delete_folder_maps_eas_exception():
    adapter = EasAdapter(FakeClient(fail=True))
    with pytest.raises(BackendError):
        adapter.delete_folder("1")


def test_apply_email_changes_returns_statuses():
    from pyactivesync.models import EmailChange

    adapter = EasAdapter(FakeClient())
    result = adapter.apply_email_changes("1", "5", [EmailChange("9:1", read=True)])
    assert result.sync_key == "2"
    assert result.statuses == {"9:1": "1"}


def test_apply_email_changes_maps_eas_exception():
    from pyactivesync.models import EmailChange

    adapter = EasAdapter(FakeClient(fail=True))
    with pytest.raises(BackendError):
        adapter.apply_email_changes("1", "5", [EmailChange("9:1", read=True)])


def test_create_email_draft_returns_result():
    from email.message import EmailMessage

    adapter = EasAdapter(FakeClient())
    result = adapter.create_email_draft("1", "5", EmailMessage(), client_id="cid1")
    assert result.server_id == "9:new"
    assert result.status == "1"
    assert result.client_id == "cid1"


def test_create_email_draft_maps_eas_exception():
    from email.message import EmailMessage

    adapter = EasAdapter(FakeClient(fail=True))
    with pytest.raises(BackendError):
        adapter.create_email_draft("1", "5", EmailMessage())


def test_move_item_returns_new_server_id():
    adapter = EasAdapter(FakeClient())
    assert adapter.move_item("9:1", "1", "2") == "10:new"


def test_move_item_maps_eas_exception():
    adapter = EasAdapter(FakeClient(fail=True))
    with pytest.raises(BackendError):
        adapter.move_item("9:1", "1", "2")


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
