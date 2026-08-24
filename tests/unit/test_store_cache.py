from __future__ import annotations

from jmap_eas.models import EmailAddress, EmailRecord, MailboxRecord
from jmap_eas.store import cache, db


def _db(tmp_path):
    return db.connect(tmp_path / "bridge.sqlite3")


def test_upsert_and_get_mailbox(tmp_path):
    database = _db(tmp_path)
    mailbox = MailboxRecord(
        account_id="alice", mailbox_id="1", parent_id="0", name="Inbox", folder_type=2, sync_key="0"
    )
    with database.transaction() as conn:
        cache.upsert_mailbox(conn, mailbox)
    assert cache.get_mailbox(database.conn, "alice", "1") == mailbox
    assert cache.get_mailbox(database.conn, "alice", "missing") is None


def test_upsert_mailbox_updates_name_but_not_sync_key(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        cache.upsert_mailbox(conn, MailboxRecord("alice", "1", "0", "Inbox", 2, "0"))
        cache.set_mailbox_sync_key(conn, "alice", "1", "42")
        cache.upsert_mailbox(conn, MailboxRecord("alice", "1", "0", "Renamed", 2, "0"))
    stored = cache.get_mailbox(database.conn, "alice", "1")
    assert stored.name == "Renamed"
    assert stored.sync_key == "42"


def test_delete_mailbox(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        cache.upsert_mailbox(conn, MailboxRecord("alice", "1", "0", "Inbox", 2, "0"))
        cache.delete_mailbox(conn, "alice", "1")
    assert cache.get_mailbox(database.conn, "alice", "1") is None


def test_list_mailboxes_scoped_by_account(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        cache.upsert_mailbox(conn, MailboxRecord("alice", "1", "0", "Inbox", 2, "0"))
        cache.upsert_mailbox(conn, MailboxRecord("alice", "2", "0", "Drafts", 3, "0"))
        cache.upsert_mailbox(conn, MailboxRecord("bob", "1", "0", "Inbox", 2, "0"))
    assert {m.mailbox_id for m in cache.list_mailboxes(database.conn, "alice")} == {"1", "2"}


def test_get_or_create_thread_id_is_stable(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        first = cache.get_or_create_thread_id(conn, "alice", "hello world", "t1")
        second = cache.get_or_create_thread_id(conn, "alice", "hello world", "t2-should-be-ignored")
    assert first == second == "t1"


def test_get_or_create_thread_id_distinct_keys(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        a = cache.get_or_create_thread_id(conn, "alice", "subject a", "ta")
        b = cache.get_or_create_thread_id(conn, "alice", "subject b", "tb")
    assert a != b


def _email(**overrides) -> EmailRecord:
    defaults = dict(
        account_id="alice",
        email_id="e1",
        mailbox_id="1",
        server_id="9:1",
        thread_id="t1",
        subject="Hello",
        from_addresses=[EmailAddress(email="a@example.com", name="A")],
        received_at="2026-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return EmailRecord(**defaults)


def test_upsert_and_get_email_round_trips_addresses(tmp_path):
    database = _db(tmp_path)
    email = _email(to_addresses=[EmailAddress(email="b@example.com", name=None)])
    with database.transaction() as conn:
        cache.upsert_email(conn, email)
    stored = cache.get_email(database.conn, "alice", "e1")
    assert stored == email


def test_upsert_email_updates_in_place(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        cache.upsert_email(conn, _email(seen=False))
        cache.upsert_email(conn, _email(seen=True, flagged=True))
    stored = cache.get_email(database.conn, "alice", "e1")
    assert stored.seen is True
    assert stored.flagged is True


def test_delete_email(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        cache.upsert_email(conn, _email())
        cache.delete_email(conn, "alice", "e1")
    assert cache.get_email(database.conn, "alice", "e1") is None


def test_find_email_id_by_server_id(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        cache.upsert_email(conn, _email())
    assert cache.find_email_id_by_server_id(database.conn, "alice", "1", "9:1") == "e1"
    assert cache.find_email_id_by_server_id(database.conn, "alice", "1", "nope") is None


def test_list_emails_in_mailbox_and_for_account(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        cache.upsert_email(conn, _email(email_id="e1", mailbox_id="1"))
        cache.upsert_email(conn, _email(email_id="e2", mailbox_id="2", server_id="9:2"))
    assert {e.email_id for e in cache.list_emails_in_mailbox(database.conn, "alice", "1")} == {"e1"}
    assert {e.email_id for e in cache.list_emails_for_account(database.conn, "alice")} == {"e1", "e2"}


def test_list_thread_email_ids(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        cache.upsert_email(conn, _email(email_id="e1", thread_id="t1", received_at="2026-01-01T00:00:00Z"))
        cache.upsert_email(conn, _email(email_id="e2", thread_id="t1", server_id="9:2",
                                         received_at="2026-01-02T00:00:00Z"))
        cache.upsert_email(conn, _email(email_id="e3", thread_id="t2", server_id="9:3"))
    assert cache.list_thread_email_ids(database.conn, "alice", "t1") == ["e1", "e2"]
    assert cache.list_thread_email_ids(database.conn, "alice", "t2") == ["e3"]
