from __future__ import annotations

from pyactivesync.models import Folder, FolderType, SyncItem, SyncResult

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.backend.sync import SyncCoordinator
from jmap_eas.store import cache, db, state


class FakeEasClient:
    """A minimal `EasClientProtocol` fake driven by scripted per-call responses."""

    def __init__(self, *, folders: list[Folder], sync_responses: dict[str, list[SyncResult]]) -> None:
        self._folders = folders
        # folder_id -> queue of SyncResult, consumed in order across sync_folder() calls.
        self._sync_responses = {k: list(v) for k, v in sync_responses.items()}

    def provision(self) -> str:
        return "policy-key"

    def list_folders(self) -> list[Folder]:
        return self._folders

    def sync_folder(self, folder_id, sync_key="0", *, window_size=100, filter_type=None):
        return self._sync_responses[folder_id].pop(0)

    def fetch_item(self, folder_id, item_id, *, body_type=None):
        raise NotImplementedError

    def fetch_attachment(self, file_reference):
        raise NotImplementedError

    def close(self) -> None:
        pass


def _coordinator(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    return SyncCoordinator(database), database


def test_reconcile_folders_creates_and_removes(tmp_path):
    coordinator, database = _coordinator(tmp_path)
    adapter = EasAdapter(FakeEasClient(
        folders=[Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")],
        sync_responses={},
    ))

    coordinator.reconcile_folders("alice", adapter)
    mailboxes = cache.list_mailboxes(database.conn, "alice")
    assert [m.mailbox_id for m in mailboxes] == ["1"]
    assert state.current_state(database.conn, "alice", "Mailbox") == "1"

    # Reconciling again with the folder renamed must update, not recreate.
    adapter2 = EasAdapter(FakeEasClient(
        folders=[Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Renamed")],
        sync_responses={},
    ))
    coordinator.reconcile_folders("alice", adapter2)
    assert cache.get_mailbox(database.conn, "alice", "1").name == "Renamed"
    assert state.current_state(database.conn, "alice", "Mailbox") == "2"

    # Reconciling with the folder gone must remove it and append a destroyed entry.
    adapter3 = EasAdapter(FakeEasClient(folders=[], sync_responses={}))
    coordinator.reconcile_folders("alice", adapter3)
    assert cache.list_mailboxes(database.conn, "alice") == []
    assert state.current_state(database.conn, "alice", "Mailbox") == "3"


def test_sync_folder_bootstraps_then_fetches_items(tmp_path):
    coordinator, database = _coordinator(tmp_path)
    adapter = EasAdapter(FakeEasClient(
        folders=[Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")],
        sync_responses={
            "1": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(
                    sync_key="2",
                    added=[SyncItem(server_id="9:1", fields={"Email.Subject": "Hello"})],
                    changed=[],
                    deleted=[],
                    more_available=False,
                ),
            ]
        },
    ))
    coordinator.reconcile_folders("alice", adapter)
    coordinator.sync_folder("alice", "1", adapter)

    emails = cache.list_emails_in_mailbox(database.conn, "alice", "1")
    assert len(emails) == 1
    assert emails[0].subject == "Hello"
    assert cache.get_mailbox(database.conn, "alice", "1").sync_key == "2"
    assert state.get_changes(database.conn, "alice", "Email", "0").created == [emails[0].email_id]


def test_sync_folder_pages_until_more_available_is_false(tmp_path):
    coordinator, database = _coordinator(tmp_path)
    adapter = EasAdapter(FakeEasClient(
        folders=[Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")],
        sync_responses={
            "1": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(
                    sync_key="2",
                    added=[SyncItem(server_id="9:1", fields={"Email.Subject": "First"})],
                    changed=[],
                    deleted=[],
                    more_available=True,
                ),
                SyncResult(
                    sync_key="3",
                    added=[SyncItem(server_id="9:2", fields={"Email.Subject": "Second"})],
                    changed=[],
                    deleted=[],
                    more_available=False,
                ),
            ]
        },
    ))
    coordinator.reconcile_folders("alice", adapter)
    coordinator.sync_folder("alice", "1", adapter)

    emails = cache.list_emails_in_mailbox(database.conn, "alice", "1")
    assert {e.subject for e in emails} == {"First", "Second"}
    assert cache.get_mailbox(database.conn, "alice", "1").sync_key == "3"


def test_sync_folder_applies_deletes(tmp_path):
    coordinator, database = _coordinator(tmp_path)
    folders = [Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")]
    adapter = EasAdapter(FakeEasClient(
        folders=folders,
        sync_responses={
            "1": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(
                    sync_key="2",
                    added=[SyncItem(server_id="9:1", fields={"Email.Subject": "Hello"})],
                    changed=[],
                    deleted=[],
                    more_available=False,
                ),
            ]
        },
    ))
    coordinator.reconcile_folders("alice", adapter)
    coordinator.sync_folder("alice", "1", adapter)
    email_id = cache.list_emails_in_mailbox(database.conn, "alice", "1")[0].email_id
    baseline = state.current_state(database.conn, "alice", "Email")

    adapter2 = EasAdapter(FakeEasClient(
        folders=folders,
        sync_responses={"1": [SyncResult(sync_key="3", added=[], changed=[], deleted=["9:1"], more_available=False)]},
    ))
    coordinator.sync_folder("alice", "1", adapter2)

    assert cache.list_emails_in_mailbox(database.conn, "alice", "1") == []
    assert state.get_changes(database.conn, "alice", "Email", baseline).destroyed == [email_id]


def test_sync_folder_changed_item_updates_existing_email_and_preserves_id(tmp_path):
    coordinator, database = _coordinator(tmp_path)
    folders = [Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")]
    adapter = EasAdapter(FakeEasClient(
        folders=folders,
        sync_responses={
            "1": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(
                    sync_key="2",
                    added=[SyncItem(server_id="9:1", fields={"Email.Subject": "Hello", "Email.Read": "0"})],
                    changed=[],
                    deleted=[],
                    more_available=False,
                ),
            ]
        },
    ))
    coordinator.reconcile_folders("alice", adapter)
    coordinator.sync_folder("alice", "1", adapter)
    original_id = cache.list_emails_in_mailbox(database.conn, "alice", "1")[0].email_id
    baseline = state.current_state(database.conn, "alice", "Email")

    adapter2 = EasAdapter(FakeEasClient(
        folders=folders,
        sync_responses={
            "1": [
                SyncResult(
                    sync_key="3",
                    added=[],
                    changed=[SyncItem(server_id="9:1", fields={"Email.Subject": "Hello", "Email.Read": "1"})],
                    deleted=[],
                    more_available=False,
                )
            ]
        },
    ))
    coordinator.sync_folder("alice", "1", adapter2)

    email = cache.get_email(database.conn, "alice", original_id)
    assert email.seen is True
    changes = state.get_changes(database.conn, "alice", "Email", baseline)
    assert changes.updated == [original_id]


def test_sync_account_reconciles_and_syncs_every_folder(tmp_path):
    coordinator, database = _coordinator(tmp_path)
    adapter = EasAdapter(FakeEasClient(
        folders=[
            Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox"),
            Folder(id="2", parent_id="0", type=FolderType.DRAFTS, name="Drafts"),
        ],
        sync_responses={
            "1": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(sync_key="2", added=[], changed=[], deleted=[], more_available=False),
            ],
            "2": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
                SyncResult(sync_key="2", added=[], changed=[], deleted=[], more_available=False),
            ],
        },
    ))
    coordinator.sync_account("alice", adapter)
    assert {m.mailbox_id for m in cache.list_mailboxes(database.conn, "alice")} == {"1", "2"}
