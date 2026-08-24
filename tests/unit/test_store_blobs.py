from __future__ import annotations

from jmap_eas.store import blobs, db


def test_insert_and_get_blob(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    with database.transaction() as conn:
        blobs.insert_blob(conn, "alice", "b1", "text/plain", b"hello")
    stored = blobs.get_blob(database.conn, "alice", "b1")
    assert stored.content_type == "text/plain"
    assert stored.data == b"hello"
    assert stored.size == 5


def test_get_blob_missing_returns_none(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    assert blobs.get_blob(database.conn, "alice", "missing") is None


def test_get_blob_scoped_by_account(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    with database.transaction() as conn:
        blobs.insert_blob(conn, "alice", "b1", "text/plain", b"hello")
    assert blobs.get_blob(database.conn, "bob", "b1") is None
