"""Account/folder synchronization coordinator (plan.md section 4).

The local cache is the source for JMAP reads. `SyncCoordinator` brings it up
to date with EAS before a request needs freshness: reconcile the folder
hierarchy, then pull each folder's `Sync` delta. A folder's `Sync` calls are
serialized per `(account_id, folder_id)` because they consume and replace
that folder's `SyncKey`; unrelated folders and accounts sync independently.
"""
from __future__ import annotations

import secrets
import sqlite3
import threading

from pyactivesync.models import SyncItem

from ..store import cache, state
from ..store.db import Database
from . import mapping
from .eas import EasAdapter

DEFAULT_MAX_PAGES_PER_CALL = 10
"""Bounds one `sync_folder()` call's worst-case latency (plan.md's "before a request" contract
must not mean "block until a multi-year mailbox is fully synced"). A folder with more pending
pages than this simply finishes over subsequent calls -- its SyncKey is persisted after every page."""


class SyncCoordinator:
    def __init__(self, database: Database, *, max_pages_per_call: int = DEFAULT_MAX_PAGES_PER_CALL) -> None:
        self._database = database
        self._max_pages_per_call = max_pages_per_call
        self._folder_locks: dict[tuple[str, str], threading.Lock] = {}
        self._folder_locks_guard = threading.Lock()

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
        """Reconcile folders, then sync every mailbox's items."""
        self.reconcile_folders(account_id, adapter)
        with self._database.transaction() as conn:
            folder_ids = [mailbox.mailbox_id for mailbox in cache.list_mailboxes(conn, account_id)]
        for folder_id in folder_ids:
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
