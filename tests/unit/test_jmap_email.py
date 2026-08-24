from __future__ import annotations

from email.message import EmailMessage as StdEmailMessage

import pytest
from pyactivesync.models import AttachmentInfo, BodyType, FetchedItem, ItemBody

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.backend.sync import SyncCoordinator
from jmap_eas.config import PolicyConfig
from jmap_eas.errors import InvalidArgumentsError
from jmap_eas.jmap import email
from jmap_eas.jmap.dispatcher import Environment
from jmap_eas.models import EmailAddress, EmailRecord
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
