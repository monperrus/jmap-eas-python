"""`Thread/get` (RFC 8621 section 3): group cached email ids by thread id."""
from __future__ import annotations

from typing import Any

from ..store import cache, state
from .dispatcher import Environment


def get(env: Environment, arguments: dict[str, Any]) -> dict[str, Any]:
    ids = arguments.get("ids")
    with env.database.transaction() as conn:
        if ids is None:
            ids = sorted({record.thread_id for record in cache.list_emails_for_account(conn, env.account_id)})
        jmap_list = []
        not_found = []
        for thread_id in ids:
            email_ids = cache.list_thread_email_ids(conn, env.account_id, thread_id)
            if email_ids:
                jmap_list.append({"id": thread_id, "emailIds": email_ids})
            else:
                not_found.append(thread_id)
        # Thread membership is entirely derived from emails, so the Email type's
        # state doubles as the Thread state; there is no separate Thread change log.
        current = state.current_state(conn, env.account_id, "Email")
    return {"accountId": env.account_id, "state": current, "list": jmap_list, "notFound": not_found}
