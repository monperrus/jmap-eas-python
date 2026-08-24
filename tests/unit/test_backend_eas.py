from __future__ import annotations

import pytest
from pyactivesync.exceptions import StatusError
from pyactivesync.models import Folder, FolderType

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

    def close(self) -> None:
        self.closed = True


def test_provision_returns_policy_key():
    adapter = EasAdapter(FakeClient())
    assert adapter.provision() == "policy-key"


def test_list_folders_returns_folders():
    adapter = EasAdapter(FakeClient())
    folders = adapter.list_folders()
    assert [f.name for f in folders] == ["Inbox"]


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
    )
    adapter = EasAdapter.connect(config)
    assert captured["args"] == ("https://mail.example.com/Microsoft-Server-ActiveSync", "alice@example.com", "secret")
    assert captured["kwargs"]["device_id"] == "dev1"
    assert adapter.provision() == "policy-key"
