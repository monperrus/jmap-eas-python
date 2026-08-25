"""Live test of the M2 mutation surface, entirely inside throwaway test data (opt-in, `conftest.py`).

Every mutation here targets only a folder or email this test session created
itself -- `live_test_mailbox`/`live_test_draft` -- never a pre-existing one.
Cleanup is guaranteed by those fixtures' teardown even on failure, and
`_verify_no_leaked_mailboxes` (session-scoped, autouse) catches anything
that still escapes.
"""
from __future__ import annotations


def test_mailbox_set_create_rename_destroy(live_call):
    created = live_call("Mailbox/set", {"create": {"c1": {"name": "jmap-eas-test-rename-check"}}})
    mailbox_id = created["created"]["c1"]["id"]
    try:
        renamed = live_call("Mailbox/set", {"update": {mailbox_id: {"name": "jmap-eas-test-renamed"}}})
        assert renamed["updated"] == {mailbox_id: None}
        fetched = live_call("Mailbox/get", {"ids": [mailbox_id], "properties": ["name"]})
        assert fetched["list"][0]["name"] == "jmap-eas-test-renamed"
    finally:
        destroyed = live_call("Mailbox/set", {"destroy": [mailbox_id]})
        assert destroyed["destroyed"] == [mailbox_id]


def test_email_set_keyword_updates(live_test_draft, live_call):
    flagged = live_call("Email/set", {"update": {live_test_draft: {"keywords/$flagged": True}}})
    assert flagged["updated"] == {live_test_draft: None}
    fetched = live_call("Email/get", {"ids": [live_test_draft], "properties": ["keywords"]})
    assert fetched["list"][0]["keywords"].get("$flagged") is True

    unflagged = live_call("Email/set", {"update": {live_test_draft: {"keywords/$flagged": False}}})
    assert unflagged["updated"] == {live_test_draft: None}
    fetched2 = live_call("Email/get", {"ids": [live_test_draft], "properties": ["keywords"]})
    assert fetched2["list"][0]["keywords"].get("$flagged") is not True


def test_email_set_rejects_unsupported_keyword(live_test_draft, live_call):
    result = live_call("Email/set", {"update": {live_test_draft: {"keywords": {"$important": True}}}})
    assert result["notUpdated"][live_test_draft]["type"] == "invalidProperties"


def test_email_set_move(live_test_draft, live_test_mailbox, live_call):
    result = live_call("Email/set", {"update": {live_test_draft: {"mailboxIds": {live_test_mailbox: True}}}})
    assert result["updated"] == {live_test_draft: None}
    fetched = live_call("Email/get", {"ids": [live_test_draft], "properties": ["mailboxIds"]})
    assert fetched["list"][0]["mailboxIds"] == {live_test_mailbox: True}


def test_email_set_destroy(live_call, live_drafts_mailbox_id):
    created = live_call("Email/set", {"create": {"d1": {
        "mailboxIds": {live_drafts_mailbox_id: True},
        "keywords": {"$draft": True},
        "subject": "[jmap-eas-test] destroy check - safe to delete",
    }}})
    email_id = created["created"]["d1"]["id"]
    result = live_call("Email/set", {"destroy": [email_id]})
    assert result["destroyed"] == [email_id]
    fetched = live_call("Email/get", {"ids": [email_id], "properties": ["id"]})
    assert fetched["notFound"] == [email_id]
