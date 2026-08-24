"""Live EAS test for the M0 adapter/registry. Opt-in via env vars, skipped otherwise.

Mirrors `pyactivesync`'s own live-test convention (plan.md section 8): the
suite skips instead of failing when credentials aren't configured, and only
exercises read-only commands, so it's safe to run against a real mailbox.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from pyactivesync.models import FolderType

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.config import AccountConfig
from jmap_eas.registry import AccountRegistry

_ENV_VARS = ("JMAP_EAS_LIVE_SERVER", "JMAP_EAS_LIVE_USER", "JMAP_EAS_LIVE_PASSWORD", "JMAP_EAS_LIVE_DEVICE_ID")


def _missing_env_vars() -> list[str]:
    return [v for v in _ENV_VARS if not os.environ.get(v)]


@pytest.fixture(scope="session")
def live_account() -> AccountConfig:
    missing = _missing_env_vars()
    if missing:
        pytest.skip(f"live EAS server not configured: {', '.join(missing)} not set")
    return AccountConfig(
        eas_server=os.environ["JMAP_EAS_LIVE_SERVER"],
        username=os.environ["JMAP_EAS_LIVE_USER"],
        password=os.environ["JMAP_EAS_LIVE_PASSWORD"],
        device_id=os.environ["JMAP_EAS_LIVE_DEVICE_ID"],
        user=os.environ.get("JMAP_EAS_LIVE_SMTP_USER"),
        api_token="live-test-token",
    )


@pytest.fixture
def live_adapter(live_account: AccountConfig) -> Iterator[EasAdapter]:
    adapter = EasAdapter.connect(live_account)
    try:
        yield adapter
    finally:
        adapter.close()


def test_provision_grants_a_policy_key(live_adapter: EasAdapter) -> None:
    assert live_adapter.provision()


def test_list_folders_includes_inbox(live_adapter: EasAdapter) -> None:
    folders = live_adapter.list_folders()
    assert any(f.type == FolderType.INBOX for f in folders)


def test_registry_lazily_provisions_command_and_ping_adapters(live_account: AccountConfig) -> None:
    registry = AccountRegistry({"live": live_account})
    try:
        context = registry.get("live")
        assert context.command is not context.ping
        assert context.command.provision()
        assert context.ping.provision()
    finally:
        registry.close_all()
