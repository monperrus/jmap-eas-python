from __future__ import annotations

import base64

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
from starlette.testclient import TestClient

from jmap_eas.app import create_app
from jmap_eas.backend.eas import EasAdapter
from jmap_eas.config import AccountConfig, AppConfig, PolicyConfig, ServerConfig
from jmap_eas.registry import AccountRegistry


class FakeClient:
    def __init__(self, *, folders=None, sync_results=None, fetched_item=None, new_folder=None):
        self._folders = folders or []
        self._sync_results = {k: list(v) for k, v in (sync_results or {}).items()}
        self._fetched_item = fetched_item
        self._new_folder = new_folder
        self.delete_folder_calls: list[str] = []
        self.draft_calls: list[tuple] = []
        self.apply_calls: list[tuple] = []
        self.sent_mail: list[tuple] = []

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

    def create_folder(self, display_name, parent_id="0", type=FolderType.USER_MAIL):
        folder = self._new_folder or Folder(id="new1", parent_id=parent_id, type=type, name=display_name)
        self._folders.append(folder)
        return folder

    def update_folder(self, folder_id, display_name, parent_id="0"):
        pass

    def delete_folder(self, folder_id):
        self.delete_folder_calls.append(folder_id)
        self._folders = [f for f in self._folders if f.id != folder_id]

    def apply_email_changes(self, folder_id, sync_key, changes, *, deletes_as_moves=True):
        self.apply_calls.append((folder_id, sync_key, list(changes), deletes_as_moves))
        return EmailChangesResult(sync_key="new-key", statuses={c.server_id: "1" for c in changes})

    def create_email_draft(self, folder_id, sync_key, message, *, read=False, flagged=False, client_id=None):
        self.draft_calls.append((folder_id, sync_key, message, read, flagged, client_id))
        return EmailAddResult(sync_key="new-key", client_id=client_id or "cid", status="1", server_id="9:new")

    def move_item(self, item_id, src_folder_id, dst_folder_id):
        return "10:new"

    def send_mail(self, message, *, save_in_sent_items=True, client_id=None):
        self.sent_mail.append((message, client_id))

    def ping(self, folder_ids, *, folder_class="Email", heartbeat=60, timeout=None):
        from pyactivesync.models import PingResult
        return PingResult(status="2", changed_folder_ids=[])

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


USING = [
    "urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail", "urn:ietf:params:jmap:submission",
]


def _app_with_fake_client(tmp_path, client: FakeClient, *, policy: PolicyConfig | None = None):
    config = AppConfig(
        server=ServerConfig(db_path=str(tmp_path / "bridge.sqlite3")), accounts={"alice": _account()},
        policy=policy or PolicyConfig(),
    )
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
        response = client.post("/api", json={"using": USING, "methodCalls": []})
        assert response.status_code == 401


def test_api_core_echo(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post(
            "/api",
            headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Core/echo", {"hello": "world"}, "c0"]]},
        )
        assert response.status_code == 200
        assert response.json()["methodResponses"] == [["Core/echo", {"hello": "world"}, "c0"]]


def test_api_malformed_request_returns_400(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"), json={"using": USING, "notMethodCalls": []}
        )
        assert response.status_code == 400


def test_healthz_reports_request_metrics(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        before = client.get("/healthz").json()["metrics"]
        client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Core/echo", {}, "c0"]]},
        )
        client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Unknown/method", {}, "c0"]]},
        )
        after = client.get("/healthz").json()["metrics"]
        assert after["requests_total"] == before["requests_total"] + 2
        assert after["errors_total"] == before["errors_total"] + 1


def test_healthz_reports_sync_failures_but_request_still_succeeds(tmp_path):
    class BrokenSyncClient(FakeClient):
        def list_folders(self):
            raise RuntimeError("network is down")

    app = _app_with_fake_client(tmp_path, BrokenSyncClient())
    with TestClient(app) as client:
        before = client.get("/healthz").json()["metrics"]
        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Core/echo", {}, "c0"]]},
        )
        assert response.status_code == 200
        assert response.json()["methodResponses"] == [["Core/echo", {}, "c0"]]
        after = client.get("/healthz").json()["metrics"]
        assert after["sync_failures_total"] == before["sync_failures_total"] + 1


def test_api_missing_using_is_rejected(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"methodCalls": [["Core/echo", {}, "c0"]]},
        )
        assert response.status_code == 400
        assert response.json()["type"] == "notRequest"


def test_api_missing_core_capability_in_using_is_rejected(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": ["urn:ietf:params:jmap:mail"], "methodCalls": [["Core/echo", {}, "c0"]]},
        )
        assert response.status_code == 400
        assert response.json()["type"] == "notRequest"


