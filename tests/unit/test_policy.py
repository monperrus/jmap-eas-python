from __future__ import annotations

from jmap_eas import policy


def test_methods_cover_the_m1_and_m2_surface():
    assert set(policy.METHODS) == {
        "Core/echo", "Mailbox/get", "Mailbox/query", "Mailbox/changes", "Mailbox/set",
        "Email/get", "Email/query", "Email/changes", "Email/set", "Thread/get",
    }


def test_no_submission_methods_registered_yet():
    assert not any(name.startswith("EmailSubmission") for name in policy.METHODS)


def test_capabilities_include_core_and_mail():
    assert policy.CORE_CAPABILITY in policy.CAPABILITIES
    assert policy.MAIL_CAPABILITY in policy.CAPABILITIES
    assert policy.CAPABILITIES[policy.MAIL_CAPABILITY]["maxMailboxesPerEmail"] == 1
