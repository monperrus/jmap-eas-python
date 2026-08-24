"""EAS field/folder/body to JMAP mapping (plan.md section 2).

`pyactivesync`'s `Sync` only implements the `Email` codepage (no `Email2`
conversation fields, no size), so threading here always uses the normalized
subject fallback described in plan.md section 6, not EAS conversation
metadata, and size/preview/body properties are not derivable from sync
metadata alone -- `Email/get` fetches those on demand (see `jmap/email.py`).
"""
from __future__ import annotations

import re
from email.utils import getaddresses

from pyactivesync.models import Folder, FolderType, SyncItem

from ..models import EmailAddress, EmailRecord, MailboxRecord

# JMAP Mailbox `role` for the EAS folder types that have a direct equivalent
# (plan.md section 1's "Mailbox role" row). Folders outside this map get no role.
MAILBOX_ROLES: dict[FolderType, str] = {
    FolderType.INBOX: "inbox",
    FolderType.DRAFTS: "drafts",
    FolderType.DELETED_ITEMS: "trash",
    FolderType.SENT_ITEMS: "sent",
    FolderType.OUTBOX: "outbox",
}

# This is a Mail-only bridge (plan.md's goal): Calendar/Contacts/Tasks/Notes/
# Journal/RecipientCache folders exist in the EAS hierarchy but are never
# exposed as JMAP Mailboxes or synced -- pyactivesync's `Sync` targets the
# Email item class and does not reliably decode other classes' items.
MAIL_FOLDER_TYPES: frozenset[FolderType] = frozenset({
    FolderType.INBOX, FolderType.DRAFTS, FolderType.DELETED_ITEMS, FolderType.SENT_ITEMS,
    FolderType.OUTBOX, FolderType.USER_MAIL,
})


def is_mail_folder(folder_type: FolderType) -> bool:
    return folder_type in MAIL_FOLDER_TYPES


_SUBJECT_PREFIX = re.compile(r"^\s*(re|fw|fwd|aw)(\[\d+\])?\s*:\s*", re.IGNORECASE)


def map_folder_to_mailbox(account_id: str, folder: Folder) -> MailboxRecord:
    return MailboxRecord(
        account_id=account_id,
        mailbox_id=folder.id,
        parent_id=folder.parent_id,
        name=folder.name,
        folder_type=int(folder.type),
        sync_key="0",
    )


def normalize_subject(subject: str | None) -> str:
    """Strip repeated Re:/Fwd:/Fw:/Aw: prefixes and surrounding whitespace, lowercased."""
    text = subject or ""
    while True:
        stripped = _SUBJECT_PREFIX.sub("", text)
        if stripped == text:
            break
        text = stripped
    return text.strip().lower()


def thread_key_for_item(mailbox_id: str, server_id: str, subject: str | None) -> str:
    """The cache's thread-grouping key: normalized subject, or a per-item fallback when blank."""
    normalized = normalize_subject(subject)
    if normalized:
        return f"subject:{normalized}"
    return f"item:{mailbox_id}:{server_id}"


def _parse_address_list(value: str | None) -> list[EmailAddress]:
    """EAS `To`/`Cc`/`From`/`ReplyTo` are `;`-separated; `email.utils` expects `,`-separated."""
    if not value or not value.strip():
        return []
    pairs = getaddresses([value.replace(";", ",")])
    return [EmailAddress(email=addr, name=name or None) for name, addr in pairs if addr]


def map_sync_item_to_email(
    *, account_id: str, mailbox_id: str, thread_id: str, email_id: str, item: SyncItem
) -> EmailRecord:
    fields = item.fields
    return EmailRecord(
        account_id=account_id,
        email_id=email_id,
        mailbox_id=mailbox_id,
        server_id=item.server_id,
        thread_id=thread_id,
        subject=fields.get("Email.Subject"),
        from_addresses=_parse_address_list(fields.get("Email.From")),
        to_addresses=_parse_address_list(fields.get("Email.To")),
        cc_addresses=_parse_address_list(fields.get("Email.Cc")),
        reply_to_addresses=_parse_address_list(fields.get("Email.ReplyTo")),
        received_at=fields.get("Email.DateReceived"),
        seen=fields.get("Email.Read") == "1",
        flagged=fields.get("Email.Status") == "2",
    )
