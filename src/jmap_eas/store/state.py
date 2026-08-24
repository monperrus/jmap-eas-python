"""JMAP state and the change log that `*/changes` methods read (plan.md section 4).

`seq` is one monotonic counter per account, shared by every object type,
so ordering between types never matters for coalescing. A type's JMAP
`state` string is simply the highest `seq` recorded for that type.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


class CannotCalculateChangesError(Exception):
    """`sinceState` does not correspond to a known point in this account's history."""


@dataclass(frozen=True)
class ChangesResult:
    old_state: str
    new_state: str
    has_more_changes: bool
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)


def append_change(conn: sqlite3.Connection, account_id: str, type_: str, object_id: str, change: str) -> int:
    """Record one `created`/`updated`/`destroyed` change. Returns the assigned `seq`."""
    if change not in {"created", "updated", "destroyed"}:
        raise ValueError(f"invalid change kind: {change!r}")
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM change_log WHERE account_id = ?", (account_id,)).fetchone()
    seq = int(row[0]) + 1
    conn.execute(
        "INSERT INTO change_log (account_id, seq, type, object_id, change) VALUES (?, ?, ?, ?, ?)",
        (account_id, seq, type_, object_id, change),
    )
    return seq


def current_state(conn: sqlite3.Connection, account_id: str, type_: str) -> str:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM change_log WHERE account_id = ? AND type = ?", (account_id, type_)
    ).fetchone()
    return str(row[0])


def _parse_state(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise CannotCalculateChangesError(f"invalid state: {value!r}") from None
    if parsed < 0:
        raise CannotCalculateChangesError(f"invalid state: {value!r}")
    return parsed


def _coalesce(rows: list[sqlite3.Row]) -> tuple[list[str], list[str], list[str]]:
    order: list[str] = []
    created_within: dict[str, bool] = {}
    last_change: dict[str, str] = {}
    for row in rows:
        object_id = str(row["object_id"])
        if object_id not in last_change:
            order.append(object_id)
            created_within[object_id] = False
        if row["change"] == "created":
            created_within[object_id] = True
        last_change[object_id] = str(row["change"])

    created: list[str] = []
    updated: list[str] = []
    destroyed: list[str] = []
    for object_id in order:
        is_new = created_within[object_id]
        final = last_change[object_id]
        if is_new and final == "destroyed":
            continue
        if is_new:
            created.append(object_id)
        elif final == "destroyed":
            destroyed.append(object_id)
        else:
            updated.append(object_id)
    return created, updated, destroyed


def get_changes(
    conn: sqlite3.Connection, account_id: str, type_: str, since_state: str, max_changes: int | None = None
) -> ChangesResult:
    """Compute the RFC 8620 `*/changes` result for `type_` since `since_state`.

    Coalesces per RFC 8620 section 5.2: an object created and destroyed within
    the same window is reported in neither list; a created-then-updated object
    is reported only as created; a pre-existing, updated-then-destroyed object
    is reported only as destroyed.
    """
    since = _parse_state(since_state)
    latest = _parse_state(current_state(conn, account_id, type_))
    if since > latest:
        raise CannotCalculateChangesError(f"state {since_state!r} is ahead of the current state")

    rows = conn.execute(
        "SELECT seq, object_id, change FROM change_log WHERE account_id = ? AND type = ? AND seq > ? ORDER BY seq",
        (account_id, type_, since),
    ).fetchall()

    has_more = False
    if max_changes is not None:
        distinct_order: list[str] = []
        seen: set[str] = set()
        for row in rows:
            object_id = str(row["object_id"])
            if object_id not in seen:
                seen.add(object_id)
                distinct_order.append(object_id)
        if len(distinct_order) > max_changes:
            cutoff_object_id = distinct_order[max_changes]
            cutoff_index = next(i for i, row in enumerate(rows) if str(row["object_id"]) == cutoff_object_id)
            rows = rows[:cutoff_index]
            has_more = True

    new_state = str(rows[-1]["seq"]) if rows else since_state
    created, updated, destroyed = _coalesce(rows)
    return ChangesResult(
        old_state=since_state,
        new_state=new_state,
        has_more_changes=has_more,
        created=created,
        updated=updated,
        destroyed=destroyed,
    )