def test_api_unknown_capability_in_using_is_rejected(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": ["urn:ietf:params:jmap:core", "urn:example:nonsense"],
                  "methodCalls": [["Core/echo", {}, "c0"]]},
        )
        assert response.status_code == 400
        assert response.json()["type"] == "unknownCapability"


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
            json={"using": USING, "methodCalls": [["Mailbox/get", {"accountId": "alice"}, "c0"]]},
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
                "using": USING,
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


# -- upload ---------------------------------------------------------------------------


def test_upload_requires_auth(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post("/upload/alice", content=b"hello")
        assert response.status_code == 401


def test_upload_then_download_round_trips(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post(
            "/upload/alice", headers={**_basic("alice", "bridge-token"), "Content-Type": "text/plain"},
            content=b"hello world",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["accountId"] == "alice"
        assert body["size"] == 11
        assert body["type"] == "text/plain"

        download = client.get(f"/download/alice/{body['blobId']}/f.txt", headers=_basic("alice", "bridge-token"))
        assert download.status_code == 200
        assert download.content == b"hello world"
        assert download.headers["content-type"].startswith("text/plain")


def test_upload_requires_matching_account(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post("/upload/bob", headers=_basic("alice", "bridge-token"), content=b"x")
        assert response.status_code == 404


# -- Mailbox/set ------------------------------------------------------------------------


def test_mailbox_set_create_and_destroy(tmp_path):
    client_fake = FakeClient(new_folder=Folder(id="new1", parent_id="0", type=FolderType.USER_MAIL, name="Test"))
    app = _app_with_fake_client(tmp_path, client_fake)
    with TestClient(app) as client:
        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING,
                  "methodCalls": [["Mailbox/set", {"accountId": "alice", "create": {"c1": {"name": "Test"}}}, "c0"]]},
        )
        result = response.json()["methodResponses"][0][1]
        assert result["created"] == {"c1": {"id": "new1"}}

        destroy_response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Mailbox/set", {"accountId": "alice", "destroy": ["new1"]}, "c0"]]},
        )
        destroy_result = destroy_response.json()["methodResponses"][0][1]
        assert destroy_result["destroyed"] == ["new1"]
        assert client_fake.delete_folder_calls == ["new1"]


def test_mailbox_set_destroy_forbidden_by_policy(tmp_path):
    client_fake = FakeClient(new_folder=Folder(id="new1", parent_id="0", type=FolderType.USER_MAIL, name="Test"))
    app = _app_with_fake_client(tmp_path, client_fake, policy=PolicyConfig(allow_delete=False))
    with TestClient(app) as client:
        client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING,
                  "methodCalls": [["Mailbox/set", {"accountId": "alice", "create": {"c1": {"name": "Test"}}}, "c0"]]},
        )
        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Mailbox/set", {"accountId": "alice", "destroy": ["new1"]}, "c0"]]},
        )
        result = response.json()["methodResponses"][0][1]
        assert result["notDestroyed"]["new1"]["type"] == "forbidden"
        assert client_fake.delete_folder_calls == []


# -- Email/set --------------------------------------------------------------------------


def test_email_set_create_draft_with_uploaded_attachment(tmp_path):
    client_fake = FakeClient(
        folders=[Folder(id="3", parent_id="0", type=FolderType.DRAFTS, name="Drafts")],
        sync_results={
            "3": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(sync_key="2", added=[], changed=[], deleted=[], more_available=False),
            ]
        },
    )
    app = _app_with_fake_client(tmp_path, client_fake)
    with TestClient(app) as client:
        # Sync once so the Drafts mailbox is cached (needed for the mailboxIds role check).
        client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Mailbox/get", {"accountId": "alice"}, "c0"]]},
        )

        upload = client.post(
            "/upload/alice", headers={**_basic("alice", "bridge-token"), "Content-Type": "text/plain"},
            content=b"attachment data",
        )
        blob_id = upload.json()["blobId"]

        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Email/set", {
                "accountId": "alice",
                "create": {
                    "c1": {
                        "mailboxIds": {"3": True},
                        "keywords": {"$draft": True},
                        "subject": "Draft",
                        "attachments": [{"blobId": blob_id, "name": "f.txt", "type": "text/plain"}],
                    }
                },
            }, "c0"]]},
        )
        result = response.json()["methodResponses"][0][1]
        assert result["notCreated"] == {}
        assert "id" in result["created"]["c1"]
        assert client_fake.draft_calls[0][0] == "3"


