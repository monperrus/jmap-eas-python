"""Centralized method/operation allowlist (plan.md section 5).

The session resource and dispatcher both derive their advertised methods and
capabilities from `METHODS`/`CAPABILITIES` here, so a method that isn't
listed is never reachable and never advertised. M1 is read-only: every
mutating method (`*/set`, `EmailSubmission/*`) is added by the milestone that
implements it, not stubbed in ahead of time.
"""
from __future__ import annotations

from .jmap import core, email, mailbox, thread
from .jmap.dispatcher import MethodHandler

METHODS: dict[str, MethodHandler] = {
    "Core/echo": core.echo,
    "Mailbox/get": mailbox.get,
    "Mailbox/query": mailbox.query,
    "Mailbox/changes": mailbox.changes,
    "Email/get": email.get,
    "Email/query": email.query,
    "Email/changes": email.changes,
    "Thread/get": thread.get,
}

CORE_CAPABILITY = "urn:ietf:params:jmap:core"
MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"

CAPABILITIES: dict[str, dict[str, object]] = {
    CORE_CAPABILITY: {
        "maxSizeUpload": 50_000_000,
        "maxConcurrentUpload": 4,
        "maxSizeRequest": 10_000_000,
        "maxConcurrentRequests": 4,
        "maxCallsInRequest": 32,
        "maxObjectsInGet": 500,
        "maxObjectsInSet": 0,
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
