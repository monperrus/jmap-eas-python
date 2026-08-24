from __future__ import annotations

from email.message import EmailMessage as StdEmailMessage

import pytest
from pyactivesync.exceptions import StatusError
from pyactivesync.models import (
    AttachmentInfo,
    BodyType,
    EmailAddResult,
    EmailChangesResult,
    FetchedItem,
    FolderType,
    ItemBody,
)

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.backend.sync import SyncCoordinator
from jmap_eas.config import PolicyConfig
from jmap_eas.errors import InvalidArgumentsError
from jmap_eas.jmap import blob as blob_module
from jmap_eas.jmap import email
from jmap_eas.jmap.dispatcher import Environment
from jmap_eas.models import EmailAddress, EmailRecord, MailboxRecord
from jmap_eas.store import blobs as store_blobs
from jmap_eas.store import cache, db, state


def _mime_bytes(*, text="hello world", html=None, attachment=False) -> bytes:
    msg = StdEmailMessage()
    msg["Subject"] = "Hello"
    msg["From"] = "a@example.com"
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    if attachment:
        msg.add_attachment(b"file-bytes", maintype="application", subtype="octet-stream", filename="f.bin")
    return msg.as_bytes()


class FakeClient:
    def __init__(self, *, mime: bytes | None = None, attachments=None, fail=False):
        self._mime = mime if mime is not None else _mime_bytes()
        self._attachments = attachments or []
        self._fail = fail
        self.apply_calls: list[tuple] = []
        self.draft_calls: list[tuple] = []
        self.move_calls: list[tuple] = []

    def provision(self):
        return "policy-key"

    def list_folders(self):
        return []

    def sync_folder(self, *a, **k):
        raise NotImplementedError

    def fetch_item(self, folder_id, item_id, *, body_type=BodyType.HTML):
        if self._fail:
            from pyactivesync.exceptions import StatusError
            raise StatusError("ItemOperations", "12")
        return FetchedItem(fields={}, bodies=[ItemBody(type=body_type, data=self._mime)], attachments=self._attachments)

    def fetch_attachment(self, file_reference):
        raise NotImplementedError

    def create_folder(self, *a, **k):
        raise NotImplementedError

    def update_folder(self, *a, **k):
        raise NotImplementedError

    def delete_folder(self, *a, **k):
        raise NotImplementedError

    def apply_email_changes(self, folder_id, sync_key, changes, *, deletes_as_moves=True):
        self.apply_calls.append((folder_id, sync_key, list(changes), deletes_as_moves))
        if self._fail:
            raise StatusError("Sync", "12")
        return EmailChangesResult(sync_key="new-key", statuses={c.server_id: "1" for c in changes})

    def create_email_draft(self, folder_id, sync_key, message, *, read=False, flagged=False, client_id=None):
        self.draft_calls.append((folder_id, sync_key, message, read, flagged, client_id))
        if self._fail:
            raise StatusError("Sync", "12")
        return EmailAddResult(sync_key="new-key", client_id=client_id or "cid", status="1", server_id="9:new")

    def move_item(self, item_id, src_folder_id, dst_folder_id):
        self.move_calls.append((item_id, src_folder_id, dst_folder_id))
        if self._fail:
            raise StatusError("MoveItems", "1")
        return "10:new"

    def close(self):
        pass


def _env(tmp_path, *, client=None):
    database = db.connect(tmp_path / "bridge.sqlite3")
    adapter = EasAdapter(client or FakeClient())
    env = Environment(
        account_id="alice", database=database, sync=SyncCoordinator(database), adapter=adapter, policy=PolicyConfig()
    )
    return env, database


