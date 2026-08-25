"""`GET /eventsource`: SSE push backed by EAS `Ping` (RFC 8620 section 7.3).

`Ping` blocks (a real long poll against EAS) for up to `heartbeat` seconds,
so each iteration of the stream loop belongs off the event loop; `app.py`
owns wiring that into a `StreamingResponse` via the thread pool. This module
holds everything that doesn't need Starlette: parsing query parameters,
running one ping-then-sync cycle, and formatting SSE frames -- all directly
testable without a live streaming HTTP connection.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..backend.sync import SyncCoordinator
from ..registry import AccountContext
from ..store import cache, state
from ..store.db import Database

ALL_TYPES = ("Mailbox", "Email", "Thread", "EmailSubmission")
PING_HEARTBEAT_SECONDS = 60


@dataclass(frozen=True)
class EventSourceParams:
    types: tuple[str, ...]
    close_after_state: bool
    ping_interval: int


def parse_params(query: dict[str, str]) -> EventSourceParams:
    """`types`: comma-separated type names or `*` for all; unknown names are dropped.
    `closeafter`: `"state"` closes the stream after the first state event, else stays open.
    `ping`: seconds between keep-alive `ping` events, `0`/absent/invalid disables them."""
    types_param = query.get("types", "*")
    types = ALL_TYPES if types_param == "*" else tuple(t for t in types_param.split(",") if t in ALL_TYPES)
    close_after_state = query.get("closeafter") == "state"
    try:
        ping_interval = max(0, int(query.get("ping", "0")))
    except ValueError:
        ping_interval = 0
    return EventSourceParams(types=types, close_after_state=close_after_state, ping_interval=ping_interval)


def current_states(database: Database, account_id: str, types: tuple[str, ...]) -> dict[str, str]:
    with database.transaction() as conn:
        return {type_: state.current_state(conn, account_id, type_) for type_ in types}


def mail_folder_ids(database: Database, account_id: str) -> list[str]:
    with database.transaction() as conn:
        return [mailbox.mailbox_id for mailbox in cache.list_mailboxes(conn, account_id)]


def ping_and_sync(
    sync: SyncCoordinator, context: AccountContext, account_id: str, folder_ids: list[str]
) -> list[str]:
    """One blocking Ping-then-targeted-sync cycle. Returns the folder ids EAS reported changed.

    Uses `context.ping` (never `context.command`) so this long poll can never
    block an ordinary JMAP request for the same account, and the sync that
    follows a change uses `context.command` under its usual lock so it can't
    race a concurrent `POST /api` either.
    """
    with context.ping_lock:
        result = context.ping.ping(folder_ids, heartbeat=PING_HEARTBEAT_SECONDS,
                                    timeout=PING_HEARTBEAT_SECONDS + 10)
    changed_folder_ids: list[str] = list(result.changed_folder_ids)
    if not changed_folder_ids:
        return []
    with context.command_lock:
        for folder_id in changed_folder_ids:
            sync.sync_folder(account_id, folder_id, context.command)
    return changed_folder_ids


def format_state_event(account_id: str, changed: dict[str, str]) -> str:
    payload = {"@type": "StateChange", "changed": {account_id: changed}}
    return f"event: state\ndata: {json.dumps(payload)}\n\n"


def format_ping_event(interval: int) -> str:
    return f"event: ping\ndata: {json.dumps({'interval': interval})}\n\n"
