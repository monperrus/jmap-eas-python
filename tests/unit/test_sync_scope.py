from __future__ import annotations

from jmap_eas.jmap.filtering import extract_inmailbox_scope
from jmap_eas.jmap.sync_scope import compute_sync_scope


def test_identity_get_needs_no_sync():
    scope = compute_sync_scope([("Identity/get", {}, "c0")])
    assert scope.reconcile_folders is False
    assert scope.sync_all_folders is False
    assert scope.folder_ids == set()


def test_core_echo_needs_no_sync():
    scope = compute_sync_scope([("Core/echo", {}, "c0")])
    assert scope.reconcile_folders is False
    assert scope.sync_all_folders is False
    assert scope.folder_ids == set()


def test_email_get_needs_no_sync():
    scope = compute_sync_scope([("Email/get", {"ids": ["1"]}, "c0")])
    assert scope.reconcile_folders is False
    assert scope.sync_all_folders is False
    assert scope.folder_ids == set()


def test_email_submission_methods_need_no_sync():
    for name in ("EmailSubmission/get", "EmailSubmission/set", "EmailSubmission/changes"):
        scope = compute_sync_scope([(name, {}, "c0")])
        assert scope.reconcile_folders is False
        assert scope.sync_all_folders is False
        assert scope.folder_ids == set()


def test_mailbox_methods_need_folder_reconciliation_only():
    for name in ("Mailbox/get", "Mailbox/query", "Mailbox/queryChanges", "Mailbox/changes"):
        scope = compute_sync_scope([(name, {}, "c0")])
        assert scope.reconcile_folders is True
        assert scope.sync_all_folders is False
        assert scope.folder_ids == set()


def test_mailbox_set_needs_no_pre_sync():
    scope = compute_sync_scope([("Mailbox/set", {"create": {}}, "c0")])
    assert scope.reconcile_folders is False
    assert scope.folder_ids == set()


def test_email_set_needs_no_pre_sync():
    scope = compute_sync_scope([("Email/set", {"update": {}}, "c0")])
    assert scope.reconcile_folders is False
    assert scope.folder_ids == set()


def test_email_query_scoped_by_inmailbox_syncs_only_that_mailbox():
    scope = compute_sync_scope([("Email/query", {"filter": {"inMailbox": "42"}}, "c0")])
    assert scope.reconcile_folders is False
    assert scope.sync_all_folders is False
    assert scope.folder_ids == {"42"}


def test_email_query_changes_scoped_by_inmailbox_syncs_only_that_mailbox():
    scope = compute_sync_scope([("Email/queryChanges", {"filter": {"inMailbox": "42"}}, "c0")])
    assert scope.folder_ids == {"42"}
    assert scope.sync_all_folders is False


def test_email_query_without_filter_syncs_every_mailbox():
    scope = compute_sync_scope([("Email/query", {}, "c0")])
    assert scope.reconcile_folders is True
    assert scope.sync_all_folders is True
    assert scope.folder_ids == set()


def test_email_query_with_unscoped_filter_syncs_every_mailbox():
    scope = compute_sync_scope([("Email/query", {"filter": {"subject": "hi"}}, "c0")])
    assert scope.sync_all_folders is True


def test_email_query_and_scope_unions_inmailbox_conditions():
    filter_ = {"operator": "AND", "conditions": [{"inMailbox": "1"}, {"hasKeyword": "$seen"}]}
    scope = compute_sync_scope([("Email/query", {"filter": filter_}, "c0")])
    assert scope.folder_ids == {"1"}
    assert scope.sync_all_folders is False


def test_email_query_or_of_two_inmailbox_conditions_unions_both():
    filter_ = {"operator": "OR", "conditions": [{"inMailbox": "1"}, {"inMailbox": "2"}]}
    scope = compute_sync_scope([("Email/query", {"filter": filter_}, "c0")])
    assert scope.folder_ids == {"1", "2"}
    assert scope.sync_all_folders is False


def test_email_query_or_with_one_unbounded_branch_syncs_every_mailbox():
    filter_ = {"operator": "OR", "conditions": [{"inMailbox": "1"}, {"subject": "hi"}]}
    scope = compute_sync_scope([("Email/query", {"filter": filter_}, "c0")])
    assert scope.sync_all_folders is True


def test_email_query_not_is_never_bounded():
    filter_ = {"operator": "NOT", "conditions": [{"inMailbox": "1"}]}
    scope = compute_sync_scope([("Email/query", {"filter": filter_}, "c0")])
    assert scope.sync_all_folders is True


def test_batch_unions_scopes_across_calls():
    calls = [
        ("Mailbox/get", {}, "c0"),
        ("Email/query", {"filter": {"inMailbox": "1"}}, "c1"),
        ("Email/query", {"filter": {"inMailbox": "2"}}, "c2"),
        ("Identity/get", {}, "c3"),
    ]
    scope = compute_sync_scope(calls)
    assert scope.reconcile_folders is True
    assert scope.folder_ids == {"1", "2"}
    assert scope.sync_all_folders is False


def test_extract_inmailbox_scope_no_filter_is_unbounded():
    assert extract_inmailbox_scope(None) is None


def test_extract_inmailbox_scope_bare_condition():
    assert extract_inmailbox_scope({"inMailbox": "1"}) == {"1"}
    assert extract_inmailbox_scope({"subject": "hi"}) is None
