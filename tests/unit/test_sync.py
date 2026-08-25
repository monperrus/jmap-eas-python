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


def test_reconcile_folders_ignores_non_mail_folder_types(tmp_path):
    coordinator, database = _coordinator(tmp_path)
    adapter = EasAdapter(FakeEasClient(
        folders=[
            Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox"),
            Folder(id="2", parent_id="0", type=FolderType.CALENDAR, name="Calendar"),
            Folder(id="3", parent_id="0", type=FolderType.CONTACTS, name="Contacts"),
        ],
        sync_responses={},
    ))
    coordinator.reconcile_folders("alice", adapter)
    assert [m.mailbox_id for m in cache.list_mailboxes(database.conn, "alice")] == ["1"]


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


def test_sync_folder_caps_pages_per_call_and_resumes_on_next_call(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    coordinator = SyncCoordinator(database, max_pages_per_call=2)
    folders = [Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")]

    def page(n: int, more: bool):
        return SyncResult(
            sync_key=str(n), added=[SyncItem(server_id=f"9:{n}", fields={"Email.Subject": f"S{n}"})],
            changed=[], deleted=[], more_available=more,
        )

    adapter = EasAdapter(FakeEasClient(
        folders=folders,
        sync_responses={
            "1": [
                SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),  # bootstrap
                page(2, more=True),
                page(3, more=True),
                page(4, more=True),
            ]
        },
    ))
    coordinator.reconcile_folders("alice", adapter)
    coordinator.sync_folder("alice", "1", adapter)  # bootstrap + 2 pages (the cap), stops with more pending

    assert len(cache.list_emails_in_mailbox(database.conn, "alice", "1")) == 2
    assert cache.get_mailbox(database.conn, "alice", "1").sync_key == "3"

    adapter2 = EasAdapter(FakeEasClient(folders=folders, sync_responses={"1": [page(5, more=False)]}))
    coordinator.sync_folder("alice", "1", adapter2)  # resumes from where it left off
    assert len(cache.list_emails_in_mailbox(database.conn, "alice", "1")) == 3
    assert cache.get_mailbox(database.conn, "alice", "1").sync_key == "5"


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


def test_sync_account_prunes_change_log(tmp_path, monkeypatch):
    coordinator, database = _coordinator(tmp_path)
    adapter = EasAdapter(FakeEasClient(folders=[], sync_responses={}))
    calls = []
    monkeypatch.setattr(state, "prune_change_log", lambda conn, account_id: calls.append(account_id))
    coordinator.sync_account("alice", adapter)
    assert calls == ["alice"]


def test_ensure_folders_reconciled_skips_within_freshness_window(tmp_path):
    coordinator, database = _coordinator(tmp_path)
    folders = [Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")]
    adapter = EasAdapter(FakeEasClient(folders=folders, sync_responses={}))

    coordinator.ensure_folders_reconciled("alice", adapter, max_age=60)
    assert {m.mailbox_id for m in cache.list_mailboxes(database.conn, "alice")} == {"1"}

    # A folder deleted upstream after the first call doesn't disappear from the cache yet --
    # the second call is skipped entirely because it's still within the freshness window.
    adapter._client._folders = []  # type: ignore[attr-defined]
    coordinator.ensure_folders_reconciled("alice", adapter, max_age=60)
    assert {m.mailbox_id for m in cache.list_mailboxes(database.conn, "alice")} == {"1"}


def test_ensure_folders_reconciled_runs_again_after_freshness_window(tmp_path):
    coordinator, database = _coordinator(tmp_path)
    folders = [Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")]
    adapter = EasAdapter(FakeEasClient(folders=folders, sync_responses={}))

    coordinator.ensure_folders_reconciled("alice", adapter, max_age=0)
    adapter._client._folders = []  # type: ignore[attr-defined]
    coordinator.ensure_folders_reconciled("alice", adapter, max_age=0)
    assert cache.list_mailboxes(database.conn, "alice") == []


def test_ensure_folder_synced_skips_within_freshness_window(tmp_path):
    coordinator, database = _coordinator(tmp_path)
    adapter = EasAdapter(FakeEasClient(
        folders=[Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")],
        sync_responses={"1": [
            SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False),
            SyncResult(sync_key="2", added=[], changed=[], deleted=[], more_available=False),
        ]},
    ))
    coordinator.reconcile_folders("alice", adapter)
    coordinator.ensure_folder_synced("alice", "1", adapter, max_age=60)
    assert cache.get_mailbox(database.conn, "alice", "1").sync_key == "2"

    # No more scripted responses left; a second call within the window must not hit EAS again.
    coordinator.ensure_folder_synced("alice", "1", adapter, max_age=60)
    assert cache.get_mailbox(database.conn, "alice", "1").sync_key == "2"
