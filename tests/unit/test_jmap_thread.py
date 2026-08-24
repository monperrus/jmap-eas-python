from __future__ import annotations

from jmap_eas.backend.sync import SyncCoordinator
from jmap_eas.config import PolicyConfig
from jmap_eas.jmap import thread
from jmap_eas.jmap.dispatcher import Environment
from jmap_eas.models import EmailRecord
from jmap_eas.store import cache, db


def _env(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    env = Environment(
        account_id="alice", database=database, sync=SyncCoordinator(database), adapter=None, policy=PolicyConfig()
    )
    return env, database


def test_get_groups_emails_by_thread(tmp_path):
    env, database = _env(tmp_path)
    with database.transaction() as conn:
        cache.upsert_email(
            conn, EmailRecord("alice", "e1", "1", "9:1", "t1", "A", received_at="2026-01-01T00:00:00Z")
        )
        cache.upsert_email(
            conn, EmailRecord("alice", "e2", "1", "9:2", "t1", "Re: A", received_at="2026-01-02T00:00:00Z")
        )
        cache.upsert_email(conn, EmailRecord("alice", "e3", "1", "9:3", "t2", "B"))
    result = thread.get(env, {"ids": ["t1", "t2"]})
    by_id = {t["id"]: t["emailIds"] for t in result["list"]}
    assert by_id == {"t1": ["e1", "e2"], "t2": ["e3"]}
    assert result["notFound"] == []


def test_get_reports_not_found_thread_ids(tmp_path):
    env, database = _env(tmp_path)
    result = thread.get(env, {"ids": ["missing"]})
    assert result["list"] == []
    assert result["notFound"] == ["missing"]


def test_get_defaults_to_every_thread_when_ids_is_none(tmp_path):
    env, database = _env(tmp_path)
    with database.transaction() as conn:
        cache.upsert_email(conn, EmailRecord("alice", "e1", "1", "9:1", "t1", "A"))
        cache.upsert_email(conn, EmailRecord("alice", "e2", "1", "9:2", "t2", "B"))
    result = thread.get(env, {})
    assert {t["id"] for t in result["list"]} == {"t1", "t2"}
