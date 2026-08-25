"""Generic JMAP `FilterOperator`/`FilterCondition` evaluation (RFC 8620 section 5.5)
and the conservative `*/queryChanges` diff (RFC 8620 section 5.6).

`Mailbox/query` and `Email/query` each supply their own leaf condition
matcher; AND/OR/NOT combination is identical across every `*/query` method,
so it lives here once.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..errors import InvalidArgumentsError

ConditionMatcher = Callable[[dict[str, Any], Any], bool]


def compute_query_changes(
    created: list[str], updated: list[str], destroyed: list[str], current_ids: list[str]
) -> tuple[list[str], list[dict[str, Any]]]:
    """Conservative `*/queryChanges` diff over the plain change log, not sort-position tracking.

    An id is reported `removed` if it was destroyed, or if it was created/updated at all --
    whether or not it currently matches the query is irrelevant, since telling a client to
    remove an id it never had is harmless (RFC 8620 section 5.6). An id is reported `added`,
    at its current index, only if it was created/updated *and* still matches the query -- a
    created-then-still-matching id is naturally added, and an updated-and-still-matching id is
    conservatively removed-then-re-added so the client repositions it without us having to
    calculate whether its sort position actually changed.
    """
    index_of = {object_id: index for index, object_id in enumerate(current_ids)}
    removed = set(destroyed) | set(created) | set(updated)
    added: list[dict[str, Any]] = [
        {"id": object_id, "index": index_of[object_id]}
        for object_id in list(created) + list(updated)
        if object_id in index_of
    ]
    added.sort(key=lambda item: int(item["index"]))
    return sorted(removed), added


def evaluate_filter(filter_: dict[str, Any] | None, record: Any, match_condition: ConditionMatcher) -> bool:
    if filter_ is None:
        return True
    if "operator" in filter_:
        operator = filter_["operator"]
        conditions = filter_.get("conditions", [])
        if not isinstance(conditions, list):
            raise InvalidArgumentsError("filter 'conditions' must be an array")
        if operator == "AND":
            return all(evaluate_filter(c, record, match_condition) for c in conditions)
        if operator == "OR":
            return any(evaluate_filter(c, record, match_condition) for c in conditions)
        if operator == "NOT":
            return not any(evaluate_filter(c, record, match_condition) for c in conditions)
        raise InvalidArgumentsError(f"unknown filter operator: {operator!r}")
    return match_condition(filter_, record)
