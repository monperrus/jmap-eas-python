"""Generic JMAP `FilterOperator`/`FilterCondition` evaluation (RFC 8620 section 5.5).

`Mailbox/query` and `Email/query` each supply their own leaf condition
matcher; AND/OR/NOT combination is identical across every `*/query` method,
so it lives here once.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..errors import InvalidArgumentsError

ConditionMatcher = Callable[[dict[str, Any], Any], bool]


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
