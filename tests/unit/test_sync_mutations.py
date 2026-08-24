from __future__ import annotations

from email.message import EmailMessage

from pyactivesync.models import EmailAddResult, EmailChangesResult, Folder, FolderType

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.backend.sync import SyncCoordinator
from jmap_eas.models import EmailRecord, MailboxRecord
from jmap_eas.store import cache, db, state


class FakeMutatingClient:
    """A minimal `EasClientProtocol` fake for mutation methods, configured per-test via its `*_result` attrs."""

    def __init__(self) -> None:
        self.apply_calls: list[tuple] = []
        self.move_calls: list[tuple] = []
        self.draft_calls: list[tuple] = []
        self.create_folder_calls: list[tuple] = []
        self.update_folder_calls: list[tuple] = []
        self.delete_folder_calls: list[str] = []
        self.apply_result: EmailChangesResult | None = None
        self.draft_result: EmailAddResult | None = None
        self.move_result: str | None = None
        self.create_folder_result: Folder | None = None

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
        self.create_folder_calls.append((display_name, parent_id, type))
        return self.create_folder_result

    def update_folder(self, folder_id, display_name, parent_id="0"):
        self.update_folder_calls.append((folder_id, display_name, parent_id))

    def delete_folder(self, folder_id):
        self.delete_folder_calls.append(folder_id)

    def apply_email_changes(self, folder_id, sync_key, changes, *, deletes_as_moves=True):
        self.apply_calls.append((folder_id, sync_key, list(changes), deletes_as_moves))
        return self.apply_result

    def create_email_draft(self, folder_id, sync_key, message, *, read=False, flagged=False, client_id=None):
        self.draft_calls.append((folder_id, sync_key, message, read, flagged, client_id))
        return self.draft_result

    def move_item(self, item_id, src_folder_id, dst_folder_id):
        self.move_calls.append((item_id, src_folder_id, dst_folder_id))
        return self.move_result

    def close(self):
        pass


