"""Live test of the M3 send surface (opt-in, `conftest.py`).

`live_test_draft` always addresses its message to the account's own
configured address, never a third party -- sending is the one live
operation here that can't be "cleaned up" afterward the way a folder or
draft can, so it must never reach anyone but the test account itself.
"""
from __future__ import annotations


def test_identity_get_matches_configured_address(live_identity):
    assert live_identity["email"]
    assert live_identity["mayDelete"] is False


def test_email_submission_set_sends_and_records(live_call, live_test_draft, live_identity):
    result = live_call("EmailSubmission/set", {"create": {"s1": {
        "identityId": live_identity["id"], "emailId": live_test_draft,
    }}})
    assert result["notCreated"] == {}
    submission_id = result["created"]["s1"]["id"]
    assert result["created"]["s1"]["undoStatus"] == "final"

    fetched = live_call("EmailSubmission/get", {"ids": [submission_id]})
    assert fetched["list"][0]["undoStatus"] == "final"
    assert fetched["list"][0]["emailId"] == live_test_draft
    assert fetched["list"][0]["identityId"] == live_identity["id"]

    # Local-only bookkeeping cleanup; this never un-sends the message.
    destroyed = live_call("EmailSubmission/set", {"destroy": [submission_id]})
    assert destroyed["destroyed"] == [submission_id]


def test_email_submission_set_rejects_unknown_identity(live_call, live_test_draft):
    result = live_call("EmailSubmission/set", {"create": {"s1": {
        "identityId": "not-a-real-identity", "emailId": live_test_draft,
    }}})
    assert result["notCreated"]["s1"]["type"] == "invalidProperties"
