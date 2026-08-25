from __future__ import annotations

from jmap_eas.store import db, submissions


def test_insert_and_get_submission(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    with database.transaction() as conn:
        submissions.insert_submission(conn, "alice", "s1", "e1", "identity", "client1")
    stored = submissions.get_submission(database.conn, "alice", "s1")
    assert stored.email_id == "e1"
    assert stored.identity_id == "identity"
    assert stored.client_id == "client1"
    assert stored.status == submissions.STATUS_PENDING


def test_get_submission_missing_returns_none(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    assert submissions.get_submission(database.conn, "alice", "missing") is None


def test_set_status(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    with database.transaction() as conn:
        submissions.insert_submission(conn, "alice", "s1", "e1", "identity", "client1")
        submissions.set_status(conn, "alice", "s1", submissions.STATUS_FINAL)
    assert submissions.get_submission(database.conn, "alice", "s1").status == submissions.STATUS_FINAL


def test_list_submissions_scoped_by_account(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    with database.transaction() as conn:
        submissions.insert_submission(conn, "alice", "s1", "e1", "identity", "c1")
        submissions.insert_submission(conn, "alice", "s2", "e2", "identity", "c2")
        submissions.insert_submission(conn, "bob", "s3", "e3", "identity", "c3")
    assert {s.submission_id for s in submissions.list_submissions(database.conn, "alice")} == {"s1", "s2"}


def test_delete_submission(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    with database.transaction() as conn:
        submissions.insert_submission(conn, "alice", "s1", "e1", "identity", "c1")
        submissions.delete_submission(conn, "alice", "s1")
    assert submissions.get_submission(database.conn, "alice", "s1") is None