def _setup(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    coordinator = SyncCoordinator(database)
    client = FakeMutatingClient()
    adapter = EasAdapter(client)
    return coordinator, database, client, adapter


def _seed_mailbox(database, **overrides) -> MailboxRecord:
    defaults = dict(account_id="alice", mailbox_id="1", parent_id="0", name="Inbox",
                     folder_type=int(FolderType.INBOX), sync_key="5")
    defaults.update(overrides)
    record = MailboxRecord(**defaults)
    with database.transaction() as conn:
        cache.upsert_mailbox(conn, record)
    return record


def _seed_email(database, **overrides) -> EmailRecord:
    defaults = dict(account_id="alice", email_id="e1", mailbox_id="1", server_id="9:1", thread_id="t1",
                     subject="Hello", seen=False, flagged=False)
    defaults.update(overrides)
    record = EmailRecord(**defaults)
    with database.transaction() as conn:
        cache.upsert_email(conn, record)
    return record


# -- Mailbox mutations --------------------------------------------------------------


def test_create_mailbox(tmp_path):
    coordinator, database, client, adapter = _setup(tmp_path)
    client.create_folder_result = Folder(id="new1", parent_id="0", type=FolderType.USER_MAIL, name="Projects")

    record = coordinator.create_mailbox("alice", "Projects", "0", adapter)

    assert record.mailbox_id == "new1"
    assert client.create_folder_calls == [("Projects", "0", FolderType.USER_MAIL)]
    assert cache.get_mailbox(database.conn, "alice", "new1").name == "Projects"
    assert state.get_changes(database.conn, "alice", "Mailbox", "0").created == ["new1"]


def test_update_mailbox_renames_and_reparents(tmp_path):
    coordinator, database, client, adapter = _setup(tmp_path)
    _seed_mailbox(database, mailbox_id="1", name="Old", parent_id="0")

    coordinator.update_mailbox("alice", "1", "New Name", "0", adapter)

    assert client.update_folder_calls == [("1", "New Name", "0")]
    stored = cache.get_mailbox(database.conn, "alice", "1")
    assert stored.name == "New Name"
    assert stored.sync_key == "5"  # unrelated to the folder-hierarchy mutation
    assert state.get_changes(database.conn, "alice", "Mailbox", "0").updated == ["1"]


def test_delete_mailbox_cascades_to_its_emails(tmp_path):
    coordinator, database, client, adapter = _setup(tmp_path)
    _seed_mailbox(database)
    _seed_email(database, email_id="e1")
    _seed_email(database, email_id="e2", server_id="9:2")

    coordinator.delete_mailbox("alice", "1", adapter)

    assert client.delete_folder_calls == ["1"]
    assert cache.get_mailbox(database.conn, "alice", "1") is None
    assert cache.list_emails_in_mailbox(database.conn, "alice", "1") == []
    email_changes = state.get_changes(database.conn, "alice", "Email", "0")
    assert set(email_changes.destroyed) == {"e1", "e2"}
    mailbox_changes = state.get_changes(database.conn, "alice", "Mailbox", "0")
    assert mailbox_changes.destroyed == ["1"]


# -- Email keyword changes -----------------------------------------------------------


def test_set_email_keywords_success_updates_cache_and_commits_sync_key(tmp_path):
    coordinator, database, client, adapter = _setup(tmp_path)
    _seed_mailbox(database, sync_key="5")
    _seed_email(database, seen=False, flagged=False)
    client.apply_result = EmailChangesResult(sync_key="6", statuses={"9:1": "1"})

    status = coordinator.set_email_keywords("alice", "e1", adapter, seen=True, flagged=None)

    assert status == "1"
    assert client.apply_calls[0][0] == "1"
    assert client.apply_calls[0][1] == "5"
    stored = cache.get_email(database.conn, "alice", "e1")
    assert stored.seen is True
    assert stored.flagged is False  # untouched (flagged=None means "leave as is")
    assert cache.get_mailbox(database.conn, "alice", "1").sync_key == "6"
    assert state.get_changes(database.conn, "alice", "Email", "0").updated == ["e1"]


def test_set_email_keywords_failure_still_commits_sync_key_but_not_cache(tmp_path):
    coordinator, database, client, adapter = _setup(tmp_path)
    _seed_mailbox(database, sync_key="5")
    _seed_email(database, seen=False)
    client.apply_result = EmailChangesResult(sync_key="6", statuses={"9:1": "6"})

    status = coordinator.set_email_keywords("alice", "e1", adapter, seen=True, flagged=None)

    assert status == "6"
    assert cache.get_email(database.conn, "alice", "e1").seen is False
    assert cache.get_mailbox(database.conn, "alice", "1").sync_key == "6"
    assert state.get_changes(database.conn, "alice", "Email", "0").updated == []


def test_set_email_keywords_unknown_email_returns_none(tmp_path):
    coordinator, _database, _client, adapter = _setup(tmp_path)
    assert coordinator.set_email_keywords("alice", "missing", adapter, seen=True, flagged=None) is None


# -- Email delete ---------------------------------------------------------------------


def test_delete_email_success(tmp_path):
    coordinator, database, client, adapter = _setup(tmp_path)
    _seed_mailbox(database, sync_key="5")
    _seed_email(database)
    client.apply_result = EmailChangesResult(sync_key="6", statuses={"9:1": "1"})

    status = coordinator.delete_email("alice", "e1", adapter)

    assert status == "1"
    assert client.apply_calls[0][3] is True  # deletes_as_moves
    assert cache.get_email(database.conn, "alice", "e1") is None
    assert state.get_changes(database.conn, "alice", "Email", "0").destroyed == ["e1"]


def test_delete_email_failure_keeps_cached_row(tmp_path):
    coordinator, database, client, adapter = _setup(tmp_path)
    _seed_mailbox(database, sync_key="5")
    _seed_email(database)
    client.apply_result = EmailChangesResult(sync_key="6", statuses={"9:1": "6"})

    status = coordinator.delete_email("alice", "e1", adapter)

    assert status == "6"
    assert cache.get_email(database.conn, "alice", "e1") is not None


# -- Email move -----------------------------------------------------------------------


def test_move_email_to_different_mailbox(tmp_path):
    coordinator, database, client, adapter = _setup(tmp_path)
    _seed_mailbox(database, mailbox_id="1")
    _seed_mailbox(database, mailbox_id="2", name="Archive")
    _seed_email(database, mailbox_id="1", server_id="9:1")
    client.move_result = "10:1"

    status = coordinator.move_email("alice", "e1", "2", adapter)

    assert status == "moved"
    assert client.move_calls == [("9:1", "1", "2")]
    stored = cache.get_email(database.conn, "alice", "e1")
    assert stored.mailbox_id == "2"
    assert stored.server_id == "10:1"
    assert stored.email_id == "e1"
    assert state.get_changes(database.conn, "alice", "Email", "0").updated == ["e1"]


def test_move_email_to_same_mailbox_is_a_no_op(tmp_path):
    coordinator, database, client, adapter = _setup(tmp_path)
    _seed_mailbox(database, mailbox_id="1")
    _seed_email(database, mailbox_id="1")

    status = coordinator.move_email("alice", "e1", "1", adapter)

    assert status == "unchanged"
    assert client.move_calls == []


def test_move_email_unknown_email_returns_none(tmp_path):
    coordinator, _database, _client, adapter = _setup(tmp_path)
    assert coordinator.move_email("alice", "missing", "2", adapter) is None


# -- Draft creation ---------------------------------------------------------------------


def test_create_draft_success(tmp_path):
    coordinator, database, client, adapter = _setup(tmp_path)
    _seed_mailbox(database, mailbox_id="3", folder_type=int(FolderType.DRAFTS), sync_key="7")
    client.draft_result = EmailAddResult(sync_key="8", client_id="cid1", status="1", server_id="9:new")

    message = EmailMessage()
    message["Subject"] = "Draft subject"
    message["From"] = "me@example.com"

    status, email_id = coordinator.create_draft(
        "alice", "3", message, read=True, flagged=False, client_id="cid1", adapter=adapter
    )

    assert status == "1"
    assert email_id is not None
    assert client.draft_calls[0][:2] == ("3", "7")
    stored = cache.get_email(database.conn, "alice", email_id)
    assert stored.subject == "Draft subject"
    assert stored.mailbox_id == "3"
    assert stored.server_id == "9:new"
    assert stored.seen is True
    assert cache.get_mailbox(database.conn, "alice", "3").sync_key == "8"
    assert state.get_changes(database.conn, "alice", "Email", "0").created == [email_id]


def test_create_draft_failure_commits_sync_key_but_creates_nothing(tmp_path):
    coordinator, database, client, adapter = _setup(tmp_path)
    _seed_mailbox(database, mailbox_id="3", folder_type=int(FolderType.DRAFTS), sync_key="7")
    client.draft_result = EmailAddResult(sync_key="8", client_id="cid1", status="6", server_id=None)

    message = EmailMessage()
    message["Subject"] = "Draft subject"

    status, email_id = coordinator.create_draft(
        "alice", "3", message, read=False, flagged=False, client_id="cid1", adapter=adapter
    )

    assert status == "6"
    assert email_id is None
    assert cache.get_mailbox(database.conn, "alice", "3").sync_key == "8"
    assert state.get_changes(database.conn, "alice", "Email", "0").created == []
