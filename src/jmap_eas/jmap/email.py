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
from email import policy as email_policy
from email.message import EmailMessage
from typing import Any

from pyactivesync.models import AttachmentInfo, BodyType

from ..backend.eas import EasAdapter
from ..errors import BackendError, CannotCalculateChangesError, InvalidArgumentsError
from ..models import EmailAddress, EmailRecord
from ..store import cache, state
from . import blob
from .dispatcher import Environment
from .filtering import evaluate_filter

CHEAP_PROPERTIES = [
    "id", "blobId", "mailboxIds", "threadId", "subject", "from", "to", "cc", "replyTo", "receivedAt", "keywords",
]
LIVE_PROPERTIES = {"size", "preview", "hasAttachment", "bodyValues", "textBody", "htmlBody", "attachments"}
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


def _parse_mime(data: bytes) -> EmailMessage:
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
    message = _parse_mime(mime_bytes)
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
    needs_live = bool(set(properties) & LIVE_PROPERTIES) or want_text or want_html

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
        live = _fetch_live_data(record, env.adapter) if needs_live else None
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


def query(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    filter_ = arguments.get("filter")
    sort = arguments.get("sort") or [{"property": "receivedAt", "isAscending": False}]
    position = arguments.get("position", 0)
    limit = arguments.get("limit")
    calculate_total = arguments.get("calculateTotal", False)

    with env.database.transaction() as conn:
        records = [
            r for r in cache.list_emails_for_account(conn, env.account_id)
            if evaluate_filter(filter_, r, _match_condition)
        ]
        current = state.current_state(conn, env.account_id, "Email")

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
        "canCalculateChanges": False,
        "position": position,
        "ids": [r.email_id for r in window],
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
