from __future__ import annotations

import pytest
from pyactivesync.exceptions import StatusError
from pyactivesync.models import Folder, FolderType

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.backend.sync import SyncCoordinator
from jmap_eas.config import PolicyConfig
from jmap_eas.errors import CannotCalculateChangesError, InvalidArgumentsError
from jmap_eas.jmap import mailbox
from jmap_eas.jmap.dispatcher import Environment
from jmap_eas.models import EmailRecord, MailboxRecord
from jmap_eas.store import cache, db, state


class FakeFolderClient:
    def __init__(self, *, fail: bool = False, new_folder_id: str = "new1") -> None:
        self.fail = fail
        self.new_folder_id = new_folder_id
        self.calls: list[tuple] = []

    def provision(self):
        return "policy-key"

    def list_folders(self):
        return []

    def sync_folder(self, *a, **k):
        raise NotImplementedError

    def fetch_item(self, *a, **k):
        raise NotImplementedError

    def fetch_attachment(self, *a, **k):
        raise NotImplementedError

    def create_folder(self, display_name, parent_id="0", type=FolderType.USER_MAIL):
        self.calls.append(("create", display_name, parent_id))
        if self.fail:
            raise StatusError("FolderCreate", "2")
        return Folder(id=self.new_folder_id, parent_id=parent_id, type=type, name=display_name)

    def update_folder(self, folder_id, display_name, parent_id="0"):
        self.calls.append(("update", folder_id, display_name, parent_id))
        if self.fail:
            raise StatusError("FolderUpdate", "2")

    def delete_folder(self, folder_id):
        self.calls.append(("delete", folder_id))
        if self.fail:
            raise StatusError("FolderDelete", "2")

    def close(self):
        pass


def _env(tmp_path, *, policy=None, client=None) -> tuple[Environment, object]:
    database = db.connect(tmp_path / "bridge.sqlite3")
    adapter = EasAdapter(client) if client is not None else None
    env = Environment(
        account_id="alice", database=database, sync=SyncCoordinator(database), adapter=adapter,
        policy=policy or PolicyConfig(),
    )
    return env, database


def _seed_mailboxes(database) -> None:
    with database.transaction() as conn:
        cache.upsert_mailbox(conn, MailboxRecord("alice", "1", "0", "Inbox", int(FolderType.INBOX), "0"))
        cache.upsert_mailbox(conn, MailboxRecord("alice", "2", "0", "Drafts", int(FolderType.DRAFTS), "0"))
        state.append_change(conn, "alice", "Mailbox", "1", "created")
        state.append_change(conn, "alice", "Mailbox", "2", "created")


