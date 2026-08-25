from __future__ import annotations

from jmap_eas import policy
from jmap_eas.config import PolicyConfig
from jmap_eas.jmap.session import build_session


def test_session_advertises_submission_when_send_allowed():
    session = build_session("alice", "http://localhost", PolicyConfig(allow_send=True))
    account_caps = session["accounts"]["alice"]["accountCapabilities"]
    assert policy.SUBMISSION_CAPABILITY in account_caps
    assert policy.SUBMISSION_CAPABILITY in session["primaryAccounts"]


def test_session_omits_submission_when_send_disallowed():
    session = build_session("alice", "http://localhost", PolicyConfig(allow_send=False))
    account_caps = session["accounts"]["alice"]["accountCapabilities"]
    assert policy.SUBMISSION_CAPABILITY not in account_caps
    assert policy.SUBMISSION_CAPABILITY not in session["primaryAccounts"]


def test_session_always_advertises_core_and_mail():
    session = build_session("alice", "http://localhost", PolicyConfig())
    account_caps = session["accounts"]["alice"]["accountCapabilities"]
    assert policy.CORE_CAPABILITY in account_caps
    assert policy.MAIL_CAPABILITY in account_caps


def test_session_is_not_read_only():
    session = build_session("alice", "http://localhost", PolicyConfig())
    assert session["accounts"]["alice"]["isReadOnly"] is False


def test_session_urls_and_username():
    session = build_session("alice", "http://localhost:8080", PolicyConfig())
    assert session["username"] == "alice"
    assert session["apiUrl"] == "http://localhost:8080/api"
    assert session["downloadUrl"].startswith("http://localhost:8080/download/")
    assert session["uploadUrl"] == "http://localhost:8080/upload/{accountId}"
