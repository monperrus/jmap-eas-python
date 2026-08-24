from __future__ import annotations

import threading

import pytest

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.config import AccountConfig
from jmap_eas.errors import UnknownAccountError
from jmap_eas.registry import AccountRegistry


class FakeClient:
    def __init__(self) -> None:
        self.closed = False

    def provision(self) -> str:
        return "policy-key"

    def list_folders(self) -> list[object]:
        return []

    def close(self) -> None:
        self.closed = True


def _account(**overrides) -> AccountConfig:
    defaults = dict(
        eas_server="https://mail.example.com/Microsoft-Server-ActiveSync",
        username="alice@example.com",
        device_id="dev1",
        password="secret",
        api_token="token",
    )
    defaults.update(overrides)
    return AccountConfig(**defaults)


def _registry(accounts, calls=None):
    calls = calls if calls is not None else []

    def factory(config: AccountConfig) -> EasAdapter:
        calls.append(config)
        return EasAdapter(FakeClient())

    return AccountRegistry(accounts, adapter_factory=factory), calls


def test_get_unknown_account_raises():
    registry, _ = _registry({})
    with pytest.raises(UnknownAccountError):
        registry.get("nobody")


def test_get_creates_context_lazily_on_first_access():
    registry, calls = _registry({"alice": _account()})
    assert calls == []
    context = registry.get("alice")
    assert len(calls) == 2  # one command adapter, one Ping adapter
    assert registry.get("alice") is context
    assert len(calls) == 2  # second get() must not reinitialize


def test_command_and_ping_adapters_are_distinct():
    registry, _ = _registry({"alice": _account()})
    context = registry.get("alice")
    assert context.command is not context.ping


def test_get_is_thread_safe_and_initializes_once():
    registry, calls = _registry({"alice": _account()})
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        registry.get("alice")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 2


def test_unrelated_account_unaffected_by_another_accounts_failing_factory():
    def factory(config: AccountConfig) -> EasAdapter:
        if config.username == "broken@example.com":
            raise RuntimeError("cannot connect")
        return EasAdapter(FakeClient())

    registry = AccountRegistry(
        {
            "broken": _account(username="broken@example.com"),
            "ok": _account(username="ok@example.com"),
        },
        adapter_factory=factory,
    )
    with pytest.raises(RuntimeError):
        registry.get("broken")
    context = registry.get("ok")
    assert context is not None


def test_failed_initialization_is_not_cached():
    attempts = {"count": 0}

    def factory(_config: AccountConfig) -> EasAdapter:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient failure")
        return EasAdapter(FakeClient())

    registry = AccountRegistry({"alice": _account()}, adapter_factory=factory)
    with pytest.raises(RuntimeError):
        registry.get("alice")
    context = registry.get("alice")
    assert context is not None


def test_close_all_closes_every_materialized_context_and_clears_registry():
    registry, _ = _registry({"alice": _account(), "bob": _account(username="bob@example.com")})
    alice = registry.get("alice")
    bob = registry.get("bob")
    registry.close_all()
    assert alice.command._client.closed is True  # type: ignore[attr-defined]
    assert alice.ping._client.closed is True  # type: ignore[attr-defined]
    assert bob.command._client.closed is True  # type: ignore[attr-defined]