def test_get_returns_all_mailboxes_when_ids_is_none(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.get(env, {})
    assert {m["id"] for m in result["list"]} == {"1", "2"}
    assert result["notFound"] == []
    assert result["accountId"] == "alice"


def test_get_reports_not_found_ids(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.get(env, {"ids": ["1", "missing"]})
    assert [m["id"] for m in result["list"]] == ["1"]
    assert result["notFound"] == ["missing"]


def test_get_restricts_to_requested_properties(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.get(env, {"ids": ["1"], "properties": ["name"]})
    assert result["list"] == [{"id": "1", "name": "Inbox"}]


def test_get_maps_role_from_folder_type(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.get(env, {"ids": ["1", "2"]})
    roles = {m["id"]: m["role"] for m in result["list"]}
    assert roles == {"1": "inbox", "2": "drafts"}


def test_get_counts_total_and_unread_emails(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    with database.transaction() as conn:
        cache.upsert_email(conn, EmailRecord("alice", "e1", "1", "9:1", "t1", "A", seen=True))
        cache.upsert_email(conn, EmailRecord("alice", "e2", "1", "9:2", "t1", "B", seen=False))
    result = mailbox.get(env, {"ids": ["1"]})
    assert result["list"][0]["totalEmails"] == 2
    assert result["list"][0]["unreadEmails"] == 1
    assert result["list"][0]["totalThreads"] == 1
    assert result["list"][0]["unreadThreads"] == 1


def test_get_myrights_are_all_read_only_in_m1(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.get(env, {"ids": ["1"]})
    rights = result["list"][0]["myRights"]
    assert rights["mayReadItems"] is True
    assert rights["mayAddItems"] is False


def test_query_filters_by_role(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.query(env, {"filter": {"role": "drafts"}})
    assert result["ids"] == ["2"]


def test_query_filters_by_name_substring_case_insensitive(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.query(env, {"filter": {"name": "draft"}})
    assert result["ids"] == ["2"]


def test_query_filter_and_or_not(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.query(env, {"filter": {"operator": "NOT", "conditions": [{"role": "drafts"}]}})
    assert result["ids"] == ["1"]


def test_query_sorts_by_name(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.query(env, {"sort": [{"property": "name", "isAscending": True}]})
    assert result["ids"] == ["2", "1"]  # "Drafts" < "Inbox"
    result_desc = mailbox.query(env, {"sort": [{"property": "name", "isAscending": False}]})
    assert result_desc["ids"] == ["1", "2"]


def test_query_position_and_limit(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.query(env, {"sort": [{"property": "name"}], "position": 1, "limit": 1})
    assert result["ids"] == ["1"]
    assert result["limit"] == 1


def test_query_calculate_total(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.query(env, {"calculateTotal": True})
    assert result["total"] == 2


def test_query_rejects_unsupported_sort_property(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    with pytest.raises(InvalidArgumentsError):
        mailbox.query(env, {"sort": [{"property": "totalEmails"}]})


def test_changes_returns_created_and_new_state(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    result = mailbox.changes(env, {"sinceState": "0"})
    assert set(result["created"]) == {"1", "2"}
    assert result["newState"] == "2"


def test_changes_rejects_missing_since_state(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    with pytest.raises(InvalidArgumentsError):
        mailbox.changes(env, {})


def test_changes_raises_cannot_calculate_changes_for_future_state(tmp_path):
    env, database = _env(tmp_path)
    _seed_mailboxes(database)
    with pytest.raises(CannotCalculateChangesError):
        mailbox.changes(env, {"sinceState": "999"})


# -- set: create ----------------------------------------------------------------------


def test_set_create_success(tmp_path):
    client = FakeFolderClient(new_folder_id="new1")
    env, database = _env(tmp_path, client=client)
    result = mailbox.set_(env, {"create": {"c1": {"name": "Projects"}}})
    assert result["created"] == {"c1": {"id": "new1"}}
    assert result["notCreated"] == {}
    assert client.calls == [("create", "Projects", "0")]
    assert cache.get_mailbox(database.conn, "alice", "new1").name == "Projects"


def test_set_create_uses_given_parent_id(tmp_path):
    client = FakeFolderClient()
    env, _database = _env(tmp_path, client=client)
    mailbox.set_(env, {"create": {"c1": {"name": "Child", "parentId": "5"}}})
    assert client.calls == [("create", "Child", "5")]


def test_set_create_rejects_missing_name(tmp_path):
    client = FakeFolderClient()
    env, _database = _env(tmp_path, client=client)
    result = mailbox.set_(env, {"create": {"c1": {}}})
    assert result["notCreated"]["c1"]["type"] == "invalidProperties"
    assert client.calls == []


def test_set_create_rejects_role(tmp_path):
    client = FakeFolderClient()
    env, _database = _env(tmp_path, client=client)
    result = mailbox.set_(env, {"create": {"c1": {"name": "X", "role": "inbox"}}})
    assert result["notCreated"]["c1"]["type"] == "invalidProperties"
    assert client.calls == []


def test_set_create_backend_failure_reports_server_fail(tmp_path):
    client = FakeFolderClient(fail=True)
    env, _database = _env(tmp_path, client=client)
    result = mailbox.set_(env, {"create": {"c1": {"name": "X"}}})
    assert result["notCreated"]["c1"]["type"] == "serverFail"


# -- set: update ----------------------------------------------------------------------


def test_set_update_renames(tmp_path):
    client = FakeFolderClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailboxes(database)
    result = mailbox.set_(env, {"update": {"1": {"name": "Renamed"}}})
    assert result["updated"] == {"1": None}
    assert client.calls == [("update", "1", "Renamed", "0")]
    assert cache.get_mailbox(database.conn, "alice", "1").name == "Renamed"


def test_set_update_reparents_uses_existing_name(tmp_path):
    client = FakeFolderClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailboxes(database)
    mailbox.set_(env, {"update": {"1": {"parentId": "2"}}})
    assert client.calls == [("update", "1", "Inbox", "2")]


def test_set_update_no_op_patch_succeeds_without_eas_call(tmp_path):
    client = FakeFolderClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailboxes(database)
    result = mailbox.set_(env, {"update": {"1": {}}})
    assert result["updated"] == {"1": None}
    assert client.calls == []


def test_set_update_not_found(tmp_path):
    client = FakeFolderClient()
    env, _database = _env(tmp_path, client=client)
    result = mailbox.set_(env, {"update": {"missing": {"name": "X"}}})
    assert result["notUpdated"]["missing"]["type"] == "notFound"


def test_set_update_rejects_unsupported_property(tmp_path):
    client = FakeFolderClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailboxes(database)
    result = mailbox.set_(env, {"update": {"1": {"role": "trash"}}})
    assert result["notUpdated"]["1"]["type"] == "invalidProperties"
    assert client.calls == []


def test_set_update_backend_failure_reports_server_fail(tmp_path):
    client = FakeFolderClient(fail=True)
    env, database = _env(tmp_path, client=client)
    _seed_mailboxes(database)
    result = mailbox.set_(env, {"update": {"1": {"name": "X"}}})
    assert result["notUpdated"]["1"]["type"] == "serverFail"


# -- set: destroy ------------------------------------------------------------------------


def test_set_destroy_success_cascades_to_emails(tmp_path):
    client = FakeFolderClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailboxes(database)
    with database.transaction() as conn:
        cache.upsert_email(conn, EmailRecord("alice", "e1", "1", "9:1", "t1", "A"))
    result = mailbox.set_(env, {"destroy": ["1"]})
    assert result["destroyed"] == ["1"]
    assert client.calls == [("delete", "1")]
    assert cache.get_mailbox(database.conn, "alice", "1") is None
    assert cache.get_email(database.conn, "alice", "e1") is None


def test_set_destroy_not_found(tmp_path):
    client = FakeFolderClient()
    env, _database = _env(tmp_path, client=client)
    result = mailbox.set_(env, {"destroy": ["missing"]})
    assert result["notDestroyed"]["missing"]["type"] == "notFound"


def test_set_destroy_forbidden_when_policy_disallows_delete(tmp_path):
    client = FakeFolderClient()
    env, database = _env(tmp_path, policy=PolicyConfig(allow_delete=False), client=client)
    _seed_mailboxes(database)
    result = mailbox.set_(env, {"destroy": ["1"]})
    assert result["notDestroyed"]["1"]["type"] == "forbidden"
    assert client.calls == []
    assert cache.get_mailbox(database.conn, "alice", "1") is not None


def test_set_destroy_backend_failure_reports_server_fail(tmp_path):
    client = FakeFolderClient(fail=True)
    env, database = _env(tmp_path, client=client)
    _seed_mailboxes(database)
    result = mailbox.set_(env, {"destroy": ["1"]})
    assert result["notDestroyed"]["1"]["type"] == "serverFail"


def test_set_state_transitions(tmp_path):
    client = FakeFolderClient()
    env, database = _env(tmp_path, client=client)
    _seed_mailboxes(database)
    with database.transaction() as conn:
        old_state = state.current_state(conn, "alice", "Mailbox")
    result = mailbox.set_(env, {"create": {"c1": {"name": "New"}}})
    assert result["oldState"] == old_state
    assert result["newState"] != old_state