def _seed(database, **overrides) -> EmailRecord:
    defaults = dict(
        account_id="alice", email_id="e1", mailbox_id="1", server_id="9:1", thread_id="t1",
        subject="Hello", from_addresses=[EmailAddress(email="a@example.com", name="A")],
        received_at="2026-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    record = EmailRecord(**defaults)
    with database.transaction() as conn:
        cache.upsert_email(conn, record)
        state.append_change(conn, "alice", "Email", record.email_id, "created")
    return record


def _seed_mailbox(database, **overrides) -> MailboxRecord:
    defaults = dict(account_id="alice", mailbox_id="1", parent_id="0", name="Inbox",
                     folder_type=int(FolderType.INBOX), sync_key="5")
    defaults.update(overrides)
    record = MailboxRecord(**defaults)
    with database.transaction() as conn:
        cache.upsert_mailbox(conn, record)
    return record


# -- get: cheap properties --------------------------------------------------------


def test_get_requires_ids():
    env = Environment(account_id="alice", database=None, sync=None, adapter=None, policy=PolicyConfig())
    with pytest.raises(InvalidArgumentsError):
        email.get(env, {})


def test_get_returns_cheap_properties_without_live_fetch(tmp_path):
    class ExplodingClient(FakeClient):
        def fetch_item(self, *a, **k):
            raise AssertionError("must not fetch_item for cheap-only properties")

    env, database = _env(tmp_path, client=ExplodingClient())
    _seed(database)
    result = email.get(env, {"ids": ["e1"]})
    assert result["list"] == [{
        "id": "e1",
        "blobId": result["list"][0]["blobId"],
        "mailboxIds": {"1": True},
        "threadId": "t1",
        "subject": "Hello",
        "from": [{"name": "A", "email": "a@example.com"}],
        "to": None,
        "cc": None,
        "replyTo": None,
        "receivedAt": "2026-01-01T00:00:00Z",
        "keywords": {},
    }]


def test_get_reports_not_found(tmp_path):
    env, database = _env(tmp_path)
    _seed(database)
    result = email.get(env, {"ids": ["e1", "missing"]})
    assert [e["id"] for e in result["list"]] == ["e1"]
    assert result["notFound"] == ["missing"]


def test_get_rejects_unsupported_property(tmp_path):
    env, database = _env(tmp_path)
    _seed(database)
    with pytest.raises(InvalidArgumentsError):
        email.get(env, {"ids": ["e1"], "properties": ["notAProperty"]})


def test_get_keywords_reflect_seen_and_flagged(tmp_path):
    env, database = _env(tmp_path)
    _seed(database, seen=True, flagged=True)
    result = email.get(env, {"ids": ["e1"], "properties": ["keywords"]})
    assert result["list"][0]["keywords"] == {"$seen": True, "$flagged": True}


def test_get_always_null_properties_returned_as_null(tmp_path):
    env, database = _env(tmp_path)
    _seed(database)
    result = email.get(env, {"ids": ["e1"], "properties": ["bcc", "sentAt", "messageId"]})
    assert result["list"][0] == {"id": "e1", "bcc": None, "sentAt": None, "messageId": None}


# -- get: live properties ----------------------------------------------------------


def test_get_live_properties_trigger_fetch_and_parse_mime(tmp_path):
    client = FakeClient(mime=_mime_bytes(text="body text"))
    env, database = _env(tmp_path, client=client)
    _seed(database)
    result = email.get(env, {"ids": ["e1"], "properties": ["size", "preview", "hasAttachment"]})
    obj = result["list"][0]
    assert obj["size"] == len(client._mime)
    assert "body text" in obj["preview"]
    assert obj["hasAttachment"] is False


def test_get_live_fetch_reports_attachments(tmp_path):
    attachment = AttachmentInfo(file_reference="ref1", display_name="f.bin", content_type="application/octet-stream",
                                 estimated_data_size=10, is_inline=False)
    client = FakeClient(attachments=[attachment])
    env, database = _env(tmp_path, client=client)
    _seed(database)
    result = email.get(env, {"ids": ["e1"], "properties": ["hasAttachment", "attachments"]})
    obj = result["list"][0]
    assert obj["hasAttachment"] is True
    assert obj["attachments"][0]["name"] == "f.bin"
    assert obj["attachments"][0]["blobId"]


def test_get_text_body_values_only_populated_when_requested(tmp_path):
    client = FakeClient(mime=_mime_bytes(text="plain text", html="<p>html text</p>"))
    env, database = _env(tmp_path, client=client)
    _seed(database)

    without = email.get(env, {"ids": ["e1"], "properties": ["textBody", "htmlBody", "bodyValues"]})
    assert without["list"][0]["bodyValues"] == {}

    with_text = email.get(env, {"ids": ["e1"], "properties": ["bodyValues"], "fetchTextBodyValues": True})
    assert with_text["list"][0]["bodyValues"]["text"]["value"].strip() == "plain text"
    assert "html" not in with_text["list"][0]["bodyValues"]

    with_all = email.get(env, {"ids": ["e1"], "properties": ["bodyValues"], "fetchAllBodyValues": True})
    assert set(with_all["list"][0]["bodyValues"]) == {"text", "html"}


def test_get_degrades_gracefully_when_live_fetch_fails(tmp_path):
    env, database = _env(tmp_path, client=FakeClient(fail=True))
    _seed(database)
    result = email.get(env, {"ids": ["e1"], "properties": ["size", "subject"]})
    obj = result["list"][0]
    assert obj["subject"] == "Hello"
    assert obj["size"] is None


# -- query --------------------------------------------------------------------------


def test_query_filters_by_in_mailbox(tmp_path):
    env, database = _env(tmp_path)
    _seed(database, email_id="e1", mailbox_id="1")
    _seed(database, email_id="e2", mailbox_id="2", server_id="9:2")
    result = email.query(env, {"filter": {"inMailbox": "1"}})
    assert result["ids"] == ["e1"]


def test_query_filters_by_has_keyword(tmp_path):
    env, database = _env(tmp_path)
    _seed(database, email_id="e1", seen=True)
    _seed(database, email_id="e2", server_id="9:2", seen=False)
    result = email.query(env, {"filter": {"hasKeyword": "$seen"}})
    assert result["ids"] == ["e1"]


def test_query_filters_by_not_keyword(tmp_path):
    env, database = _env(tmp_path)
    _seed(database, email_id="e1", flagged=True)
    _seed(database, email_id="e2", server_id="9:2", flagged=False)
    result = email.query(env, {"filter": {"notKeyword": "$flagged"}})
    assert result["ids"] == ["e2"]


def test_query_filters_by_subject_substring(tmp_path):
    env, database = _env(tmp_path)
    _seed(database, email_id="e1", subject="Quarterly report")
    _seed(database, email_id="e2", server_id="9:2", subject="Lunch")
    result = email.query(env, {"filter": {"subject": "report"}})
    assert result["ids"] == ["e1"]


def test_query_filters_by_text_across_fields(tmp_path):
    env, database = _env(tmp_path)
    _seed(database, email_id="e1", from_addresses=[EmailAddress(email="bob@example.com", name="Bob")])
    _seed(database, email_id="e2", server_id="9:2", from_addresses=[EmailAddress(email="carol@example.com")])
    result = email.query(env, {"filter": {"text": "bob"}})
    assert result["ids"] == ["e1"]


def test_query_before_and_after(tmp_path):
    env, database = _env(tmp_path)
    _seed(database, email_id="e1", received_at="2026-01-01T00:00:00Z")
    _seed(database, email_id="e2", server_id="9:2", received_at="2026-02-01T00:00:00Z")
    result = email.query(env, {"filter": {"before": "2026-01-15T00:00:00Z"}})
    assert result["ids"] == ["e1"]
    result2 = email.query(env, {"filter": {"after": "2026-01-15T00:00:00Z"}})
    assert result2["ids"] == ["e2"]


def test_query_operator_and(tmp_path):
    env, database = _env(tmp_path)
    _seed(database, email_id="e1", subject="Report", seen=True)
    _seed(database, email_id="e2", server_id="9:2", subject="Report", seen=False)
    result = email.query(env, {"filter": {"operator": "AND", "conditions": [
        {"subject": "Report"}, {"hasKeyword": "$seen"}
    ]}})
    assert result["ids"] == ["e1"]


def test_query_sorts_by_received_at_default_descending(tmp_path):
    env, database = _env(tmp_path)
    _seed(database, email_id="e1", received_at="2026-01-01T00:00:00Z")
    _seed(database, email_id="e2", server_id="9:2", received_at="2026-01-02T00:00:00Z")
    result = email.query(env, {})
    assert result["ids"] == ["e2", "e1"]


def test_query_position_and_limit(tmp_path):
    env, database = _env(tmp_path)
    _seed(database, email_id="e1", received_at="2026-01-01T00:00:00Z")
    _seed(database, email_id="e2", server_id="9:2", received_at="2026-01-02T00:00:00Z")
    _seed(database, email_id="e3", server_id="9:3", received_at="2026-01-03T00:00:00Z")
    result = email.query(env, {"position": 1, "limit": 1})
    assert result["ids"] == ["e2"]


def test_query_rejects_unsupported_sort_property(tmp_path):
    env, database = _env(tmp_path)
    _seed(database)
    with pytest.raises(InvalidArgumentsError):
        email.query(env, {"sort": [{"property": "size"}]})


# -- changes -------------------------------------------------------------------------


def test_changes_reports_created(tmp_path):
    env, database = _env(tmp_path)
    _seed(database, email_id="e1")
    result = email.changes(env, {"sinceState": "0"})
    assert result["created"] == ["e1"]


def test_changes_requires_since_state(tmp_path):
    env, database = _env(tmp_path)
    with pytest.raises(InvalidArgumentsError):
        email.changes(env, {})


# -- set: create (drafts only) -------------------------------------------------------


def test_set_create_draft_success(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="3", folder_type=int(FolderType.DRAFTS), sync_key="7")

    result = email.set_(env, {
        "create": {
            "c1": {
                "mailboxIds": {"3": True},
                "keywords": {"$draft": True},
                "subject": "Hi",
                "from": [{"email": "me@example.com", "name": "Me"}],
                "to": [{"email": "you@example.com"}],
                "bodyValues": {"b": {"value": "hello"}},
                "textBody": [{"partId": "b", "type": "text/plain"}],
            }
        }
    })

    assert result["notCreated"] == {}
    email_id = result["created"]["c1"]["id"]
    stored = cache.get_email(database.conn, "alice", email_id)
    assert stored.subject == "Hi"
    assert stored.mailbox_id == "3"
    folder_id, sync_key, message, read, flagged, client_id = client.draft_calls[0]
    assert folder_id == "3"
    assert sync_key == "7"
    assert message["Subject"] == "Hi"
    assert message["To"] == "you@example.com"
    assert 1 <= len(client_id) <= 40


def test_set_create_rejects_non_drafts_mailbox(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="1", folder_type=int(FolderType.INBOX))
    result = email.set_(env, {
        "create": {"c1": {"mailboxIds": {"1": True}, "keywords": {"$draft": True}, "subject": "Hi"}}
    })
    assert result["notCreated"]["c1"]["type"] == "invalidProperties"
    assert result["notCreated"]["c1"]["properties"] == ["mailboxIds"]


def test_set_create_rejects_multiple_mailboxes(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="3", folder_type=int(FolderType.DRAFTS))
    _seed_mailbox(database, mailbox_id="1", folder_type=int(FolderType.INBOX))
    result = email.set_(env, {
        "create": {"c1": {"mailboxIds": {"3": True, "1": True}, "keywords": {"$draft": True}}}
    })
    assert result["notCreated"]["c1"]["type"] == "invalidProperties"


def test_set_create_requires_draft_keyword(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="3", folder_type=int(FolderType.DRAFTS))
    result = email.set_(env, {"create": {"c1": {"mailboxIds": {"3": True}, "subject": "Hi"}}})
    assert result["notCreated"]["c1"]["type"] == "invalidProperties"
    assert result["notCreated"]["c1"]["properties"] == ["keywords"]


def test_set_create_rejects_unsupported_keyword(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="3", folder_type=int(FolderType.DRAFTS))
    result = email.set_(env, {
        "create": {"c1": {"mailboxIds": {"3": True}, "keywords": {"$draft": True, "$important": True}}}
    })
    assert result["notCreated"]["c1"]["type"] == "invalidProperties"


def test_set_create_sets_read_and_flagged_from_keywords(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="3", folder_type=int(FolderType.DRAFTS))
    email.set_(env, {
        "create": {"c1": {"mailboxIds": {"3": True}, "keywords": {"$draft": True, "$seen": True, "$flagged": True}}}
    })
    _, _, _, read, flagged, _ = client.draft_calls[0]
    assert read is True
    assert flagged is True


def test_set_create_backend_failure(tmp_path):
    client = FakeClient(fail=True)
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="3", folder_type=int(FolderType.DRAFTS))
    result = email.set_(env, {"create": {"c1": {"mailboxIds": {"3": True}, "keywords": {"$draft": True}}}})
    assert result["notCreated"]["c1"]["type"] == "serverFail"