def test_email_set_destroy_forbidden_by_policy(tmp_path):
    client_fake = FakeClient(
        folders=[Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")],
        sync_results={
            "1": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(
                    sync_key="2",
                    added=[SyncItem(server_id="9:1", fields={"Email.Subject": "Hi"})],
                    changed=[], deleted=[], more_available=False,
                ),
            ]
        },
    )
    app = _app_with_fake_client(tmp_path, client_fake, policy=PolicyConfig(allow_delete=False))
    with TestClient(app) as client:
        mailbox_get = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Email/query", {"accountId": "alice"}, "c0"]]},
        )
        email_id = mailbox_get.json()["methodResponses"][0][1]["ids"][0]

        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Email/set", {"accountId": "alice", "destroy": [email_id]}, "c0"]]},
        )
        result = response.json()["methodResponses"][0][1]
        assert result["notDestroyed"][email_id]["type"] == "forbidden"
        assert client_fake.apply_calls == []


# -- Identity/get and EmailSubmission/set ----------------------------------------------------


def test_identity_get_returns_configured_email(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Identity/get", {"accountId": "alice"}, "c0"]]},
        )
        result = response.json()["methodResponses"][0][1]
        assert result["list"][0]["email"] == "alice@example.com"


def test_email_submission_set_sends_referenced_email(tmp_path):
    client_fake = FakeClient(
        folders=[Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")],
        sync_results={
            "1": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(
                    sync_key="2", added=[SyncItem(server_id="9:1", fields={"Email.Subject": "Hi"})],
                    changed=[], deleted=[], more_available=False,
                ),
            ]
        },
        fetched_item=FetchedItem(fields={}, bodies=[ItemBody(type=BodyType.MIME, data=b"Subject: Hi\r\n\r\nBody")],
                                  attachments=[]),
    )
    app = _app_with_fake_client(tmp_path, client_fake)
    with TestClient(app) as client:
        query = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Email/query", {"accountId": "alice"}, "c0"]]},
        )
        email_id = query.json()["methodResponses"][0][1]["ids"][0]

        identity_response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Identity/get", {"accountId": "alice"}, "cid"]]},
        )
        identity_id = identity_response.json()["methodResponses"][0][1]["list"][0]["id"]

        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["EmailSubmission/set", {
                "accountId": "alice",
                "create": {"s1": {"identityId": identity_id, "emailId": email_id}},
            }, "c1"]]},
        )
        result = response.json()["methodResponses"][0][1]
        assert result["notCreated"] == {}
        assert result["created"]["s1"]["undoStatus"] == "final"
        assert len(client_fake.sent_mail) == 1


def test_email_submission_set_forbidden_by_policy(tmp_path):
    client_fake = FakeClient(
        folders=[Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")],
        sync_results={
            "1": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(
                    sync_key="2", added=[SyncItem(server_id="9:1", fields={"Email.Subject": "Hi"})],
                    changed=[], deleted=[], more_available=False,
                ),
            ]
        },
    )
    app = _app_with_fake_client(tmp_path, client_fake, policy=PolicyConfig(allow_send=False))
    with TestClient(app) as client:
        query = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["Email/query", {"accountId": "alice"}, "c0"]]},
        )
        email_id = query.json()["methodResponses"][0][1]["ids"][0]

        response = client.post(
            "/api", headers=_basic("alice", "bridge-token"),
            json={"using": USING, "methodCalls": [["EmailSubmission/set", {
                "accountId": "alice", "create": {"s1": {"identityId": "identity", "emailId": email_id}},
            }, "c0"]]},
        )
        result = response.json()["methodResponses"][0][1]
        assert result["notCreated"]["s1"]["type"] == "forbidden"
        assert client_fake.sent_mail == []

        session = client.get("/.well-known/jmap", headers=_basic("alice", "bridge-token"))
        assert "urn:ietf:params:jmap:submission" not in session.json()["accounts"]["alice"]["accountCapabilities"]


# -- eventsource --------------------------------------------------------------------------


def test_eventsource_requires_auth(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.get("/eventsource")
        assert response.status_code == 401


def test_eventsource_closeafter_state_sends_one_event_and_closes(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.get(
            "/eventsource?closeafter=state", headers=_basic("alice", "bridge-token")
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: state" in response.text
        assert '"@type": "StateChange"' in response.text
        assert '"alice"' in response.text


def test_eventsource_closeafter_state_respects_types_filter(tmp_path):
    app = _app_with_fake_client(tmp_path, FakeClient())
    with TestClient(app) as client:
        response = client.get(
            "/eventsource?closeafter=state&types=Mailbox", headers=_basic("alice", "bridge-token")
        )
        payload = response.text.split("data: ", 1)[1]
        import json
        changed = json.loads(payload)["changed"]["alice"]
        assert set(changed) == {"Mailbox"}
