"""`Identity/get`, synthesized from account configuration (plan.md section 5's M3 note).

There is exactly one identity per account -- this bridge has no concept of
send-as aliases -- so `Identity/set` is not implemented; the identity is
fixed for the account's lifetime.
"""
from __future__ import annotations

from typing import Any

from .dispatcher import Environment

IDENTITY_ID = "identity"


def _identity_object(env: Environment) -> dict[str, Any]:
    return {
        "id": IDENTITY_ID,
        "name": env.account_id,
        "email": env.identity_email,
        "replyTo": None,
        "bcc": None,
        "textSignature": "",
        "htmlSignature": "",
        "mayDelete": False,
    }


def get(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    ids = arguments.get("ids")
    identity = _identity_object(env)
    if ids is None:
        found = [identity]
        not_found: list[str] = []
    else:
        found = [identity] if IDENTITY_ID in ids else []
        not_found = [i for i in ids if i != IDENTITY_ID]
    return {"accountId": env.account_id, "state": "static", "list": found, "notFound": not_found}
