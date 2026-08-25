from __future__ import annotations

import pytest
from pyactivesync.exceptions import StatusError
from pyactivesync.models import BodyType, FetchedItem, ItemBody

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.config import PolicyConfig
from jmap_eas.errors import InvalidArgumentsError
from jmap_eas.jmap import submission
from jmap_eas.jmap.dispatcher import Environment
from jmap_eas.jmap.identity import IDENTITY_ID
from jmap_eas.models import EmailRecord
from jmap_eas.store import cache, db, submissions


class FakeClient:
    def __init__(self, *, fail: bool = False, mime: bytes = b"raw mime bytes"):
        self._fail = fail
        self._mime = mime
        self.sent: list[tuple] = []

    def provision(self):
        return "policy-key"

    def list_folders(self):
        return []

    def sync_folder(self, *a, **k):
        raise NotImplementedError

    def fetch_item(self, folder_id, item_id, *, body_type=BodyType.HTML):
        return FetchedItem(fields={}, bodies=[ItemBody(type=body_type, data=self._mime)], attachments=[])

    def fetch_attachment(self, file_reference):
        raise NotImplementedError

    def create_folder(self, *a, **k):
        raise NotImplementedError

    def update_folder(self, *a, **k):
        raise NotImplementedError

    def delete_folder(self, *a, **k):
        raise NotImplementedError

    def apply_email_changes(self, *a, **k):
        raise NotImplementedError

    def create_email_draft(self, *a, **k):
        raise NotImplementedError

    def move_item(self, *a, **k):
        raise NotImplementedError

    def send_mail(self, message, *, save_in_sent_items=True, client_id=None):
        self.sent.append((message, client_id))
        if self._fail:
            raise StatusError("SendMail", "6")

    def close(self):
        pass


def _env(tmp_path, *, client=None, policy=None):
    database = db.connect(tmp_path / "bridge.sqlite3")
    adapter = EasAdapter(client or FakeClient())
    from jmap_eas.backend.sync import SyncCoordinator

    return Environment(
        account_id="alice", database=database, sync=SyncCoordinator(database), adapter=adapter,
        policy=policy or PolicyConfig(), identity_email="alice@example.com",
    ), database


def _seed_email(database, **overrides) -> EmailRecord:
    defaults = dict(account_id="alice", email_id="e1", mailbox_id="1", server_id="9:1", thread_id="t1",
                     subject="Hello")
    defaults.update(overrides)
    record = EmailRecord(**defaults)
    with database.transaction() as conn:
        cache.upsert_email(conn, record)
    return record


# -- get --------------------------------------------------------------------------------


def test_get_returns_all_submissions_when_ids_is_none(tmp_path):
    env, database = _env(tmp_path)
    with database.transaction() as conn:
        submissions.insert_submission(conn, "alice", "s1", "e1", IDENTITY_ID, "c1", submissions.STATUS_FINAL)
    result = submission.get(env, {})
    assert [s["id"] for s in result["list"]] == ["s1"]
    assert result["list"][0]["undoStatus"] == "final"
    assert result["list"][0]["emailId"] == "e1"


def test_get_reports_not_found(tmp_path):
    env, database = _env(tmp_path)
    result = submission.get(env, {"ids": ["missing"]})
    assert result["list"] == []
    assert result["notFound"] == ["missing"]


# -- set: create --------------------------------------------------------------------------


