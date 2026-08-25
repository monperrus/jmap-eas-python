"""Live test of the M1 read path through the full ASGI app (opt-in, `conftest.py`).

Read-only: no fixture here creates or mutates anything, so there's nothing
for `_verify_no_leaked_mailboxes` to catch.
"""
from __future__ import annotations

import pytest


def test_session_resource(live_client, live_auth_headers):
    response = live_client.get("/.well-known/jmap", headers=live_auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert "live" in body["accounts"]
    assert body["accounts"]["live"]["isReadOnly"] is False
    assert body["apiUrl"].endswith("/api")


def test_mailbox_get_includes_inbox_with_a_role(live_call):
    result = live_call("Mailbox/get", {})
    roles = {m["role"] for m in result["list"]}
    assert "inbox" in roles
    assert result["notFound"] == []


def test_mailbox_query_filters_by_role(live_call):
    result = live_call("Mailbox/query", {"filter": {"role": "inbox"}})
    assert len(result["ids"]) == 1
    assert result["canCalculateChanges"] is True


def _inbox_id(live_call) -> str:
    return next(m for m in live_call("Mailbox/get", {})["list"] if m["role"] == "inbox")["id"]


def test_email_query_and_get_inbox(live_call):
    inbox_id = _inbox_id(live_call)
    query = live_call("Email/query", {"filter": {"inMailbox": inbox_id}, "limit": 3})
    if not query["ids"]:
        pytest.skip("inbox has no messages to read")
    result = live_call("Email/get", {
        "ids": query["ids"], "properties": ["subject", "from", "receivedAt", "preview", "size", "threadId"],
    })
    assert result["notFound"] == []
    for email in result["list"]:
        assert isinstance(email["size"], int)
        assert email["threadId"]


def test_thread_get_matches_a_real_email(live_call):
    inbox_id = _inbox_id(live_call)
    query = live_call("Email/query", {"filter": {"inMailbox": inbox_id}, "limit": 1})
    if not query["ids"]:
        pytest.skip("inbox has no messages to read")
    email = live_call("Email/get", {"ids": query["ids"], "properties": ["threadId"]})["list"][0]
    thread = live_call("Thread/get", {"ids": [email["threadId"]]})
    assert thread["list"][0]["id"] == email["threadId"]
    assert query["ids"][0] in thread["list"][0]["emailIds"]


def test_email_changes_from_zero(live_call):
    """Nothing has been pruned in a fresh test-run database, so `sinceState: "0"` (the full
    history) must succeed against real accumulated sync data, not just the fake-backed unit tests."""
    result = live_call("Email/changes", {"sinceState": "0"})
    assert "newState" in result
    assert isinstance(result["created"], list)
