from __future__ import annotations

from jmap_eas import policy


def test_methods_cover_the_m1_read_surface():
    assert set(policy.METHODS) == {
        "Core/echo", "Mailbox/get", "Mailbox/query", "Mailbox/changes",
        "Email/get", "Email/query", "Email/changes", "Thread/get",
    }


def test_no_mutating_methods_registered_yet():
    assert not any(name.endswith("/set") for name in policy.METHODS)
    assert not any(name.startswith("EmailSubmission") for name in policy.METHODS)


def test_capabilities_include_core_and_mail():
    assert policy.CORE_CAPABILITY in policy.CAPABILITIES
    assert policy.MAIL_CAPABILITY in policy.CAPABILITIES
    assert policy.CAPABILITIES[policy.MAIL_CAPABILITY]["maxMailboxesPerEmail"] == 1
