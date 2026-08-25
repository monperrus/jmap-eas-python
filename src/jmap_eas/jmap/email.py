"""`Email/get`, `Email/query`, `Email/changes` (RFC 8621 section 4).

`pyactivesync`'s `Sync` only carries a handful of `Email` codepage fields
(plan.md's mapping notes), so this bridge splits Email properties in two:

- **Cheap** properties come straight from the synced cache -- no network call.
- **Live** properties (`size`, `preview`, `hasAttachment`, `bodyValues`,
  `textBody`, `htmlBody`, `attachments`) need the full MIME body, fetched
  on demand via `ItemOperations` when a caller actually asks for them.

`bcc`, `sender`, `sentAt`, `inReplyTo`, `references`, `messageId`, and
`bodyStructure` are not derivable from either source in M1 and are always
returned as `null` (a spec-valid degradation for nullable properties) rather
than silently omitted.
"""
from __future__ import annotations

import email
import re
import secrets
from email import policy as email_policy
from email.message import EmailMessage, Message
from email.utils import formataddr
from typing import Any

from pyactivesync.models import AttachmentInfo, BodyType, FolderType

from ..backend.eas import EasAdapter
from ..backend.mapping import MAILBOX_ROLES
from ..errors import BackendError, CannotCalculateChangesError, ForbiddenError, InvalidArgumentsError
from ..models import EmailAddress, EmailRecord
from ..store import cache, state
from . import blob
from .dispatcher import Environment
from .filtering import compute_query_changes, evaluate_filter

CHEAP_PROPERTIES = [
    "id", "blobId", "mailboxIds", "threadId", "subject", "from", "to", "cc", "replyTo", "receivedAt", "keywords",
]
LIVE_PROPERTIES = {"size", "preview", "hasAttachment", "bodyValues", "textBody", "htmlBody", "attachments"}
SUMMARY_LIVE_PROPERTIES = {"size", "preview", "hasAttachment"}
"""The subset of `LIVE_PROPERTIES` `get()` can serve from `EmailRecord.cached_*` alone (issue
#2) once populated -- the rest (`attachments`, `bodyValues`, `textBody`, `htmlBody`) always need
the full MIME body, so any request for one of those still fetches live."""
ALWAYS_NULL_PROPERTIES = {"bcc", "sender", "sentAt", "inReplyTo", "references", "messageId", "bodyStructure"}
ALL_PROPERTIES = set(CHEAP_PROPERTIES) | LIVE_PROPERTIES | ALWAYS_NULL_PROPERTIES

_PREVIEW_WHITESPACE = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]+>")


def _addresses_to_jmap(addresses: list[EmailAddress]) -> list[dict[str, str | None]] | None:
    return [a.to_jmap() for a in addresses] or None


def _keywords(record: EmailRecord) -> dict[str, bool]:
    keywords = {}
    if record.seen:
        keywords["$seen"] = True
    if record.flagged:
        keywords["$flagged"] = True
    return keywords


def _make_preview(text: str | None, html: str | None) -> str:
    source = text if text is not None else _HTML_TAG.sub(" ", html or "")
    collapsed = _PREVIEW_WHITESPACE.sub(" ", source).strip()
    return collapsed[:256]


def parse_mime(data: bytes) -> EmailMessage:
    message = email.message_from_bytes(data, policy=email_policy.default)
    assert isinstance(message, EmailMessage)
    return message


def _extract_bodies(message: EmailMessage) -> tuple[str | None, str | None]:
    text_part = message.get_body(preferencelist=("plain",))
    html_part = message.get_body(preferencelist=("html",))
    text = text_part.get_content() if text_part is not None else None
    html = html_part.get_content() if html_part is not None else None
    return text, html


def _attachment_to_jmap(attachment: AttachmentInfo) -> dict[str, Any]:
    return {
        "blobId": blob.encode_attachment_blob_id(attachment.file_reference),
        "type": attachment.content_type or "application/octet-stream",
        "name": attachment.display_name,
        "size": attachment.estimated_data_size,
        "cid": attachment.content_id,
        "disposition": "inline" if attachment.is_inline else "attachment",
    }