def test_set_create_attachment_from_uploaded_blob(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="3", folder_type=int(FolderType.DRAFTS))
    with database.transaction() as conn:
        store_blobs.insert_blob(conn, "alice", "u1", "text/plain", b"file contents")
    blob_id = blob_module.encode_upload_blob_id("u1")

    result = email.set_(env, {
        "create": {"c1": {
            "mailboxIds": {"3": True}, "keywords": {"$draft": True},
            "attachments": [{"blobId": blob_id, "name": "notes.txt", "type": "text/plain"}],
        }}
    })
    assert result["notCreated"] == {}
    message = client.draft_calls[0][2]
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content() == "file contents"  # text/plain: decoded to str, not raw bytes
    assert attachments[0].get_filename() == "notes.txt"


def test_set_create_rejects_unresolvable_attachment_blob(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="3", folder_type=int(FolderType.DRAFTS))
    result = email.set_(env, {
        "create": {"c1": {
            "mailboxIds": {"3": True}, "keywords": {"$draft": True},
            "attachments": [{"blobId": "not-a-real-blob-id"}],
        }}
    })
    assert result["notCreated"]["c1"]["type"] == "invalidProperties"


# -- set: update (keywords, move) ------------------------------------------------------


def test_set_update_keywords_full_replace(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="1", sync_key="5")
    _seed(database, mailbox_id="1", server_id="9:1", seen=False, flagged=False)

    result = email.set_(env, {"update": {"e1": {"keywords": {"$seen": True}}}})

    assert result["updated"] == {"e1": None}
    stored = cache.get_email(database.conn, "alice", "e1")
    assert stored.seen is True
    assert stored.flagged is False
    assert client.apply_calls[0][0] == "1"


