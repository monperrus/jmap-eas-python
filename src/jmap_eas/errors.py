"""JMAP error types and mapping from backend (pyactivesync) exceptions.

Backend exception details must never reach a JMAP client response (plan.md
section 6): :class:`BackendError` carries the original exception as `cause`
for server-side logging only, and exposes a generic message instead.
"""
from __future__ import annotations

from pyactivesync.exceptions import EASError


class JmapError(Exception):
    """Base for errors that map to an RFC 8620 request- or method-level error type."""

    type: str = "serverFail"


class UnknownAccountError(JmapError):
    """The request referenced an `accountId` this deployment does not know about."""

    type = "accountNotFound"

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        super().__init__(f"unknown accountId: {account_id!r}")


class ForbiddenError(JmapError):
    """The operation is disabled by deployment policy (plan.md section 6)."""

    type = "forbidden"


class BackendError(JmapError):
    """An EAS operation failed. `cause` is for server-side logging, never for client responses."""

    type = "serverFail"

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__("an internal error occurred while contacting the mailbox")


def map_eas_exception(exc: EASError) -> BackendError:
    return BackendError(exc)
