"""`Mailbox/get`, `Mailbox/query`, `Mailbox/changes` (RFC 8620/8621).

Read-only for M1: `myRights` always reports every mutating right as `false`
until M2 adds `Mailbox/set` (plan.md milestones).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from pyactivesync.models import FolderType

from ..backend.mapping import MAILBOX_ROLES
from ..errors import CannotCalculateChangesError, InvalidArgumentsError
from ..models import MailboxRecord
from ..store import cache, state
from .dispatcher import Environment
from .filtering import evaluate_filter

DEFAULT_PROPERTIES = [
    "id", "name", "parentId", "role", "sortOrder", "totalEmails", "unreadEmails",
    "totalThreads", "unreadThreads", "isSubscribed", "myRights",
]

_MY_RIGHTS = {
    "mayReadItems": True,
    "mayAddItems": False,
    "mayRemoveItems": False,
    "maySetSeen": False,
    "maySetKeywords": False,
    "mayCreateChild": False,
    "mayRename": False,
    "mayDelete": False,
    "maySubmit": False,
}


def _to_jmap(conn: sqlite3.Connection, record: MailboxRecord, properties: list[str]) -> dict[str, Any]:
    total, unread = cache.count_emails_in_mailbox(conn, record.account_id, record.mailbox_id)
    total_threads, unread_threads = cache.count_threads_in_mailbox(conn, record.account_id, record.mailbox_id)
    full = {
        "id": record.mailbox_id,
        "name": record.name,
        "parentId": record.parent_id if record.parent_id != "0" else None,
        "role": MAILBOX_ROLES.get(FolderType(record.folder_type)),
        "sortOrder": 0,
        "totalEmails": total,
        "unreadEmails": unread,
        "totalThreads": total_threads,
        "unreadThreads": unread_threads,
        "isSubscribed": True,
        "myRights": dict(_MY_RIGHTS),
    }
    return {"id": record.mailbox_id, **{p: full[p] for p in properties if p != "id" and p in full}}


def get(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    ids = arguments.get("ids")
    properties = arguments.get("properties") or DEFAULT_PROPERTIES
    with env.database.transaction() as conn:
        if ids is None:
            records = cache.list_mailboxes(conn, env.account_id)
            not_found: list[str] = []
        else:
            records = []
            not_found = []
            for mailbox_id in ids:
                record = cache.get_mailbox(conn, env.account_id, mailbox_id)
                if record is None:
                    not_found.append(mailbox_id)
                else:
                    records.append(record)
        jmap_list = [_to_jmap(conn, record, properties) for record in records]
        current = state.current_state(conn, env.account_id, "Mailbox")
    return {"accountId": env.account_id, "state": current, "list": jmap_list, "notFound": not_found}


def _match_condition(condition: dict[str, Any], record: MailboxRecord) -> bool:
    if "parentId" in condition:
        expected = condition["parentId"]
        actual = record.parent_id if record.parent_id != "0" else None
        if actual != expected:
            return False
    if "name" in condition and condition["name"].lower() not in record.name.lower():
        return False
    if "role" in condition and MAILBOX_ROLES.get(FolderType(record.folder_type)) != condition["role"]:
        return False
    return True


def query(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    filter_ = arguments.get("filter")
    sort = arguments.get("sort") or [{"property": "name", "isAscending": True}]
    position = arguments.get("position", 0)
    limit = arguments.get("limit")
    calculate_total = arguments.get("calculateTotal", False)

    with env.database.transaction() as conn:
        records = [
            r for r in cache.list_mailboxes(conn, env.account_id) if evaluate_filter(filter_, r, _match_condition)
        ]
        current = state.current_state(conn, env.account_id, "Mailbox")

    for comparator in reversed(sort):
        prop = comparator.get("property")
        if prop not in {"name", "sortOrder"}:
            raise InvalidArgumentsError(f"unsupported Mailbox/query sort property: {prop!r}")
        ascending = comparator.get("isAscending", True)
        key = (lambda r: r.name) if prop == "name" else (lambda r: 0)
        records.sort(key=key, reverse=not ascending)

    total = len(records)
    if position < 0:
        position = max(total + position, 0)
    window = records[position: position + limit] if limit is not None else records[position:]
    result: dict[str, Any] = {
        "accountId": env.account_id,
        "queryState": current,
        "canCalculateChanges": False,
        "position": position,
        "ids": [r.mailbox_id for r in window],
    }
    if limit is not None:
        result["limit"] = limit
    if calculate_total:
        result["total"] = total
    return result


def changes(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    since_state = arguments.get("sinceState")
    max_changes = arguments.get("maxChanges")
    if not isinstance(since_state, str):
        raise InvalidArgumentsError("sinceState is required")
    with env.database.transaction() as conn:
        try:
            result = state.get_changes(conn, env.account_id, "Mailbox", since_state, max_changes)
        except state.CannotCalculateChangesError as exc:
            raise CannotCalculateChangesError(str(exc)) from exc
    return {
        "accountId": env.account_id,
        "oldState": result.old_state,
        "newState": result.new_state,
        "hasMoreChanges": result.has_more_changes,
        "created": result.created,
        "updated": result.updated,
        "destroyed": result.destroyed,
    }
