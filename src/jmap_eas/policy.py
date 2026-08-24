"""Centralized method/operation allowlist (plan.md section 5).

The session resource and dispatcher both derive their advertised methods and
capabilities from `METHODS`/`CAPABILITIES` here, so a method that isn't
listed is never reachable and never advertised. `EmailSubmission/*` is M3, not
stubbed in ahead of time. Within the mutating methods here, `Mailbox/set`
destroy and `Email/set` destroy/move are further gated per object by
`PolicyConfig.allow_delete`/`allow_moves` (plan.md section 6) -- disabling
one doesn't remove the method, since e.g. read/flag updates must stay
available even with moves or deletes turned off.
"""
from __future__ import annotations

from .jmap import core, email, mailbox, thread
from .jmap.dispatcher import MethodHandler

METHODS: dict[str, MethodHandler] = {
    "Core/echo": core.echo,
    "Mailbox/get": mailbox.get,
    "Mailbox/query": mailbox.query,
    "Mailbox/changes": mailbox.changes,
    "Mailbox/set": mailbox.set_,
    "Email/get": email.get,
    "Email/query": email.query,
    "Email/changes": email.changes,
    "Email/set": email.set_,
    "Thread/get": thread.get,
}

CORE_CAPABILITY = "urn:ietf:params:jmap:core"
MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"

MAX_UPLOAD_SIZE_BYTES = 50_000_000

CAPABILITIES: dict[str, dict[str, object]] = {
    CORE_CAPABILITY: {
        "maxSizeUpload": MAX_UPLOAD_SIZE_BYTES,
        "maxConcurrentUpload": 4,
        "maxSizeRequest": 10_000_000,
        "maxConcurrentRequests": 4,
        "maxCallsInRequest": 32,
        "maxObjectsInGet": 500,
        "maxObjectsInSet": 50,
        "collationAlgorithms": [],
    },
    MAIL_CAPABILITY: {
        "maxMailboxesPerEmail": 1,
        "maxMailboxDepth": None,
        "maxSizeMailboxName": 255,
        "maxSizeAttachmentsPerEmail": 25_000_000,
        "emailQuerySortOptions": ["receivedAt", "subject"],
        "mayCreateTopLevelMailbox": False,
        "submissionExtensions": {},
    },
}