def test_set_create_sends_and_records_submission(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_email(database)

    result = submission.set_(env, {"create": {"c1": {"identityId": IDENTITY_ID, "emailId": "e1"}}})

    assert result["notCreated"] == {}
    submission_id = result["created"]["c1"]["id"]
    assert result["created"]["c1"]["undoStatus"] == "final"
    assert len(client.sent) == 1
    message, client_id = client.sent[0]
    assert 1 <= len(client_id) <= 40
    stored = submissions.get_submission(database.conn, "alice", submission_id)
    assert stored.status == submissions.STATUS_FINAL
    assert stored.email_id == "e1"


def test_set_create_rejects_unknown_identity(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_email(database)
    result = submission.set_(env, {"create": {"c1": {"identityId": "not-real", "emailId": "e1"}}})
    assert result["notCreated"]["c1"]["type"] == "invalidProperties"
    assert client.sent == []


def test_set_create_rejects_unknown_email(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    result = submission.set_(env, {"create": {"c1": {"identityId": IDENTITY_ID, "emailId": "missing"}}})
    assert result["notCreated"]["c1"]["type"] == "invalidProperties"
    assert client.sent == []


def test_set_create_forbidden_when_policy_disallows_send(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client, policy=PolicyConfig(allow_send=False))
    _seed_email(database)
    result = submission.set_(env, {"create": {"c1": {"identityId": IDENTITY_ID, "emailId": "e1"}}})
    assert result["notCreated"]["c1"]["type"] == "forbidden"
    assert client.sent == []


def test_set_create_backend_failure_records_failed_status(tmp_path):
    client = FakeClient(fail=True)
    env, database = _env(tmp_path, client=client)
    _seed_email(database)
    result = submission.set_(env, {"create": {"c1": {"identityId": IDENTITY_ID, "emailId": "e1"}}})
    assert result["notCreated"]["c1"]["type"] == "serverFail"
    # A record survives the failed send for correlation (plan.md's M3 idempotency note).
    records = submissions.list_submissions(database.conn, "alice")
    assert len(records) == 1
    assert records[0].status == submissions.STATUS_FAILED


def test_set_create_persists_submission_before_sending(tmp_path):
    """The record must exist even if send_mail() were to raise after partially succeeding server-side."""

    class RecordingClient(FakeClient):
        def send_mail(self, message, *, save_in_sent_items=True, client_id=None):
            # At this point, the submission row must already be persisted.
            assert self._existing_count() == 1
            super().send_mail(message, save_in_sent_items=save_in_sent_items, client_id=client_id)

        def _existing_count(self):
            return len(submissions.list_submissions(database.conn, "alice"))

    client = RecordingClient()
    env, database = _env(tmp_path, client=client)
    _seed_email(database)
    result = submission.set_(env, {"create": {"c1": {"identityId": IDENTITY_ID, "emailId": "e1"}}})
    assert result["notCreated"] == {}


def test_set_create_sends_exact_mime_from_referenced_email(tmp_path):
    client = FakeClient(mime=b"Subject: Hi\r\n\r\nBody text")
    env, database = _env(tmp_path, client=client)
    _seed_email(database)
    submission.set_(env, {"create": {"c1": {"identityId": IDENTITY_ID, "emailId": "e1"}}})
    message, _ = client.sent[0]
    assert message["Subject"] == "Hi"


def test_set_rejects_malformed_top_level_arguments(tmp_path):
    env, database = _env(tmp_path)
    with pytest.raises(InvalidArgumentsError):
        submission.set_(env, {"create": "not-a-dict"})


# -- set: update / destroy ----------------------------------------------------------------


def test_set_update_always_rejected(tmp_path):
    env, database = _env(tmp_path)
    with database.transaction() as conn:
        submissions.insert_submission(conn, "alice", "s1", "e1", IDENTITY_ID, "c1", submissions.STATUS_FINAL)
    result = submission.set_(env, {"update": {"s1": {"undoStatus": "canceled"}}})
    assert result["notUpdated"]["s1"]["type"] == "invalidProperties"


def test_set_update_not_found(tmp_path):
    env, database = _env(tmp_path)
    result = submission.set_(env, {"update": {"missing": {}}})
    assert result["notUpdated"]["missing"]["type"] == "notFound"


def test_set_destroy_removes_local_record(tmp_path):
    env, database = _env(tmp_path)
    with database.transaction() as conn:
        submissions.insert_submission(conn, "alice", "s1", "e1", IDENTITY_ID, "c1", submissions.STATUS_FINAL)
    result = submission.set_(env, {"destroy": ["s1"]})
    assert result["destroyed"] == ["s1"]
    assert submissions.get_submission(database.conn, "alice", "s1") is None


def test_set_destroy_not_found(tmp_path):
    env, database = _env(tmp_path)
    result = submission.set_(env, {"destroy": ["missing"]})
    assert result["notDestroyed"]["missing"]["type"] == "notFound"


# -- changes -------------------------------------------------------------------------------


def test_changes_reports_created(tmp_path):
    client = FakeClient()
    env, database = _env(tmp_path, client=client)
    _seed_email(database)
    submission.set_(env, {"create": {"c1": {"identityId": IDENTITY_ID, "emailId": "e1"}}})
    result = submission.changes(env, {"sinceState": "0"})
    assert len(result["created"]) == 1


def test_changes_requires_since_state(tmp_path):
    env, database = _env(tmp_path)
    with pytest.raises(InvalidArgumentsError):
        submission.changes(env, {})
