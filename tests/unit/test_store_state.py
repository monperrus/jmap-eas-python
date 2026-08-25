from __future__ import annotations

import pytest

from jmap_eas.store import db, state


def _db(tmp_path):
    return db.connect(tmp_path / "bridge.sqlite3")


def test_current_state_is_zero_with_no_history(tmp_path):
    database = _db(tmp_path)
    assert state.current_state(database.conn, "alice", "Email") == "0"


def test_append_change_advances_state(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        seq = state.append_change(conn, "alice", "Email", "e1", "created")
    assert seq == 1
    assert state.current_state(database.conn, "alice", "Email") == "1"


def test_append_change_rejects_invalid_kind(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn, pytest.raises(ValueError):
        state.append_change(conn, "alice", "Email", "e1", "renamed")


def test_state_is_per_type(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        state.append_change(conn, "alice", "Email", "e1", "created")
        state.append_change(conn, "alice", "Mailbox", "m1", "created")
    assert state.current_state(database.conn, "alice", "Email") == "1"
    assert state.current_state(database.conn, "alice", "Mailbox") == "2"


def test_get_changes_from_zero_reports_created(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        state.append_change(conn, "alice", "Email", "e1", "created")
        state.append_change(conn, "alice", "Email", "e2", "created")
    result = state.get_changes(database.conn, "alice", "Email", "0")
    assert set(result.created) == {"e1", "e2"}
    assert result.updated == []
    assert result.destroyed == []
    assert result.new_state == "2"
    assert result.has_more_changes is False


def test_get_changes_created_then_updated_reports_created_only(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        state.append_change(conn, "alice", "Email", "e1", "created")
        state.append_change(conn, "alice", "Email", "e1", "updated")
    result = state.get_changes(database.conn, "alice", "Email", "0")
    assert result.created == ["e1"]
    assert result.updated == []


def test_get_changes_created_then_destroyed_reports_neither(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        state.append_change(conn, "alice", "Email", "e1", "created")
        state.append_change(conn, "alice", "Email", "e1", "destroyed")
    result = state.get_changes(database.conn, "alice", "Email", "0")
    assert result.created == []
    assert result.updated == []
    assert result.destroyed == []
    assert result.new_state == "2"


def test_get_changes_preexisting_object_updated_then_destroyed_reports_destroyed_only(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        state.append_change(conn, "alice", "Email", "e1", "created")
    baseline = state.current_state(database.conn, "alice", "Email")
    with database.transaction() as conn:
        state.append_change(conn, "alice", "Email", "e1", "updated")
        state.append_change(conn, "alice", "Email", "e1", "destroyed")
    result = state.get_changes(database.conn, "alice", "Email", baseline)
    assert result.destroyed == ["e1"]
    assert result.updated == []


def test_get_changes_preexisting_object_updated_twice_reports_once(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        state.append_change(conn, "alice", "Email", "e1", "created")
    baseline = state.current_state(database.conn, "alice", "Email")
    with database.transaction() as conn:
        state.append_change(conn, "alice", "Email", "e1", "updated")
        state.append_change(conn, "alice", "Email", "e1", "updated")
    result = state.get_changes(database.conn, "alice", "Email", baseline)
    assert result.updated == ["e1"]


def test_get_changes_rejects_state_ahead_of_current(tmp_path):
    database = _db(tmp_path)
    with pytest.raises(state.CannotCalculateChangesError):
        state.get_changes(database.conn, "alice", "Email", "5")


def test_get_changes_rejects_non_integer_state(tmp_path):
    database = _db(tmp_path)
    with pytest.raises(state.CannotCalculateChangesError):
        state.get_changes(database.conn, "alice", "Email", "not-a-number")


def test_get_changes_respects_max_changes_and_reports_has_more(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        for i in range(5):
            state.append_change(conn, "alice", "Email", f"e{i}", "created")
    result = state.get_changes(database.conn, "alice", "Email", "0", max_changes=2)
    assert result.created == ["e0", "e1"]
    assert result.has_more_changes is True
    assert result.new_state == "2"

    next_result = state.get_changes(database.conn, "alice", "Email", result.new_state, max_changes=2)
    assert next_result.created == ["e2", "e3"]
    assert next_result.has_more_changes is True

    final_result = state.get_changes(database.conn, "alice", "Email", next_result.new_state, max_changes=2)
    assert final_result.created == ["e4"]
    assert final_result.has_more_changes is False


def test_get_changes_max_changes_keeps_all_rows_for_included_objects(tmp_path):
    """An included object's later mutation, interleaved after the cutoff object first appears,
    must still be picked up by the *next* call rather than silently lost."""
    database = _db(tmp_path)
    with database.transaction() as conn:
        state.append_change(conn, "alice", "Email", "e1", "created")
    baseline = state.current_state(database.conn, "alice", "Email")
    with database.transaction() as conn:
        state.append_change(conn, "alice", "Email", "e2", "created")  # will be excluded (max_changes=1)
        state.append_change(conn, "alice", "Email", "e1", "updated")  # interleaved after the cutoff

    result = state.get_changes(database.conn, "alice", "Email", baseline, max_changes=1)
    assert result.created == ["e2"]
    assert result.has_more_changes is True

    next_result = state.get_changes(database.conn, "alice", "Email", result.new_state, max_changes=1)
    assert next_result.updated == ["e1"]


# -- prune_change_log ------------------------------------------------------------------------


def test_prune_change_log_no_op_below_keep_threshold(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        for i in range(5):
            state.append_change(conn, "alice", "Email", f"e{i}", "created")
        state.prune_change_log(conn, "alice", keep=100)
    # Nothing pruned: sinceState="0" still works.
    result = state.get_changes(database.conn, "alice", "Email", "0")
    assert len(result.created) == 5


def test_prune_change_log_deletes_old_rows_and_records_watermark(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        for i in range(10):
            state.append_change(conn, "alice", "Email", f"e{i}", "created")
        state.prune_change_log(conn, "alice", keep=3)

    rows = database.execute("SELECT COUNT(*) FROM change_log WHERE account_id = 'alice'").fetchone()[0]
    assert rows == 3

    # A sinceState older than the prune point can no longer be answered correctly.
    with pytest.raises(state.CannotCalculateChangesError):
        state.get_changes(database.conn, "alice", "Email", "0")

    # But the retained tail still works.
    result = state.get_changes(database.conn, "alice", "Email", "7")
    assert result.created == ["e7", "e8", "e9"]


def test_prune_change_log_is_per_account(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        for i in range(10):
            state.append_change(conn, "alice", "Email", f"e{i}", "created")
            state.append_change(conn, "bob", "Email", f"e{i}", "created")
        state.prune_change_log(conn, "alice", keep=3)

    # bob's history is untouched.
    result = state.get_changes(database.conn, "bob", "Email", "0")
    assert len(result.created) == 10


def test_prune_change_log_watermark_persists_across_calls(tmp_path):
    database = _db(tmp_path)
    with database.transaction() as conn:
        for i in range(10):
            state.append_change(conn, "alice", "Email", f"e{i}", "created")
        state.prune_change_log(conn, "alice", keep=5)
        state.prune_change_log(conn, "alice", keep=100)  # a no-op this time, watermark must not move backward
    with pytest.raises(state.CannotCalculateChangesError):
        state.get_changes(database.conn, "alice", "Email", "0")
