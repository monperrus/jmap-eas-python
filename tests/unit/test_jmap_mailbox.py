from __future__ import annotations

import pytest
from pyactivesync.models import FolderType

from jmap_eas.backend.sync import SyncCoordinator
from jmap_eas.config import PolicyConfig
from jmap_eas.errors import CannotCalculateChangesError, InvalidArgumentsError
from jmap_eas.jmap import mailbox
from jmap_eas.jmap.dispatcher import Environment
from jmap_eas.models import EmailRecord, MailboxRecord
from jmap_eas.store import cache, db, state


def _env(tmp_path) -> tuple[Environment, object]:
    database = db.connect(tmp_path / "bridge.sqlite3")
    env = Environment(
        account_id="alice", database=database, sync=SyncCoordinator(database), adapter=None, policy=PolicyConfig()
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
