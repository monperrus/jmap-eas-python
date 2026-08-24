from __future__ import annotations

import base64

from pyactivesync.models import BodyType, FetchedItem, Folder, FolderType, ItemBody, SyncResult
from starlette.testclient import TestClient

from jmap_eas.app import create_app
from jmap_eas.backend.eas import EasAdapter
from jmap_eas.config import AccountConfig, AppConfig, ServerConfig
from jmap_eas.registry import AccountRegistry


class FakeClient:
    def __init__(self, *, folders=None, sync_results=None, fetched_item=None):
        self._folders = folders or []
        self._sync_results = {k: list(v) for k, v in (sync_results or {}).items()}
        self._fetched_item = fetched_item

    def provision(self):
        return "policy-key"

    def list_folders(self):
        return self._folders

    def sync_folder(self, folder_id, sync_key="0", *, window_size=100, filter_type=None):
        return self._sync_results[folder_id].pop(0)

    def fetch_item(self, folder_id, item_id, *, body_type=BodyType.HTML):
        return self._fetched_item

    def fetch_attachment(self, file_reference):
        return b"attachment-bytes"

    def close(self):
        pass


def _account(**overrides) -> AccountConfig:
    defaults = dict(
        eas_server="https://mail.example.com/Microsoft-Server-ActiveSync",
        username="alice@example.com",
        device_id="dev1",
        password="eas-secret",
        api_token="bridge-token",
    )
    defaults.update(overrides)
    return AccountConfig(**defaults)


def _basic(username: str, password: str) -> dict[str, str]:
    raw = f"{username}:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _app_with_fake_client(tmp_path, client: FakeClient):
    config = AppConfig(server=ServerConfig(db_path=str(tmp_path / "bridge.sqlite3")), accounts={"alice": _account()})
    app = create_app(config)
    app.state.jmap_eas.registry = AccountRegistry(
        config.accounts, adapter_factory=lambda cfg: EasAdapter(client)
    )
    return app


# -- session ------------------------------------------------------------------------


def test_well_known_jmap_requires_auth(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.get("/.well-known/jmap")
        assert response.status_code == 401


def test_well_known_jmap_returns_session_for_authenticated_account(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.get("/.well-known/jmap", headers=_basic("alice", "bridge-token"))
        assert response.status_code == 200
        body = response.json()
        assert "alice" in body["accounts"]
        assert body["apiUrl"].endswith("/api")


# -- api ------------------------------------------------------------------------------


def test_api_requires_auth(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post("/api", json={"methodCalls": []})
        assert response.status_code == 401


def test_api_core_echo(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post(
            "/api",
            headers=_basic("alice", "bridge-token"),
            json={"methodCalls": [["Core/echo", {"hello": "world"}, "c0"]]},
        )
        assert response.status_code == 200
        assert response.json()["methodResponses"] == [["Core/echo", {"hello": "world"}, "c0"]]


def test_api_malformed_request_returns_400(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post("/api", headers=_basic("alice", "bridge-token"), json={"notMethodCalls": []})
        assert response.status_code == 400


def test_api_syncs_then_serves_mailbox_get(tmp_path):
    client_fake = FakeClient(
        folders=[Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")],
        sync_results={
            "1": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(sync_key="2", added=[], changed=[], deleted=[], more_available=False),
            ]
        },
    )
    app = _app_with_fake_client(tmp_path, client_fake)
    with TestClient(app) as client:
        response = client.post(
            "/api",
            headers=_basic("alice", "bridge-token"),
            json={"methodCalls": [["Mailbox/get", {"accountId": "alice"}, "c0"]]},
        )
        assert response.status_code == 200
        [[name, result, call_id]] = response.json()["methodResponses"]
        assert name == "Mailbox/get"
        assert call_id == "c0"
        assert [m["id"] for m in result["list"]] == ["1"]


def test_api_result_reference_across_calls(tmp_path):
    client_fake = FakeClient(
        folders=[Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")],
        sync_results={
            "1": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(sync_key="2", added=[], changed=[], deleted=[], more_available=False),
            ]
        },
    )
    app = _app_with_fake_client(tmp_path, client_fake)
    with TestClient(app) as client:
        response = client.post(
            "/api",
            headers=_basic("alice", "bridge-token"),
            json={
                "methodCalls": [
                    ["Mailbox/get", {"accountId": "alice", "properties": ["id"]}, "c0"],
                    [
                        "Mailbox/get",
                        {"accountId": "alice", "ids#": {"resultOf": "c0", "name": "Mailbox/get", "path": "/list/*/id"}},
                        "c1",
                    ],
                ]
            },
        )
        responses = response.json()["methodResponses"]
        assert responses[1][1]["list"][0]["id"] == "1"


# -- download -----------------------------------------------------------------------


def test_download_message_blob(tmp_path):
    item = FetchedItem(fields={}, bodies=[ItemBody(type=BodyType.MIME, data=b"raw mime")], attachments=[])
    app = _app_with_fake_client(tmp_path, FakeClient(fetched_item=item))
    with TestClient(app) as client:
        from jmap_eas.jmap.blob import encode_message_blob_id

        blob_id = encode_message_blob_id("1", "9:1")
        response = client.get(f"/download/alice/{blob_id}/message.eml", headers=_basic("alice", "bridge-token"))
        assert response.status_code == 200
        assert response.content == b"raw mime"
        assert response.headers["content-type"] == "message/rfc822"


def test_download_requires_matching_account(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        from jmap_eas.jmap.blob import encode_message_blob_id

        blob_id = encode_message_blob_id("1", "9:1")
        response = client.get(f"/download/bob/{blob_id}/x", headers=_basic("alice", "bridge-token"))
        assert response.status_code == 404


def test_download_invalid_blob_id(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.get("/download/alice/not-a-blob-id/x", headers=_basic("alice", "bridge-token"))
        assert response.status_code == 404
