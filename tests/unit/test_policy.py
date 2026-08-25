from __future__ import annotations

from jmap_eas import policy


def test_methods_cover_the_m1_through_m4_surface():
    assert set(policy.METHODS) == {
        "Core/echo", "Mailbox/get", "Mailbox/query", "Mailbox/queryChanges", "Mailbox/changes", "Mailbox/set",
        "Email/get", "Email/query", "Email/queryChanges", "Email/changes", "Email/set", "Thread/get",
        "Identity/get", "EmailSubmission/get", "EmailSubmission/set", "EmailSubmission/changes",
    }


def test_capabilities_include_core_mail_and_submission():
    assert policy.CORE_CAPABILITY in policy.CAPABILITIES
    assert policy.MAIL_CAPABILITY in policy.CAPABILITIES
    assert policy.SUBMISSION_CAPABILITY in policy.CAPABILITIES
    assert policy.CAPABILITIES[policy.MAIL_CAPABILITY]["maxMailboxesPerEmail"] == 1
    assert policy.CAPABILITIES[policy.SUBMISSION_CAPABILITY]["maxDelayedSend"] == 0
