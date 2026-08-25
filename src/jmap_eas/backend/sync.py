"""Account/folder synchronization coordinator (plan.md section 4).

The local cache is the source for JMAP reads. `SyncCoordinator` brings it up
to date with EAS before a request needs freshness: reconcile the folder
hierarchy, then pull each folder's `Sync` delta. A folder's `Sync` calls are
serialized per `(account_id, folder_id)` because they consume and replace
that folder's `SyncKey`; unrelated folders and accounts sync independently.

`reconcile_folders()`/`sync_folder()` always hit EAS; `ensure_folders_reconciled()`/
`ensure_folder_synced()` wrap them with a short freshness window so a foreground
`POST /api` request doesn't repeat the same round trip a moment-ago request already
made (`jmap.sync_scope` decides, per request batch, which of these -- if any -- a
given batch of method calls actually needs).
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from email.message import Message

from pyactivesync.models import EmailChange, FolderType, SyncItem

from ..models import EmailRecord, MailboxRecord
from ..store import cache, state
from ..store.db import Database
from . import mapping
from .eas import EasAdapter

DEFAULT_MAX_PAGES_PER_CALL = 10
"""Bounds one `sync_folder()` call's worst-case latency (plan.md's "before a request" contract
must not mean "block until a multi-year mailbox is fully synced"). A folder with more pending
pages than this simply finishes over subsequent calls -- its SyncKey is persisted after every page."""

DEFAULT_FRESHNESS_SECONDS = 5.0
"""How long `ensure_folders_reconciled()`/`ensure_folder_synced()` trust a sync they just did
(plan.md's per-request scoping): a foreground request repeating within this window serves the
cache as-is instead of repeating the same EAS round trip. Push/Ping-driven syncing (the
`/eventsource` stream) and every direct mutation are unaffected -- they call `reconcile_folders()`
/`sync_folder()` directly, which always hit EAS."""


class SyncCoordinator:
    def __init__(self, database: Database, *, max_pages_per_call: int = DEFAULT_MAX_PAGES_PER_CALL) -> None:
        self._database = database
        self._max_pages_per_call = max_pages_per_call
        self._folder_locks: dict[tuple[str, str], threading.Lock] = {}
        self._folder_locks_guard = threading.Lock()
        self._reconciled_at: dict[str, float] = {}
        self._folder_synced_at: dict[tuple[str, str], float] = {}
        self._freshness_guard = threading.Lock()

    def _folder_lock(self, account_id: str, folder_id: str) -> threading.Lock:
        key = (account_id, folder_id)
        with self._folder_locks_guard:
            lock = self._folder_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._folder_locks[key] = lock
            return lock

    def reconcile_folders(self, account_id: str, adapter: EasAdapter) -> None:
        """Full `FolderSync` listing, diffed against the cache and applied in one transaction.

        Only mail-class folders are cached or synced (`mapping.is_mail_folder`);
        this is a Mail-only bridge, and `pyactivesync`'s `Sync` targets the
        Email item class.
        """
        remote_folders = [f for f in adapter.list_folders() if mapping.is_mail_folder(f.type)]
        remote_ids = {folder.id for folder in remote_folders}
        with self._database.transaction() as conn:
            local_ids = {mailbox.mailbox_id for mailbox in cache.list_mailboxes(conn, account_id)}
            for folder in remote_folders:
                existed = folder.id in local_ids
                cache.upsert_mailbox(conn, mapping.map_folder_to_mailbox(account_id, folder))
                state.append_change(conn, account_id, "Mailbox", folder.id, "updated" if existed else "created")
            for removed_id in local_ids - remote_ids:
                cache.delete_mailbox(conn, account_id, removed_id)
                state.append_change(conn, account_id, "Mailbox", removed_id, "destroyed")

    def sync_folder(self, account_id: str, folder_id: str, adapter: EasAdapter) -> None:
        """Bring one folder's cached items closer to up to date, paging until `more_available`
        is false or `max_pages_per_call` pages have been applied, whichever comes first.

        A stored `SyncKey` of `"0"` means this folder has never been synced:
        the first call only bootstraps a key (plan.md section 4), so at least
        one further call is always made to fetch its actual items. A folder
        with more pending pages than the cap simply finishes over later calls.
        """
        with self._folder_lock(account_id, folder_id):
            with self._database.transaction() as conn:
                mailbox = cache.get_mailbox(conn, account_id, folder_id)
            sync_key = mailbox.sync_key if mailbox is not None else "0"
            pages_applied = 0
            while True:
                was_bootstrap = sync_key == "0"
                sync_key, more_available = self._sync_folder_once(account_id, folder_id, adapter, sync_key)
                if was_bootstrap:
                    continue
                pages_applied += 1
                if not more_available or pages_applied >= self._max_pages_per_call:
                    break

    def sync_account(self, account_id: str, adapter: EasAdapter) -> None:
        """Reconcile folders, sync every mailbox's items, then prune old change-log history."""
        self.reconcile_folders(account_id, adapter)
        with self._database.transaction() as conn:
            folder_ids = [mailbox.mailbox_id for mailbox in cache.list_mailboxes(conn, account_id)]
        for folder_id in folder_ids:
            self.sync_folder(account_id, folder_id, adapter)
        with self._database.transaction() as conn:
            state.prune_change_log(conn, account_id)

    # -- Request-scoped freshness (plan.md's per-request sync scoping) ---------------

    def ensure_folders_reconciled(
        self, account_id: str, adapter: EasAdapter, *, max_age: float = DEFAULT_FRESHNESS_SECONDS
    ) -> None:
        """`reconcile_folders()`, skipped if this account was already reconciled within `max_age`."""
        with self._freshness_guard:
            seen_at = self._reconciled_at.get(account_id)
            if seen_at is not None and time.monotonic() - seen_at < max_age:
                return
            self._reconciled_at[account_id] = time.monotonic()
        self.reconcile_folders(account_id, adapter)

    def ensure_folder_synced(
        self, account_id: str, folder_id: str, adapter: EasAdapter, *, max_age: float = DEFAULT_FRESHNESS_SECONDS
    ) -> None:
        """`sync_folder()`, skipped if this folder was already synced within `max_age`."""
        key = (account_id, folder_id)
        with self._freshness_guard:
            seen_at = self._folder_synced_at.get(key)
            if seen_at is not None and time.monotonic() - seen_at < max_age:
                return
            self._folder_synced_at[key] = time.monotonic()
        self.sync_folder(account_id, folder_id, adapter)

    def _sync_folder_once(
        self, account_id: str, folder_id: str, adapter: EasAdapter, sync_key: str
    ) -> tuple[str, bool]:
        result = adapter.sync_folder(folder_id, sync_key)
        with self._database.transaction() as conn:
            for item in result.added + result.changed:
                self._apply_item(conn, account_id, folder_id, item)
            for server_id in result.deleted:
                email_id = cache.find_email_id_by_server_id(conn, account_id, folder_id, server_id)
                if email_id is not None:
                    cache.delete_email(conn, account_id, email_id)
                    state.append_change(conn, account_id, "Email", email_id, "destroyed")
            cache.set_mailbox_sync_key(conn, account_id, folder_id, result.sync_key)
        return result.sync_key, result.more_available

    def _apply_item(self, conn: sqlite3.Connection, account_id: str, folder_id: str, item: SyncItem) -> None:
        email_id = cache.find_email_id_by_server_id(conn, account_id, folder_id, item.server_id)
        is_create = email_id is None
        if email_id is None:
            email_id = secrets.token_urlsafe(16)
        thread_key = mapping.thread_key_for_item(folder_id, item.server_id, item.fields.get("Email.Subject"))
        thread_id = cache.get_or_create_thread_id(conn, account_id, thread_key, secrets.token_urlsafe(16))
        record = mapping.map_sync_item_to_email(
            account_id=account_id, mailbox_id=folder_id, thread_id=thread_id, email_id=email_id, item=item
        )
        cache.upsert_email(conn, record)
        state.append_change(conn, account_id, "Email", email_id, "created" if is_create else "updated")

    # -- Mailbox mutations (M2) ------------------------------------------------------

    def create_mailbox(self, account_id: str, name: str, parent_id: str, adapter: EasAdapter) -> MailboxRecord:
        folder = adapter.create_folder(name, parent_id, type=FolderType.USER_MAIL)
        record = mapping.map_folder_to_mailbox(account_id, folder)
        with self._database.transaction() as conn:
            cache.upsert_mailbox(conn, record)
            state.append_change(conn, account_id, "Mailbox", record.mailbox_id, "created")
        return record

    def update_mailbox(self, account_id: str, mailbox_id: str, name: str, parent_id: str, adapter: EasAdapter) -> None:
        adapter.update_folder(mailbox_id, name, parent_id)
        with self._database.transaction() as conn:
            mailbox = cache.get_mailbox(conn, account_id, mailbox_id)
            assert mailbox is not None
            cache.upsert_mailbox(conn, MailboxRecord(account_id, mailbox_id, parent_id, name, mailbox.folder_type,
                                                       mailbox.sync_key))
            state.append_change(conn, account_id, "Mailbox", mailbox_id, "updated")

    def delete_mailbox(self, account_id: str, mailbox_id: str, adapter: EasAdapter) -> None:
        """Deletes the EAS folder, then cascades the deletion to its cached emails (plan.md's Mailbox destroy)."""
        adapter.delete_folder(mailbox_id)
        with self._folder_lock(account_id, mailbox_id):
            with self._database.transaction() as conn:
                removed_email_ids = cache.delete_emails_in_mailbox(conn, account_id, mailbox_id)
                for email_id in removed_email_ids:
                    state.append_change(conn, account_id, "Email", email_id, "destroyed")
                cache.delete_mailbox(conn, account_id, mailbox_id)
                state.append_change(conn, account_id, "Mailbox", mailbox_id, "destroyed")

    # -- Email mutations (M2) --------------------------------------------------------

    def _apply_email_change(
        self, account_id: str, email_id: str, adapter: EasAdapter,
        build_change: Callable[[str], EmailChange], *, deletes_as_moves: bool = True,
    ) -> tuple[str, EmailRecord] | None:
        """Runs one `EmailChange` against its email's own folder, holding that folder's lock.

        Commits the returned SyncKey even when the per-item status isn't `"1"`
        (plan.md's M2 note): a caller must still record the advanced key.
        Returns `None` if the email isn't cached, else `(status, record)`
        where `record` is the email's state *before* this change.
        """
        with self._database.transaction() as conn:
            record = cache.get_email(conn, account_id, email_id)
        if record is None:
            return None
        change = build_change(record.server_id)
        with self._folder_lock(account_id, record.mailbox_id):
            with self._database.transaction() as conn:
                mailbox = cache.get_mailbox(conn, account_id, record.mailbox_id)
            assert mailbox is not None
            result = adapter.apply_email_changes(
                record.mailbox_id, mailbox.sync_key, [change], deletes_as_moves=deletes_as_moves
            )
            status = result.statuses.get(record.server_id, "1")
            with self._database.transaction() as conn:
                cache.set_mailbox_sync_key(conn, account_id, record.mailbox_id, result.sync_key)
        return status, record

    def set_email_keywords(
        self, account_id: str, email_id: str, adapter: EasAdapter, *, seen: bool | None, flagged: bool | None
    ) -> str | None:
        """Applies a read/flag change. Returns the EAS per-item status, or `None` if not cached."""
        outcome = self._apply_email_change(
            account_id, email_id, adapter, lambda server_id: EmailChange(server_id, read=seen, flagged=flagged)
        )
        if outcome is None:
            return None
        status, record = outcome
        if status == "1":
            new_seen = record.seen if seen is None else seen
            new_flagged = record.flagged if flagged is None else flagged
            with self._database.transaction() as conn:
                cache.set_email_keywords(conn, account_id, email_id, seen=new_seen, flagged=new_flagged)
                state.append_change(conn, account_id, "Email", email_id, "updated")
        return status

    def delete_email(self, account_id: str, email_id: str, adapter: EasAdapter) -> str | None:
        """Deletes (moves to Deleted Items) one email. Returns the EAS per-item status, or `None` if not cached."""
        outcome = self._apply_email_change(
            account_id, email_id, adapter, lambda server_id: EmailChange(server_id, delete=True),
            deletes_as_moves=True,
        )
        if outcome is None:
            return None
        status, _record = outcome
        if status == "1":
            with self._database.transaction() as conn:
                cache.delete_email(conn, account_id, email_id)
                state.append_change(conn, account_id, "Email", email_id, "destroyed")
        return status

    def move_email(self, account_id: str, email_id: str, dst_mailbox_id: str, adapter: EasAdapter) -> str | None:
        """Moves one email to another mailbox, keeping its local `email_id` (plan.md section 3).

        Returns `"moved"`/`"unchanged"`, or `None` if the email isn't cached.
        Both folders are locked (sorted, to avoid deadlocking a concurrent move
        in the opposite direction) so a concurrent sync of either folder can't
        race the cache update, even though `MoveItems` itself has no SyncKey.
        """
        with self._database.transaction() as conn:
            record = cache.get_email(conn, account_id, email_id)
        if record is None:
            return None
        src_mailbox_id = record.mailbox_id
        if src_mailbox_id == dst_mailbox_id:
            return "unchanged"
        first, second = sorted((src_mailbox_id, dst_mailbox_id))
        with self._folder_lock(account_id, first), self._folder_lock(account_id, second):
            new_server_id = adapter.move_item(record.server_id, src_mailbox_id, dst_mailbox_id)
            with self._database.transaction() as conn:
                cache.move_email(conn, account_id, email_id, dst_mailbox_id, new_server_id)
                state.append_change(conn, account_id, "Email", email_id, "updated")
        return "moved"

    def create_draft(
        self, account_id: str, mailbox_id: str, message: Message, *, read: bool, flagged: bool, client_id: str,
        adapter: EasAdapter,
    ) -> tuple[str, str | None]:
        """Composes one draft via `Sync Add`, holding the Drafts folder's lock.

        Commits the returned SyncKey even on a non-`"1"` status (plan.md's M2
        note). Returns `(status, email_id)`; `email_id` is `None` on failure.
        """
        with self._folder_lock(account_id, mailbox_id):
            with self._database.transaction() as conn:
                mailbox = cache.get_mailbox(conn, account_id, mailbox_id)
            assert mailbox is not None
            result = adapter.create_email_draft(
                mailbox_id, mailbox.sync_key, message, read=read, flagged=flagged, client_id=client_id
            )
            with self._database.transaction() as conn:
                cache.set_mailbox_sync_key(conn, account_id, mailbox_id, result.sync_key)
                if result.status != "1" or result.server_id is None:
                    return result.status, None
                email_id = secrets.token_urlsafe(16)
                thread_key = mapping.thread_key_for_item(mailbox_id, result.server_id, message.get("Subject"))
                thread_id = cache.get_or_create_thread_id(conn, account_id, thread_key, secrets.token_urlsafe(16))
                record = mapping.map_draft_message_to_email(
                    account_id=account_id, mailbox_id=mailbox_id, server_id=result.server_id,
                    thread_id=thread_id, email_id=email_id, message=message, read=read, flagged=flagged,
                )
                cache.upsert_email(conn, record)
                state.append_change(conn, account_id, "Email", email_id, "created")
            return result.status, email_id