def _fetch_live_data(record: EmailRecord, adapter: EasAdapter) -> dict[str, Any] | None:
    try:
        item = adapter.fetch_item(record.mailbox_id, record.server_id, body_type=BodyType.MIME)
    except BackendError:
        return None
    if not item.bodies:
        return None
    data = item.bodies[0].data
    mime_bytes = data.encode("utf-8") if isinstance(data, str) else data
    message = parse_mime(mime_bytes)
    text_body, html_body = _extract_bodies(message)
    return {
        "size": len(mime_bytes),
        "preview": _make_preview(text_body, html_body),
        "hasAttachment": bool(item.attachments),
        "attachments": [_attachment_to_jmap(a) for a in item.attachments],
        "textBody": _body_part(record, "text", text_body),
        "htmlBody": _body_part(record, "html", html_body),
        "bodyValues": {"text": text_body, "html": html_body},
    }


def _cached_summary(record: EmailRecord) -> dict[str, Any]:
    """A `_fetch_live_data()`-shaped dict built from `EmailRecord.cached_*` alone (issue #2),
    for a request that only needs `SUMMARY_LIVE_PROPERTIES` and already has them cached."""
    return {
        "size": record.cached_size,
        "preview": record.cached_preview,
        "hasAttachment": bool(record.cached_has_attachment),
        "attachments": [],
        "textBody": [],
        "htmlBody": [],
        "bodyValues": {"text": None, "html": None},
    }


def _body_part(record: EmailRecord, part_id: str, content: str | None) -> list[dict[str, Any]]:
    if content is None:
        return []
    message_blob_id = blob.encode_message_blob_id(record.mailbox_id, record.server_id)
    media_type = "text/plain" if part_id == "text" else "text/html"
    return [{"partId": part_id, "blobId": message_blob_id, "type": media_type, "size": len(content.encode())}]


def _to_jmap(
    record: EmailRecord, properties: list[str], live: dict[str, Any] | None, *, want_text: bool, want_html: bool
) -> dict[str, Any]:
    full: dict[str, Any] = {
        "id": record.email_id,
        "blobId": blob.encode_message_blob_id(record.mailbox_id, record.server_id),
        "mailboxIds": {record.mailbox_id: True},
        "threadId": record.thread_id,
        "subject": record.subject,
        "from": _addresses_to_jmap(record.from_addresses),
        "to": _addresses_to_jmap(record.to_addresses),
        "cc": _addresses_to_jmap(record.cc_addresses),
        "replyTo": _addresses_to_jmap(record.reply_to_addresses),
        "receivedAt": record.received_at,
        "keywords": _keywords(record),
        **dict.fromkeys(ALWAYS_NULL_PROPERTIES),
        "size": None, "preview": None, "hasAttachment": None, "attachments": None,
        "textBody": None, "htmlBody": None, "bodyValues": {},
    }
    if live is not None:
        full["size"] = live["size"]
        full["preview"] = live["preview"]
        full["hasAttachment"] = live["hasAttachment"]
        full["attachments"] = live["attachments"]
        full["textBody"] = live["textBody"]
        full["htmlBody"] = live["htmlBody"]
        body_values = {}
        if want_text and live["bodyValues"]["text"] is not None:
            body_values["text"] = {"value": live["bodyValues"]["text"], "isEncodingProblem": False,
                                    "isTruncated": False}
        if want_html and live["bodyValues"]["html"] is not None:
            body_values["html"] = {"value": live["bodyValues"]["html"], "isEncodingProblem": False,
                                    "isTruncated": False}
        full["bodyValues"] = body_values
    return {"id": record.email_id, **{p: full[p] for p in properties if p != "id"}}


