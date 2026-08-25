"""Live EAS test for the M0 adapter/registry. Opt-in via env vars, skipped otherwise (`conftest.py`).

Mirrors `pyactivesync`'s own live-test convention (plan.md section 8): the
suite skips instead of failing when credentials aren't configured, and only
exercises read-only commands, so it's safe to run against a real mailbox.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from pyactivesync.models import FolderType

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.config import AccountConfig
from jmap_eas.registry import AccountRegistry


@pytest.fixture
def live_adapter(live_account_config: AccountConfig) -> Iterator[EasAdapter]:
    adapter = EasAdapter.connect(live_account_config)
    try:
        yield adapter
    finally:
        adapter.close()


def test_provision_grants_a_policy_key(live_adapter: EasAdapter) -> None:
    assert live_adapter.provision()


def test_list_folders_includes_inbox(live_adapter: EasAdapter) -> None:
    folders = live_adapter.list_folders()
    assert any(f.type == FolderType.INBOX for f in folders)


def test_registry_lazily_provisions_command_and_ping_adapters(live_account_config: AccountConfig) -> None:
    registry = AccountRegistry({"live": live_account_config})
    try:
        context = registry.get("live")
        assert context.command is not context.ping
        assert context.command.provision()
        assert context.ping.provision()
    finally:
        registry.close_all()
