from __future__ import annotations

import base64

import pytest

from jmap_eas.auth import AuthenticationError, authenticate
from jmap_eas.config import AccountConfig


def _accounts() -> dict[str, AccountConfig]:
    return {
        "alice": AccountConfig(
            eas_server="https://mail.example.com/Microsoft-Server-ActiveSync",
            username="alice@example.com",
            device_id="dev1",
            password="eas-secret",
            api_token="bridge-token",
        )
    }


def _basic(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def test_authenticate_valid_credentials():
    assert authenticate(_basic("alice", "bridge-token"), _accounts()) == "alice"


def test_authenticate_missing_header():
    with pytest.raises(AuthenticationError):
        authenticate(None, _accounts())


def test_authenticate_non_basic_scheme():
    with pytest.raises(AuthenticationError):
        authenticate("Bearer sometoken", _accounts())


def test_authenticate_malformed_base64():
    with pytest.raises(AuthenticationError):
        authenticate("Basic not-valid-base64!!!", _accounts())


def test_authenticate_missing_colon():
    header = "Basic " + base64.b64encode(b"no-colon-here").decode()
    with pytest.raises(AuthenticationError):
        authenticate(header, _accounts())


def test_authenticate_unknown_account():
    with pytest.raises(AuthenticationError):
        authenticate(_basic("bob", "bridge-token"), _accounts())


def test_authenticate_wrong_token():
    with pytest.raises(AuthenticationError):
        authenticate(_basic("alice", "wrong-token"), _accounts())


def test_authenticate_does_not_accept_eas_password_as_token():
    with pytest.raises(AuthenticationError):
        authenticate(_basic("alice", "eas-secret"), _accounts())
