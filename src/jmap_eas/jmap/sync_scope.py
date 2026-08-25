"""Computes what EAS synchronization one JMAP request batch actually requires.

Replaces a blanket per-request `SyncCoordinator.sync_account()` call (which
reconciled the folder list and synced every cached mailbox's items before
dispatching *any* method, even `Identity/get`): most methods only need
particular pieces of the cache to be fresh, several need none at all, and an
`Email/query`/`Email/queryChanges` scoped by `inMailbox` only needs that one
mailbox synced. `compute_sync_scope` unions every call's requirement across
one batch, so a request with several method calls does at most one folder-list
reconciliation and one `Sync` per mailbox actually touched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .filtering import extract_inmailbox_scope

Invocation = tuple[str, dict[str, Any], str]

_FOLDER_LIST_METHODS = {"Mailbox/get", "Mailbox/query", "Mailbox/queryChanges", "Mailbox/changes"}
_MAILBOX_SCOPED_QUERY_METHODS = {"Email/query", "Email/queryChanges"}


@dataclass
class SyncScope:
    """`reconcile_folders`: whether the folder list needs a `FolderSync` reconciliation.
    `sync_all_folders`: whether every currently cached mailbox needs its items synced
    (an `Email/query`-family call whose filter doesn't bound matches to specific mailboxes).
    `folder_ids`: specific mailboxes that need their items synced, independent of
    `sync_all_folders` (a superset is harmless, so the two aren't mutually exclusive)."""

    reconcile_folders: bool = False
    sync_all_folders: bool = False
    folder_ids: set[str] = field(default_factory=set)

    def merge(self, other: SyncScope) -> None:
        self.reconcile_folders = self.reconcile_folders or other.reconcile_folders
        self.sync_all_folders = self.sync_all_folders or other.sync_all_folders
        self.folder_ids |= other.folder_ids


def _call_scope(name: str, arguments: dict[str, Any]) -> SyncScope:
    if name in _FOLDER_LIST_METHODS:
        return SyncScope(reconcile_folders=True)
    if name in _MAILBOX_SCOPED_QUERY_METHODS:
        filter_ = arguments.get("filter")
        if not (filter_ is None or isinstance(filter_, dict)):
            return SyncScope(reconcile_folders=True, sync_all_folders=True)
        mailbox_ids = extract_inmailbox_scope(filter_)
        if mailbox_ids:
            return SyncScope(folder_ids=set(mailbox_ids))
        return SyncScope(reconcile_folders=True, sync_all_folders=True)
    return SyncScope()


def compute_sync_scope(calls: list[Invocation]) -> SyncScope:
    """Unions every call's requirement (RFC 8620 batches share one EAS round trip per mailbox)."""
    scope = SyncScope()
    for name, arguments, _call_id in calls:
        scope.merge(_call_scope(name, arguments if isinstance(arguments, dict) else {}))
    return scope
