"""Typed internal models shared between the store, backend, and JMAP layers."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EmailAddress:
    """One JMAP `EmailAddress` object: `{"name": ..., "email": ...}`."""

    email: str | None
    name: str | None = None

    def to_jmap(self) -> dict[str, str | None]:
        return {"name": self.name, "email": self.email}


@dataclass(frozen=True)
class MailboxRecord:
    """A cached EAS folder. `mailbox_id` is the EAS `Folder.ServerId`, used directly as the JMAP Mailbox id."""

    account_id: str
    mailbox_id: str
    parent_id: str
    name: str
    folder_type: int
    sync_key: str


@dataclass(frozen=True)
class EmailRecord:
    """A cached EAS mail item, normalized to the subset of JMAP Email properties sync metadata provides.

    `email_id` is a locally assigned stable id (plan.md section 3); `mailbox_id`/`server_id` are the
    item's current EAS location and are the only fields a move (M2) needs to update.
    """

    account_id: str
    email_id: str
    mailbox_id: str
    server_id: str
    thread_id: str
    subject: str | None
    from_addresses: list[EmailAddress] = field(default_factory=list)
    to_addresses: list[EmailAddress] = field(default_factory=list)
    cc_addresses: list[EmailAddress] = field(default_factory=list)
    reply_to_addresses: list[EmailAddress] = field(default_factory=list)
    received_at: str | None = None
    seen: bool = False
    flagged: bool = False
    cached_preview: str | None = None
    cached_size: int | None = None
    cached_has_attachment: bool | None = None
    """`Email/get` live-data summary, persisted after the first `ItemOperations` fetch and
    cleared by every `upsert_email()` call from `Sync` (issue #2): `None` means "not fetched
    since this item's content last changed," not "no attachment" or "empty preview.\""""