def get(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    ids = arguments.get("ids")
    if not isinstance(ids, list):
        raise InvalidArgumentsError("ids is required")
    properties = arguments.get("properties")
    if properties is None:
        properties = CHEAP_PROPERTIES
    unsupported = set(properties) - ALL_PROPERTIES
    if unsupported:
        raise InvalidArgumentsError(f"unsupported Email property: {sorted(unsupported)!r}")

    fetch_text = bool(arguments.get("fetchTextBodyValues"))
    fetch_html = bool(arguments.get("fetchHTMLBodyValues"))
    fetch_all = bool(arguments.get("fetchAllBodyValues"))
    want_text = fetch_text or fetch_all
    want_html = fetch_html or fetch_all
    requested_live = set(properties) & LIVE_PROPERTIES
    needs_live = bool(requested_live) or want_text or want_html
    summary_only = needs_live and not want_text and not want_html and requested_live <= SUMMARY_LIVE_PROPERTIES

    with env.database.transaction() as conn:
        records: list[EmailRecord] = []
        not_found: list[str] = []
        for email_id in ids:
            record = cache.get_email(conn, env.account_id, email_id)
            if record is None:
                not_found.append(email_id)
            else:
                records.append(record)
        current = state.current_state(conn, env.account_id, "Email")

    jmap_list = []
    for record in records:
        live: dict[str, Any] | None = None
        if needs_live:
            if summary_only and record.cached_size is not None:
                live = _cached_summary(record)
            else:
                live = _fetch_live_data(record, env.adapter)
                if live is not None:
                    with env.database.transaction() as conn:
                        cache.set_email_live_summary(
                            conn, env.account_id, record.email_id,
                            preview=live["preview"], size=live["size"], has_attachment=live["hasAttachment"],
                        )
        jmap_list.append(_to_jmap(record, properties, live, want_text=want_text, want_html=want_html))
    return {"accountId": env.account_id, "state": current, "list": jmap_list, "notFound": not_found}


def _has_keyword(record: EmailRecord, keyword: str) -> bool:
    if keyword == "$seen":
        return record.seen
    if keyword == "$flagged":
        return record.flagged
    return False


def _address_matches(addresses: list[EmailAddress], needle: str) -> bool:
    needle = needle.lower()
    return any(needle in (a.email or "").lower() or needle in (a.name or "").lower() for a in addresses)


def _text_matches(record: EmailRecord, needle: str) -> bool:
    needle = needle.lower()
    if needle in (record.subject or "").lower():
        return True
    for addresses in (record.from_addresses, record.to_addresses, record.cc_addresses):
        if _address_matches(addresses, needle):
            return True
    return False


def _match_condition(condition: dict[str, Any], record: EmailRecord) -> bool:
    if "inMailbox" in condition and record.mailbox_id != condition["inMailbox"]:
        return False
    if "before" in condition and not (record.received_at and record.received_at < condition["before"]):
        return False
    if "after" in condition and not (record.received_at and record.received_at >= condition["after"]):
        return False
    if "hasKeyword" in condition and not _has_keyword(record, condition["hasKeyword"]):
        return False
    if "notKeyword" in condition and _has_keyword(record, condition["notKeyword"]):
        return False
    if "subject" in condition and condition["subject"].lower() not in (record.subject or "").lower():
        return False
    if "from" in condition and not _address_matches(record.from_addresses, condition["from"]):
        return False
    if "to" in condition and not _address_matches(record.to_addresses, condition["to"]):
        return False
    if "text" in condition and not _text_matches(record, condition["text"]):
        return False
    return True


_SORT_KEYS = {
    "receivedAt": lambda r: r.received_at or "",
    "subject": lambda r: (r.subject or "").lower(),
}

_DEFAULT_SORT = [{"property": "receivedAt", "isAscending": False}]


def _fast_path_mailbox_id(filter_: dict[str, Any] | None, sort: list[dict[str, Any]]) -> str | None:
    """The mailbox id if `filter_`/`sort` are exactly the common `inMailbox` + `receivedAt` DESC
    query (issue #2), else `None`. Lets `query()` use `cache.query_emails_page()`'s indexed SQL
    page instead of loading and sorting every cached email in the account."""
    if not isinstance(filter_, dict) or set(filter_) != {"inMailbox"}:
        return None
    mailbox_id = filter_.get("inMailbox")
    if not isinstance(mailbox_id, str):
        return None
    return mailbox_id if sort == _DEFAULT_SORT else None


def query(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    filter_ = arguments.get("filter")
    sort = arguments.get("sort") or _DEFAULT_SORT
    position = arguments.get("position", 0)
    limit = arguments.get("limit")
    calculate_total = arguments.get("calculateTotal", False)

    fast_mailbox_id = _fast_path_mailbox_id(filter_, sort) if position >= 0 else None

    with env.database.transaction() as conn:
        current = state.current_state(conn, env.account_id, "Email")
        if fast_mailbox_id is not None:
            total = cache.count_emails_in_mailbox(conn, env.account_id, fast_mailbox_id)[0] if calculate_total \
                else None
            window = cache.query_emails_page(conn, env.account_id, fast_mailbox_id, offset=position, limit=limit)
        else:
            records = [
                r for r in cache.list_emails_for_account(conn, env.account_id)
                if evaluate_filter(filter_, r, _match_condition)
            ]
            for comparator in reversed(sort):
                prop = comparator.get("property")
                key = _SORT_KEYS.get(prop)
                if key is None:
                    raise InvalidArgumentsError(f"unsupported Email/query sort property: {prop!r}")
                records.sort(key=key, reverse=not comparator.get("isAscending", True))
            total = len(records)
            if position < 0:
                position = max(total + position, 0)
            window = records[position: position + limit] if limit is not None else records[position:]

    result: dict[str, Any] = {
        "accountId": env.account_id,
        "queryState": current,
        "canCalculateChanges": True,
        "position": position,
        "ids": [r.email_id for r in window],
    }
    if limit is not None:
        result["limit"] = limit
    if calculate_total:
        result["total"] = total
    return result


def query_changes(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    since_query_state = arguments.get("sinceQueryState")
    if not isinstance(since_query_state, str):
        raise InvalidArgumentsError("sinceQueryState is required")
    max_changes = arguments.get("maxChanges")
    calculate_total = bool(arguments.get("calculateTotal"))

    with env.database.transaction() as conn:
        try:
            diff = state.get_changes(conn, env.account_id, "Email", since_query_state, max_changes)
        except state.CannotCalculateChangesError as exc:
            raise CannotCalculateChangesError(str(exc)) from exc

    full = query(env, {
        "filter": arguments.get("filter"), "sort": arguments.get("sort"), "calculateTotal": calculate_total,
    })
    removed, added = compute_query_changes(diff.created, diff.updated, diff.destroyed, full["ids"])
    result: dict[str, Any] = {
        "accountId": env.account_id,
        "oldQueryState": diff.old_state,
        "newQueryState": diff.new_state,
        "removed": removed,
        "added": added,
    }
    if calculate_total:
        result["total"] = full["total"]
    return result


CREATE_REQUIRED_KEYWORD = "$draft"
CREATE_ALLOWED_KEYWORDS = {"$draft", "$seen", "$flagged"}
UPDATE_SUPPORTED_KEYWORDS = {"$seen", "$flagged"}
_UPDATE_MUTABLE_TOP_LEVEL = {"keywords", "mailboxIds"}
_ADDRESS_HEADERS = (("From", "from"), ("To", "to"), ("Cc", "cc"), ("Bcc", "bcc"), ("Reply-To", "replyTo"))


def _resolve_bodies(props: dict[str, Any]) -> tuple[str | None, str | None]:
    body_values = props.get("bodyValues") or {}

    def resolve(parts: Any) -> str | None:
        if not parts:
            return None
        part_id = parts[0].get("partId") if isinstance(parts[0], dict) else None
        entry = body_values.get(part_id) if part_id else None
        return entry.get("value") if isinstance(entry, dict) else None

    return resolve(props.get("textBody")), resolve(props.get("htmlBody"))


def _compose_message(env: Environment, props: dict[str, Any]) -> tuple[Message | None, dict[str, Any] | None]:
    """Builds a stdlib `EmailMessage` from JMAP Email creation properties.

    Returns `(message, None)` on success, or `(None, error)` where `error` is
    the per-object `notCreated` value (e.g. an unresolvable attachment blobId).
    """
    message = EmailMessage(policy=email_policy.SMTP)
    subject = props.get("subject")
    if subject is not None:
        if not isinstance(subject, str):
            return None, {"type": "invalidProperties", "properties": ["subject"]}
        message["Subject"] = subject

    for header, key in _ADDRESS_HEADERS:
        addresses = props.get(key)
        if not addresses:
            continue
        if not isinstance(addresses, list):
            return None, {"type": "invalidProperties", "properties": [key]}
        formatted = []
        for address in addresses:
            email_address = address.get("email") if isinstance(address, dict) else None
            if not email_address:
                return None, {"type": "invalidProperties", "properties": [key]}
            name = address.get("name")
            formatted.append(formataddr((name, email_address)) if name else email_address)
        message[header] = ", ".join(formatted)

    in_reply_to = props.get("inReplyTo")
    if in_reply_to:
        message["In-Reply-To"] = " ".join(in_reply_to)
    references = props.get("references")
    if references:
        message["References"] = " ".join(references)
    for header in props.get("headers") or []:
        name = header.get("name") if isinstance(header, dict) else None
        value = header.get("value") if isinstance(header, dict) else None
        if not name or value is None:
            return None, {"type": "invalidProperties", "properties": ["headers"]}
        message[name] = value

    text_body, html_body = _resolve_bodies(props)
    if text_body is not None and html_body is not None:
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")
    elif html_body is not None:
        message.set_content(html_body, subtype="html")
    elif text_body is not None:
        message.set_content(text_body)
    else:
        message.set_content("")

    for attachment in props.get("attachments") or []:
        if not isinstance(attachment, dict) or not attachment.get("blobId"):
            return None, {"type": "invalidProperties", "properties": ["attachments"]}
        try:
            locator = blob.decode_blob_id(attachment["blobId"])
        except ValueError:
            return None, {"type": "invalidProperties", "properties": ["attachments"]}
        resolved = blob.resolve_blob(locator, account_id=env.account_id, adapter=env.adapter,
                                      database=env.database)
        if resolved is None:
            return None, {"type": "invalidProperties", "properties": ["attachments"]}
        data, default_content_type = resolved
        content_type = attachment.get("type") or default_content_type
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                                filename=attachment.get("name"))
    return message, None


def _validate_create(env: Environment, props: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, bool, bool]:
    """Returns `(error, draftsMailboxId, read, flagged)`; only draft creation is supported (plan.md's M2 note)."""
    mailbox_ids = props.get("mailboxIds")
    if not isinstance(mailbox_ids, dict) or list(mailbox_ids.values()) != [True]:
        return {"type": "invalidProperties", "properties": ["mailboxIds"]}, None, False, False
    mailbox_id = next(iter(mailbox_ids))

    with env.database.transaction() as conn:
        target = cache.get_mailbox(conn, env.account_id, mailbox_id)
    role = MAILBOX_ROLES.get(FolderType(target.folder_type)) if target is not None else None
    if role != "drafts":
        return {"type": "invalidProperties", "properties": ["mailboxIds"]}, None, False, False

    keywords = props.get("keywords")
    if not isinstance(keywords, dict) or not keywords.get(CREATE_REQUIRED_KEYWORD):
        return {"type": "invalidProperties", "properties": ["keywords"]}, None, False, False
    unsupported = {k for k, v in keywords.items() if v} - CREATE_ALLOWED_KEYWORDS
    if unsupported:
        return {"type": "invalidProperties", "properties": ["keywords"]}, None, False, False

    return None, mailbox_id, bool(keywords.get("$seen")), bool(keywords.get("$flagged"))


def _resolve_patch_object(current: dict[str, bool], patch: dict[str, Any], prefix: str) -> dict[str, bool]:
    """A JMAP patch object's value for `prefix`: full replacement if `prefix` is a top-level
    key, else `current` with every `prefix/subkey` entry applied (RFC 8620 section 5.3)."""
    if prefix in patch:
        value = patch[prefix]
        return dict(value) if isinstance(value, dict) else {}
    result = dict(current)
    for key, value in patch.items():
        if not key.startswith(prefix + "/"):
            continue
        subkey = key[len(prefix) + 1:].replace("~1", "/").replace("~0", "~")
        if value:
            result[subkey] = True
        else:
            result.pop(subkey, None)
    return result


def _apply_update(env: Environment, email_id: str, record: EmailRecord, patch: dict[str, Any]) -> dict[str, Any] | None:
    """Applies a supported update patch. Returns an error dict, or `None` on success."""
    unknown = sorted({key.split("/")[0] for key in patch} - _UPDATE_MUTABLE_TOP_LEVEL)
    if unknown:
        return {"type": "invalidProperties", "properties": unknown}

    seen: bool | None = None
    flagged: bool | None = None
    if "keywords" in patch or any(k.startswith("keywords/") for k in patch):
        new_keywords = _resolve_patch_object(_keywords(record), patch, "keywords")
        if set(k for k, v in new_keywords.items() if v) - UPDATE_SUPPORTED_KEYWORDS:
            return {"type": "invalidProperties", "properties": ["keywords"]}
        seen = bool(new_keywords.get("$seen"))
        flagged = bool(new_keywords.get("$flagged"))

    new_mailbox_id: str | None = None
    if "mailboxIds" in patch or any(k.startswith("mailboxIds/") for k in patch):
        new_mailbox_ids = _resolve_patch_object({record.mailbox_id: True}, patch, "mailboxIds")
        targets = [k for k, v in new_mailbox_ids.items() if v]
        if len(targets) != 1:
            return {"type": "invalidProperties", "properties": ["mailboxIds"]}
        new_mailbox_id = targets[0]
        if new_mailbox_id != record.mailbox_id:
            if not env.policy.allow_moves:
                return {"type": ForbiddenError.type}
            with env.database.transaction() as conn:
                target = cache.get_mailbox(conn, env.account_id, new_mailbox_id)
            if target is None:
                return {"type": "invalidProperties", "properties": ["mailboxIds"]}

    try:
        if seen is not None or flagged is not None:
            status = env.sync.set_email_keywords(env.account_id, email_id, env.adapter, seen=seen, flagged=flagged)
            if status != "1":
                return {"type": "serverFail"}
        if new_mailbox_id is not None and new_mailbox_id != record.mailbox_id:
            outcome = env.sync.move_email(env.account_id, email_id, new_mailbox_id, env.adapter)
            if outcome != "moved":
                return {"type": "serverFail"}
    except BackendError:
        return {"type": "serverFail"}
    return None


def set_(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    """`Email/set`. Named `set_` to avoid shadowing the builtin `set` used elsewhere in this module."""
    create = arguments.get("create") or {}
    update = arguments.get("update") or {}
    destroy = arguments.get("destroy") or []
    if not isinstance(create, dict) or not isinstance(update, dict) or not isinstance(destroy, list):
        raise InvalidArgumentsError("create/update must be objects and destroy must be an array")

    with env.database.transaction() as conn:
        old_state = state.current_state(conn, env.account_id, "Email")

    created: dict[str, Any] = {}
    not_created: dict[str, Any] = {}
    for client_id, props in create.items():
        if not isinstance(props, dict):
            not_created[client_id] = {"type": "invalidProperties"}
            continue
        error, mailbox_id, read, flagged = _validate_create(env, props)
        if error is not None:
            not_created[client_id] = error
            continue
        message, error = _compose_message(env, props)
        if error is not None:
            not_created[client_id] = error
            continue
        assert message is not None and mailbox_id is not None
        try:
            status, email_id = env.sync.create_draft(
                env.account_id, mailbox_id, message, read=read, flagged=flagged,
                client_id=secrets.token_urlsafe(16), adapter=env.adapter,
            )
        except BackendError:
            not_created[client_id] = {"type": "serverFail"}
            continue
        if status != "1" or email_id is None:
            not_created[client_id] = {"type": "serverFail"}
            continue
        created[client_id] = {"id": email_id}

    updated: dict[str, None] = {}
    not_updated: dict[str, Any] = {}
    for email_id, patch in update.items():
        with env.database.transaction() as conn:
            record = cache.get_email(conn, env.account_id, email_id)
        if record is None:
            not_updated[email_id] = {"type": "notFound"}
            continue
        if not isinstance(patch, dict):
            not_updated[email_id] = {"type": "invalidProperties"}
            continue
        error = _apply_update(env, email_id, record, patch)
        if error is not None:
            not_updated[email_id] = error
            continue
        updated[email_id] = None

    destroyed: list[str] = []
    not_destroyed: dict[str, Any] = {}
    for email_id in destroy:
        with env.database.transaction() as conn:
            record = cache.get_email(conn, env.account_id, email_id)
        if record is None:
            not_destroyed[email_id] = {"type": "notFound"}
            continue
        if not env.policy.allow_delete:
            not_destroyed[email_id] = {"type": ForbiddenError.type}
            continue
        try:
            delete_status = env.sync.delete_email(env.account_id, email_id, env.adapter)
        except BackendError:
            not_destroyed[email_id] = {"type": "serverFail"}
            continue
        if delete_status != "1":
            not_destroyed[email_id] = {"type": "serverFail"}
            continue
        destroyed.append(email_id)

    with env.database.transaction() as conn:
        new_state = state.current_state(conn, env.account_id, "Email")

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
            result = state.get_changes(conn, env.account_id, "Email", since_state, max_changes)
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
