from __future__ import annotations

import pytest

from jmap_eas.errors import InvalidArgumentsError
from jmap_eas.jmap.filtering import compute_query_changes, evaluate_filter


def _match(condition, record):
    return condition.get("v") == record


def test_evaluate_filter_none_matches_everything():
    assert evaluate_filter(None, "x", _match) is True


def test_evaluate_filter_leaf_condition():
    assert evaluate_filter({"v": "x"}, "x", _match) is True
    assert evaluate_filter({"v": "y"}, "x", _match) is False


def test_evaluate_filter_and():
    filter_ = {"operator": "AND", "conditions": [{"v": "x"}, {"v": "x"}]}
    assert evaluate_filter(filter_, "x", _match) is True
    filter_2 = {"operator": "AND", "conditions": [{"v": "x"}, {"v": "y"}]}
    assert evaluate_filter(filter_2, "x", _match) is False


def test_evaluate_filter_or():
    filter_ = {"operator": "OR", "conditions": [{"v": "y"}, {"v": "x"}]}
    assert evaluate_filter(filter_, "x", _match) is True


def test_evaluate_filter_not():
    filter_ = {"operator": "NOT", "conditions": [{"v": "y"}]}
    assert evaluate_filter(filter_, "x", _match) is True
    filter_2 = {"operator": "NOT", "conditions": [{"v": "x"}]}
    assert evaluate_filter(filter_2, "x", _match) is False


def test_evaluate_filter_nested_operators():
    filter_ = {"operator": "AND", "conditions": [
        {"operator": "OR", "conditions": [{"v": "x"}, {"v": "y"}]},
        {"operator": "NOT", "conditions": [{"v": "z"}]},
    ]}
    assert evaluate_filter(filter_, "x", _match) is True


def test_evaluate_filter_unknown_operator_raises():
    with pytest.raises(InvalidArgumentsError):
        evaluate_filter({"operator": "XOR", "conditions": []}, "x", _match)


def test_evaluate_filter_non_list_conditions_raises():
    with pytest.raises(InvalidArgumentsError):
        evaluate_filter({"operator": "AND", "conditions": "not-a-list"}, "x", _match)


# -- compute_query_changes ------------------------------------------------------------


def test_compute_query_changes_destroyed_is_removed_only():
    removed, added = compute_query_changes([], [], ["e1"], ["e2", "e3"])
    assert removed == ["e1"]
    assert added == []


def test_compute_query_changes_created_and_matching_is_added():
    removed, added = compute_query_changes(["e1"], [], [], ["e1", "e2"])
    assert removed == ["e1"]  # conservative: also reported removed, harmless if never shown
    assert added == [{"id": "e1", "index": 0}]


def test_compute_query_changes_created_and_not_matching_is_removed_only():
    removed, added = compute_query_changes(["e1"], [], [], ["e2", "e3"])
    assert removed == ["e1"]
    assert added == []


def test_compute_query_changes_updated_and_still_matching_is_removed_then_added():
    removed, added = compute_query_changes([], ["e1"], [], ["e2", "e1"])
    assert removed == ["e1"]
    assert added == [{"id": "e1", "index": 1}]


def test_compute_query_changes_added_sorted_by_index():
    removed, added = compute_query_changes(["e1", "e2"], [], [], ["e2", "e1"])
    assert added == [{"id": "e2", "index": 0}, {"id": "e1", "index": 1}]


def test_compute_query_changes_removed_is_sorted_and_deduplicated():
    removed, added = compute_query_changes(["e1"], ["e1"], ["e1"], [])
    assert removed == ["e1"]
