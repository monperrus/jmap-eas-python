"""Centralized method/operation allowlist (plan.md section 5).

The session resource and dispatcher both derive their advertised methods and
capabilities from `METHODS`/`CAPABILITIES` here, so a method that isn't
listed is never reachable and never advertised. Within the mutating methods
here, `Mailbox/set` destroy, `Email/set` destroy/move, and `EmailSubmission/
set` create are further gated per object by `PolicyConfig.allow_delete`/
`allow_moves`/`allow_send` (plan.md section 6) -- disabling one doesn't
remove the method, since e.g. read/flag updates must stay available even
with moves or deletes turned off. `EmailSubmission`'s *capability* is
additionally omitted from a request-scoped account's `accountCapabilities`
when `allow_send` is off (`jmap/session.py`), since unlike `Email/set` it has
no other purpose a disabled account would still need.
"""
from __future__ import annotations

from .jmap import core, email, identity, mailbox, submission, thread
from .jmap.dispatcher import MethodHandler

METHODS: dict[str, MethodHandler] = {
    "Core/echo": core.echo,
    "Mailbox/get": mailbox.get,
    "Mailbox/query": mailbox.query,
    "Mailbox/queryChanges": mailbox.query_changes,
    "Mailbox/changes": mailbox.changes,
    "Mailbox/set": mailbox.set_,
    "Email/get": email.get,
    "Email/query": email.query,
    "Email/queryChanges": email.query_changes,
    "Email/changes": email.changes,
    "Email/set": email.set_,
    "Thread/get": thread.get,
    "Identity/get": identity.get,
    "EmailSubmission/get": submission.get,
    "EmailSubmission/set": submission.set_,
    "EmailSubmission/changes": submission.changes,
}

CORE_CAPABILITY = "urn:ietf:params:jmap:core"
MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"
SUBMISSION_CAPABILITY = "urn:ietf:params:jmap:submission"

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
    },
    SUBMISSION_CAPABILITY: {
        "maxDelayedSend": 0,  # SendMail is immediate; there is no Outbox delay to schedule
        "submissionExtensions": {},
    },
}
