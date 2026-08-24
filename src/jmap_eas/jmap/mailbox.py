"""`Mailbox/get`, `Mailbox/query`, `Mailbox/changes`, `Mailbox/set` (RFC 8620/8621).

`Mailbox/set` create/update only support `name` and `parentId`; `role` is
always server-derived from the EAS folder type (plan.md section 1) and can
never be set. Destroy is gated by `policy.allow_delete` (plan.md section 6)
and cascades to every email the folder contained.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from pyactivesync.models import FolderType

from ..backend.mapping import MAILBOX_ROLES
from ..errors import BackendError, CannotCalculateChangesError, ForbiddenError, InvalidArgumentsError
from ..models import MailboxRecord
from ..store import cache, state
from .dispatcher import Environment
from .filtering import evaluate_filter

DEFAULT_PROPERTIES = [
    "id", "name", "parentId", "role", "sortOrder", "totalEmails", "unreadEmails",
    "totalThreads", "unreadThreads", "isSubscribed", "myRights",
]

_MUTABLE_PROPERTIES = {"name", "parentId"}


def _my_rights(env: Environment, role: str | None) -> dict[str, bool]:
    return {
        "mayReadItems": True,
        "mayAddItems": role == "drafts",  # EAS Sync Add only works for Drafts (plan.md section 6)
        "mayRemoveItems": env.policy.allow_delete,
        "maySetSeen": True,
        "maySetKeywords": True,
        "mayCreateChild": True,
        "mayRename": True,
        "mayDelete": env.policy.allow_delete,
        "maySubmit": False,
    }


def _to_jmap(
    env: Environment, conn: sqlite3.Connection, record: MailboxRecord, properties: list[str]
) -> dict[str, Any]:
    total, unread = cache.count_emails_in_mailbox(conn, record.account_id, record.mailbox_id)
    total_threads, unread_threads = cache.count_threads_in_mailbox(conn, record.account_id, record.mailbox_id)
    role = MAILBOX_ROLES.get(FolderType(record.folder_type))
    full = {
        "id": record.mailbox_id,
        "name": record.name,
        "parentId": record.parent_id if record.parent_id != "0" else None,
        "role": role,
        "sortOrder": 0,
        "totalEmails": total,
        "unreadEmails": unread,
        "totalThreads": total_threads,
        "unreadThreads": unread_threads,
        "isSubscribed": True,
        "myRights": _my_rights(env, role),
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
        jmap_list = [_to_jmap(env, conn, record, properties) for record in records]
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


def _validate_create(props: dict[str, Any]) -> dict[str, Any] | None:
    name = props.get("name")
    if not isinstance(name, str) or not name.strip():
        return {"type": "invalidProperties", "properties": ["name"]}
    if props.get("role") is not None:
        return {"type": "invalidProperties", "properties": ["role"]}
    parent_id = props.get("parentId")
    if parent_id is not None and not isinstance(parent_id, str):
        return {"type": "invalidProperties", "properties": ["parentId"]}
    return None


def _validate_update(patch: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    unknown = sorted({key.split("/")[0] for key in patch} - _MUTABLE_PROPERTIES)
    if unknown:
        return {"type": "invalidProperties", "properties": unknown}, None, None
    name = patch.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        return {"type": "invalidProperties", "properties": ["name"]}, None, None
    parent_id = patch.get("parentId")
    if parent_id is not None and not isinstance(parent_id, str):
        return {"type": "invalidProperties", "properties": ["parentId"]}, None, None
    return None, name, parent_id


def set_(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    """`Mailbox/set`. Named `set_` to avoid shadowing the builtin `set`."""
    create = arguments.get("create") or {}
    update = arguments.get("update") or {}
    destroy = arguments.get("destroy") or []
    if not isinstance(create, dict) or not isinstance(update, dict) or not isinstance(destroy, list):
        raise InvalidArgumentsError("create/update must be objects and destroy must be an array")

    with env.database.transaction() as conn:
        old_state = state.current_state(conn, env.account_id, "Mailbox")

    created: dict[str, Any] = {}
    not_created: dict[str, Any] = {}
    for client_id, props in create.items():
        error = _validate_create(props) if isinstance(props, dict) else {"type": "invalidProperties"}
        if error is not None:
            not_created[client_id] = error
            continue
        try:
            record = env.sync.create_mailbox(env.account_id, props["name"], props.get("parentId") or "0",
                                              env.adapter)
        except BackendError:
            not_created[client_id] = {"type": "serverFail"}
            continue
        created[client_id] = {"id": record.mailbox_id}

    updated: dict[str, None] = {}
    not_updated: dict[str, Any] = {}
    for mailbox_id, patch in update.items():
        with env.database.transaction() as conn:
            existing = cache.get_mailbox(conn, env.account_id, mailbox_id)
        if existing is None:
            not_updated[mailbox_id] = {"type": "notFound"}
            continue
        error, name, parent_id = (
            _validate_update(patch) if isinstance(patch, dict) else ({"type": "invalidProperties"}, None, None)
        )
        if error is not None:
            not_updated[mailbox_id] = error
            continue
        if name is None and parent_id is None:
            updated[mailbox_id] = None
            continue
        try:
            env.sync.update_mailbox(env.account_id, mailbox_id, name or existing.name,
                                     parent_id or existing.parent_id, env.adapter)
        except BackendError:
            not_updated[mailbox_id] = {"type": "serverFail"}
            continue
        updated[mailbox_id] = None

    destroyed: list[str] = []
    not_destroyed: dict[str, Any] = {}
    for mailbox_id in destroy:
        if not env.policy.allow_delete:
            not_destroyed[mailbox_id] = {"type": ForbiddenError.type}
            continue
        with env.database.transaction() as conn:
            existing = cache.get_mailbox(conn, env.account_id, mailbox_id)
        if existing is None:
            not_destroyed[mailbox_id] = {"type": "notFound"}
            continue
        try:
            env.sync.delete_mailbox(env.account_id, mailbox_id, env.adapter)
        except BackendError:
            not_destroyed[mailbox_id] = {"type": "serverFail"}
            continue
        destroyed.append(mailbox_id)

    with env.database.transaction() as conn:
        new_state = state.current_state(conn, env.account_id, "Mailbox")

    return {
        "accountId": env.account_id,
        "oldState": old_state,
        "newState": new_state,
        "created": created,
        "updated": updated,
        "destroyed": destroyed,
        "notCreated": not_created,
        "notUpdated": not_updated,
        "notDestroyed": not_destroyed,
    }


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
