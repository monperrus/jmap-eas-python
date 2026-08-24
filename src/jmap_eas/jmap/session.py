"""RFC 8620 section 2 session resource.

One authenticated HTTP request maps to exactly one EAS account (`auth.py`),
so the session always advertises exactly that one account -- never another
configured account the caller didn't authenticate as (plan.md section 6).
"""
from __future__ import annotations

from typing import Any

from .. import policy


def build_session(account_id: str, base_url: str) -> dict[str, Any]:
    mail_capability = policy.CAPABILITIES[policy.MAIL_CAPABILITY]
    return {
        "capabilities": policy.CAPABILITIES,
        "accounts": {
            account_id: {
                "name": account_id,
                "isPersonal": True,
                "isReadOnly": True,
                "accountCapabilities": {
                    policy.CORE_CAPABILITY: {},
                    policy.MAIL_CAPABILITY: mail_capability,
                },
            },
        },
        "primaryAccounts": {
            policy.CORE_CAPABILITY: account_id,
            policy.MAIL_CAPABILITY: account_id,
        },
        "username": account_id,
        "apiUrl": f"{base_url}/api",
        "downloadUrl": f"{base_url}/download/{{accountId}}/{{blobId}}/{{name}}",
        "uploadUrl": f"{base_url}/upload/{{accountId}}",
        "eventSourceUrl": f"{base_url}/eventsource",
        "state": "single-account",
    }
