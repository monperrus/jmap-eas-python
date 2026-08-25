"""`EmailSubmission/get`, `/set`, `/changes` (RFC 8621 section 7).

`EmailSubmission/set` create sends immediately through EAS's `SendMail` --
there is no delayed-send/Outbox model to implement -- so a created
submission's `undoStatus` is always `"final"` and update/destroy have
nothing meaningful to change. The referenced `emailId`'s full MIME is
fetched live via `ItemOperations` and passed to `send_mail()` unchanged;
`envelope` overrides are not supported in v1.

Sending is not automatically safe to retry (plan.md section 6): the
submission record is persisted *before* `send_mail()` is called and its
outcome recorded immediately after, so a repeated request can be handled
deliberately rather than by guessing at an ambiguous transport failure.
"""
from __future__ import annotations

import secrets
from typing import Any

from pyactivesync.models import BodyType

from ..errors import BackendError, CannotCalculateChangesError, ForbiddenError, InvalidArgumentsError
from ..models import EmailRecord
from ..store import cache, state, submissions
from .dispatcher import Environment
from .email import parse_mime
from .identity import IDENTITY_ID

DEFAULT_PROPERTIES = [
    "id", "identityId", "emailId", "threadId", "envelope", "sendAt", "undoStatus", "deliveryStatus",
    "dsnBlobIds", "mdnBlobIds",
]

_UNDO_STATUS = {
    submissions.STATUS_PENDING: "pending",
    submissions.STATUS_FINAL: "final",
    submissions.STATUS_FAILED: "canceled",
}


def _to_jmap(record: submissions.SubmissionRecord, properties: list[str]) -> dict[str, Any]:
    full: dict[str, Any] = {
        "id": record.submission_id,
        "identityId": record.identity_id,
        "emailId": record.email_id,
        "threadId": None,
        "envelope": None,
        "sendAt": record.created_at,
        "undoStatus": _UNDO_STATUS[record.status],
        "deliveryStatus": None,
        "dsnBlobIds": [],
        "mdnBlobIds": [],
    }
    return {"id": record.submission_id, **{p: full[p] for p in properties if p != "id"}}


def get(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    ids = arguments.get("ids")
    properties = arguments.get("properties") or DEFAULT_PROPERTIES
    with env.database.transaction() as conn:
        if ids is None:
            records = submissions.list_submissions(conn, env.account_id)
            not_found: list[str] = []
        else:
            records = []
            not_found = []
            for submission_id in ids:
                record = submissions.get_submission(conn, env.account_id, submission_id)
                if record is None:
                    not_found.append(submission_id)
                else:
                    records.append(record)
        current = state.current_state(conn, env.account_id, "EmailSubmission")
    jmap_list = [_to_jmap(record, properties) for record in records]
    return {"accountId": env.account_id, "state": current, "list": jmap_list, "notFound": not_found}


def _validate_create(env: Environment, props: dict[str, Any]) -> tuple[dict[str, Any] | None, EmailRecord | None]:
    identity_id = props.get("identityId")
    if identity_id != IDENTITY_ID:
        return {"type": "invalidProperties", "properties": ["identityId"]}, None
    email_id = props.get("emailId")
    if not isinstance(email_id, str):
        return {"type": "invalidProperties", "properties": ["emailId"]}, None
    with env.database.transaction() as conn:
        record = cache.get_email(conn, env.account_id, email_id)
    if record is None:
        return {"type": "invalidProperties", "properties": ["emailId"]}, None
    return None, record


def _send(env: Environment, record: EmailRecord) -> tuple[dict[str, Any] | None, str | None]:
    """Fetches the referenced email's MIME and sends it. Returns `(error, submissionId)`."""
    try:
        item = env.adapter.fetch_item(record.mailbox_id, record.server_id, body_type=BodyType.MIME)
    except BackendError:
        return {"type": "serverFail"}, None
    if not item.bodies:
        return {"type": "serverFail"}, None
    data = item.bodies[0].data
    mime_bytes = data.encode("utf-8") if isinstance(data, str) else data
    message = parse_mime(mime_bytes)

    submission_id = secrets.token_urlsafe(16)
    eas_client_id = secrets.token_urlsafe(16)
    with env.database.transaction() as conn:
        submissions.insert_submission(conn, env.account_id, submission_id, record.email_id, IDENTITY_ID,
                                       eas_client_id)
    try:
        env.adapter.send_mail(message, client_id=eas_client_id)
    except BackendError:
        with env.database.transaction() as conn:
            submissions.set_status(conn, env.account_id, submission_id, submissions.STATUS_FAILED)
        return {"type": "serverFail"}, None
    with env.database.transaction() as conn:
        submissions.set_status(conn, env.account_id, submission_id, submissions.STATUS_FINAL)
        state.append_change(conn, env.account_id, "EmailSubmission", submission_id, "created")
    return None, submission_id


def set_(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    """`EmailSubmission/set`. Named `set_` to avoid shadowing the builtin `set`."""
    create = arguments.get("create") or {}
    update = arguments.get("update") or {}
    destroy = arguments.get("destroy") or []
    if not isinstance(create, dict) or not isinstance(update, dict) or not isinstance(destroy, list):
        raise InvalidArgumentsError("create/update must be objects and destroy must be an array")

    with env.database.transaction() as conn:
        old_state = state.current_state(conn, env.account_id, "EmailSubmission")

    created: dict[str, Any] = {}
    not_created: dict[str, Any] = {}
    for client_key, props in create.items():
        if not isinstance(props, dict):
            not_created[client_key] = {"type": "invalidProperties"}
            continue
        error, record = _validate_create(env, props)
        if error is not None:
            not_created[client_key] = error
            continue
        assert record is not None
        if not env.policy.allow_send:
            not_created[client_key] = {"type": ForbiddenError.type}
            continue
        send_error, submission_id = _send(env, record)
        if send_error is not None:
            not_created[client_key] = send_error
            continue
        created[client_key] = {"id": submission_id, "undoStatus": "final"}

    # Nothing is mutable once a submission reaches its terminal state (plan.md's M3 note:
    # sending is immediate, there is no pending/Outbox window to update or cancel).
    updated: dict[str, None] = {}
    not_updated: dict[str, Any] = {}
    for submission_id in update:
        with env.database.transaction() as conn:
            existing = submissions.get_submission(conn, env.account_id, submission_id)
        not_updated[submission_id] = {"type": "notFound"} if existing is None else {"type": "invalidProperties"}

    destroyed: list[str] = []
    not_destroyed: dict[str, Any] = {}
    for submission_id in destroy:
        with env.database.transaction() as conn:
            existing = submissions.get_submission(conn, env.account_id, submission_id)
        if existing is None:
            not_destroyed[submission_id] = {"type": "notFound"}
            continue
        with env.database.transaction() as conn:
            submissions.delete_submission(conn, env.account_id, submission_id)
            state.append_change(conn, env.account_id, "EmailSubmission", submission_id, "destroyed")
        destroyed.append(submission_id)

    with env.database.transaction() as conn:
        new_state = state.current_state(conn, env.account_id, "EmailSubmission")

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
            result = state.get_changes(conn, env.account_id, "EmailSubmission", since_state, max_changes)
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