def test_set_update_keywords_patch_path(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="1", sync_key="5")
    _seed(database, mailbox_id="1", server_id="9:1", seen=False, flagged=False)

    email.set_(env, {"update": {"e1": {"keywords/$flagged": True}}})

    stored = cache.get_email(database.conn, "alice", "e1")
    assert stored.flagged is True
    assert stored.seen is False  # untouched


def test_set_update_rejects_unsupported_keyword(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="1")
    _seed(database, mailbox_id="1", server_id="9:1")
    result = email.set_(env, {"update": {"e1": {"keywords": {"$important": True}}}})
    assert result["notUpdated"]["e1"]["type"] == "invalidProperties"


def test_set_update_rejects_unsupported_property(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="1")
    _seed(database, mailbox_id="1", server_id="9:1")
    result = email.set_(env, {"update": {"e1": {"subject": "New subject"}}})
    assert result["notUpdated"]["e1"]["type"] == "invalidProperties"
    assert result["notUpdated"]["e1"]["properties"] == ["subject"]


def test_set_update_not_found(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    result = email.set_(env, {"update": {"missing": {"keywords": {"$seen": True}}}})
    assert result["notUpdated"]["missing"]["type"] == "notFound"


def test_set_update_move_success(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="1")
    _seed_mailbox(database, mailbox_id="2", name="Archive")
    _seed(database, mailbox_id="1", server_id="9:1")

    result = email.set_(env, {"update": {"e1": {"mailboxIds": {"2": True}}}})

    assert result["updated"] == {"e1": None}
    stored = cache.get_email(database.conn, "alice", "e1")
    assert stored.mailbox_id == "2"
    assert client.move_calls == [("9:1", "1", "2")]


def test_set_update_move_forbidden_when_policy_disallows(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    env.policy.allow_moves = False
    _seed_mailbox(database, mailbox_id="1")
    _seed_mailbox(database, mailbox_id="2")
    _seed(database, mailbox_id="1", server_id="9:1")

    result = email.set_(env, {"update": {"e1": {"mailboxIds": {"2": True}}}})

    assert result["notUpdated"]["e1"]["type"] == "forbidden"
    assert client.move_calls == []


def test_set_update_move_to_unknown_mailbox_rejected(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="1")
    _seed(database, mailbox_id="1", server_id="9:1")
    result = email.set_(env, {"update": {"e1": {"mailboxIds": {"missing": True}}}})
    assert result["notUpdated"]["e1"]["type"] == "invalidProperties"


def test_set_update_multiple_mailboxes_rejected(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="1")
    _seed_mailbox(database, mailbox_id="2")
    _seed(database, mailbox_id="1", server_id="9:1")
    result = email.set_(env, {"update": {"e1": {"mailboxIds": {"1": True, "2": True}}}})
    assert result["notUpdated"]["e1"]["type"] == "invalidProperties"


# -- set: destroy ------------------------------------------------------------------------


def test_set_destroy_success(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="1", sync_key="5")
    _seed(database, mailbox_id="1", server_id="9:1")

    result = email.set_(env, {"destroy": ["e1"]})

    assert result["destroyed"] == ["e1"]
    assert cache.get_email(database.conn, "alice", "e1") is None
    assert client.apply_calls[0][3] is True  # deletes_as_moves


def test_set_destroy_forbidden_when_policy_disallows(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    env.policy.allow_delete = False
    _seed_mailbox(database, mailbox_id="1")
    _seed(database, mailbox_id="1", server_id="9:1")

    result = email.set_(env, {"destroy": ["e1"]})

    assert result["notDestroyed"]["e1"]["type"] == "forbidden"
    assert cache.get_email(database.conn, "alice", "e1") is not None


def test_set_destroy_not_found(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    result = email.set_(env, {"destroy": ["missing"]})
    assert result["notDestroyed"]["missing"]["type"] == "notFound"


def test_set_destroy_backend_failure(tmp_path):
    client = FakeClient(fail=True)
    env, database = _env(tmp_path, client=client)
    _seed_mailbox(database, mailbox_id="1")
    _seed(database, mailbox_id="1", server_id="9:1")
    result = email.set_(env, {"destroy": ["e1"]})
    assert result["notDestroyed"]["e1"]["type"] == "serverFail"


def test_set_rejects_malformed_top_level_arguments(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    with pytest.raises(InvalidArgumentsError):
        email.set_(env, {"destroy": "not-a-list"})
