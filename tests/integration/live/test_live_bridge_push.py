"""Live test of the M4 push/queryChanges surface (opt-in, `conftest.py`).

`GET /eventsource` is only exercised with `closeafter=state`: a real Ping
long poll (heartbeat >= 60s server-side minimum, confirmed live against
webmail.kth.se) would make the rest of the suite unacceptably slow, and the
Ping-then-sync cycle itself is already covered by `jmap/eventsource.py`'s
unit tests against a fake Ping client.
"""
from __future__ import annotations


def test_eventsource_closeafter_state(live_client, live_auth_headers):
    response = live_client.get("/eventsource?closeafter=state", headers=live_auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: state" in response.text
    assert '"live"' in response.text


def test_mailbox_query_changes_reports_a_created_and_destroyed_mailbox(live_call):
    before = live_call("Mailbox/query", {})
    since = before["queryState"]

    created = live_call("Mailbox/set", {"create": {"c1": {"name": "jmap-eas-test-querychanges"}}})
    mailbox_id = created["created"]["c1"]["id"]
    try:
        changes = live_call("Mailbox/queryChanges", {"sinceQueryState": since})
        assert any(added["id"] == mailbox_id for added in changes["added"])
        since_after_create = changes["newQueryState"]
    finally:
        live_call("Mailbox/set", {"destroy": [mailbox_id]})

    changes_after_destroy = live_call("Mailbox/queryChanges", {"sinceQueryState": since_after_create})
    assert mailbox_id in changes_after_destroy["removed"]

    # Created and destroyed within the same window relative to the *original* `since`: per RFC 8620 S5.2
    # this coalesces to no report at all, not a "removed" entry -- from that vantage point it never existed.
    changes_from_original_since = live_call("Mailbox/queryChanges", {"sinceQueryState": since})
    assert mailbox_id not in changes_from_original_since["removed"]
    assert mailbox_id not in [added["id"] for added in changes_from_original_since["added"]]


def test_email_query_changes_reports_a_move_into_the_filtered_mailbox(live_test_draft, live_test_mailbox, live_call):
    before = live_call("Email/query", {"filter": {"inMailbox": live_test_mailbox}})
    since = before["queryState"]

    live_call("Email/set", {"update": {live_test_draft: {"mailboxIds": {live_test_mailbox: True}}}})

    changes = live_call("Email/queryChanges", {
        "sinceQueryState": since, "filter": {"inMailbox": live_test_mailbox},
    })
    assert any(added["id"] == live_test_draft for added in changes["added"])
