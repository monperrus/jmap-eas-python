"""JMAP authentication and account resolution (plan.md section 2).

HTTP Basic auth against the bridge itself, distinct from the EAS credentials
`registry.py` uses: the Basic auth *username* is the configured account id,
the *password* is that account's `api_token` (plan.md section 6: credentials
live only in the proxy process and are never re-derived from the mailbox
password). Comparison is constant-time to avoid a timing side channel.
"""
from __future__ import annotations

import base64
import binascii
import hmac

from .config import AccountConfig


class AuthenticationError(Exception):
    """The request's credentials are missing or do not match a configured account."""


def _parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if header is None or not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    username, _, password = decoded.partition(":")
    return username, password


def authenticate(authorization_header: str | None, accounts: dict[str, AccountConfig]) -> str:
    """Returns the authenticated `accountId`, or raises `AuthenticationError`."""
    credentials = _parse_basic_auth(authorization_header)
    if credentials is None:
        raise AuthenticationError("missing or malformed Authorization header")
    account_id, token = credentials
    account = accounts.get(account_id)
    if account is None:
        raise AuthenticationError("unknown account")
    expected = account.resolve_api_token()
    if not hmac.compare_digest(token, expected):
        raise AuthenticationError("invalid token")
    return account_id
