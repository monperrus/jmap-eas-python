"""Shared fixtures for live tests against a real EAS server (plan.md section 8).

Scoped to this `live/` subdirectory only -- its `_verify_no_leaked_mailboxes`
autouse fixture must never run for the fake-backed tests in the parent
`tests/integration/` directory, which don't touch a real server at all.

Every live bridge test goes through fixtures that (1) skip cleanly when
credentials aren't configured -- the exact env vars documented in the
README -- (2) touch only test data they create themselves, and (3) clean
that data up even when the test fails, backstopped by a session-wide
mailbox-leak check. This is what M2's live verification learned the hard
way: a killed process once left an orphaned test folder behind, caught only
by a duplicate-name failure on the next run. `live_test_mailbox` below
always tears down in a `finally`, and `_verify_no_leaked_mailboxes` fails
the whole session loudly if anything still escapes that.

Nothing here ever calls `Mailbox/set` destroy or `Email/set` destroy against
an id it didn't itself create in this session.
"""
from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable, Iterator

import pytest
from starlette.testclient import TestClient

from jmap_eas.app import create_app
from jmap_eas.config import AccountConfig, AppConfig, ServerConfig

LIVE_ACCOUNT_ID = "live"
LIVE_API_TOKEN = "live-test-token"
LIVE_ENV_VARS = ("JMAP_EAS_LIVE_SERVER", "JMAP_EAS_LIVE_USER", "JMAP_EAS_LIVE_PASSWORD", "JMAP_EAS_LIVE_DEVICE_ID")
USING = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail", "urn:ietf:params:jmap:submission"]

LiveCall = Callable[..., dict]


def _missing_live_env_vars() -> list[str]:
    return [v for v in LIVE_ENV_VARS if not os.environ.get(v)]


@pytest.fixture(scope="session")
def live_account_config() -> AccountConfig:
    missing = _missing_live_env_vars()
    if missing:
        pytest.skip(f"live EAS server not configured: {', '.join(missing)} not set")
    return AccountConfig(
        eas_server=os.environ["JMAP_EAS_LIVE_SERVER"],
        username=os.environ["JMAP_EAS_LIVE_USER"],
        password=os.environ["JMAP_EAS_LIVE_PASSWORD"],
        device_id=os.environ["JMAP_EAS_LIVE_DEVICE_ID"],
        user=os.environ.get("JMAP_EAS_LIVE_SMTP_USER"),
        api_token=LIVE_API_TOKEN,
    )


@pytest.fixture(scope="session")
def live_client(tmp_path_factory, live_account_config: AccountConfig) -> Iterator[TestClient]:
    db_path = tmp_path_factory.mktemp("jmap-eas-live") / "bridge.sqlite3"
    config = AppConfig(
        server=ServerConfig(db_path=str(db_path)), accounts={LIVE_ACCOUNT_ID: live_account_config},
    )
    app = create_app(config)
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def live_auth_headers() -> dict[str, str]:
    raw = f"{LIVE_ACCOUNT_ID}:{LIVE_API_TOKEN}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@pytest.fixture(scope="session")
def live_call(live_client: TestClient, live_auth_headers: dict[str, str]) -> LiveCall:
    """`live_call("Email/set", {"destroy": [...]})` -- binds accountId, asserts no method error."""

    def _do(method: str, args: dict, call_id: str = "c0") -> dict:
        response = live_client.post("/api", headers=live_auth_headers, json={
            "using": USING,
            "methodCalls": [[method, {"accountId": LIVE_ACCOUNT_ID, **args}, call_id]],
        })
        assert response.status_code == 200, response.text
        name, result, _ = response.json()["methodResponses"][0]
        assert name != "error", f"{method} failed: {result}"
        return result

    return _do


@pytest.fixture(scope="session")
def _preexisting_mailbox_ids(live_call: LiveCall) -> set[str]:
    return {m["id"] for m in live_call("Mailbox/get", {})["list"]}


@pytest.fixture(autouse=True, scope="session")
def _verify_no_leaked_mailboxes(live_call: LiveCall, _preexisting_mailbox_ids: set[str]) -> Iterator[None]:
    """Session-wide safety net: fails loudly if any live test leaves a mailbox behind."""
    yield
    current_ids = {m["id"] for m in live_call("Mailbox/get", {})["list"]}
    leaked = current_ids - _preexisting_mailbox_ids
    assert not leaked, f"live test run leaked mailboxes that were never cleaned up: {leaked}"


@pytest.fixture
def live_test_mailbox(live_call: LiveCall) -> Iterator[str]:
    """One freshly created, uniquely named test folder.

    Always destroyed on teardown, even if the test raises, and even if it
    still contains emails a test forgot to clean up itself.
    """
    name = f"jmap-eas-test-{int(time.time() * 1000)}"
    created = live_call("Mailbox/set", {"create": {"c1": {"name": name}}})
    mailbox_id = created["created"]["c1"]["id"]
    try:
        yield mailbox_id
    finally:
        leftover = live_call("Email/query", {"filter": {"inMailbox": mailbox_id}})
        if leftover["ids"]:
            live_call("Email/set", {"destroy": leftover["ids"]})
        live_call("Mailbox/set", {"destroy": [mailbox_id]})


@pytest.fixture(scope="session")
def live_drafts_mailbox_id(live_call: LiveCall) -> str:
    drafts = next(m for m in live_call("Mailbox/get", {})["list"] if m["role"] == "drafts")
    return drafts["id"]


@pytest.fixture(scope="session")
def live_identity(live_call: LiveCall) -> dict:
    return live_call("Identity/get", {})["list"][0]


@pytest.fixture
def live_test_draft(live_call: LiveCall, live_drafts_mailbox_id: str, live_identity: dict) -> Iterator[str]:
    """One draft email in the real Drafts folder, addressed to the account's own address (never a
    third party) so it's also usable by send tests. Clearly labeled; always destroyed on teardown."""
    subject = f"[jmap-eas-test] safe to delete {int(time.time() * 1000)}"
    created = live_call("Email/set", {"create": {"d1": {
        "mailboxIds": {live_drafts_mailbox_id: True},
        "keywords": {"$draft": True},
        "subject": subject,
        "to": [{"email": live_identity["email"]}],
        "bodyValues": {"b": {"value": "Automated jmap-eas-python live test draft. Safe to delete."}},
        "textBody": [{"partId": "b", "type": "text/plain"}],
    }}})
    email_id = created["created"]["d1"]["id"]
    try:
        yield email_id
    finally:
        # It may have already moved (a move test) or been destroyed (a destroy test); Email/set
        # destroy on an id that's no longer in the mailbox it started in still works since the
        # local email_id is stable across a move (plan.md section 3) -- but if a test already
        # destroyed it, destroying it again would fail, so check first.
        current = live_call("Email/get", {"ids": [email_id], "properties": ["id"]})
        if not current["notFound"]:
            live_call("Email/set", {"destroy": [email_id]})
