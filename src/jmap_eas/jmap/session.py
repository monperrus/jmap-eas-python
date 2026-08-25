"""RFC 8620 section 2 session resource.

One authenticated HTTP request maps to exactly one EAS account (`auth.py`),
so the session always advertises exactly that one account -- never another
configured account the caller didn't authenticate as (plan.md section 6).
"""
from __future__ import annotations

from typing import Any

from .. import policy
from ..config import PolicyConfig


def build_session(account_id: str, base_url: str, account_policy: PolicyConfig) -> dict[str, Any]:
    account_capabilities = {
        policy.CORE_CAPABILITY: {},
        policy.MAIL_CAPABILITY: policy.CAPABILITIES[policy.MAIL_CAPABILITY],
    }
    if account_policy.allow_send:
        # Identity/get is introduced alongside EmailSubmission by RFC 8621 section 7 and is
        # bundled with the same capability; an account with sending disabled has no use for it.
        account_capabilities[policy.SUBMISSION_CAPABILITY] = policy.CAPABILITIES[policy.SUBMISSION_CAPABILITY]

    primary_accounts = {policy.CORE_CAPABILITY: account_id, policy.MAIL_CAPABILITY: account_id}
    if account_policy.allow_send:
        primary_accounts[policy.SUBMISSION_CAPABILITY] = account_id

    return {
        "capabilities": policy.CAPABILITIES,
        "accounts": {
            account_id: {
                "name": account_id,
                "isPersonal": True,
                "isReadOnly": False,
                "accountCapabilities": account_capabilities,
            },
        },
        "primaryAccounts": primary_accounts,
        "username": account_id,
        "apiUrl": f"{base_url}/api",
        "downloadUrl": f"{base_url}/download/{{accountId}}/{{blobId}}/{{name}}",
        "uploadUrl": f"{base_url}/upload/{{accountId}}",
        "eventSourceUrl": f"{base_url}/eventsource",
        "state": "single-account",
    }
