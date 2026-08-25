"""Lazy, failure-safe per-account EAS client registry (plan.md section 2).

A context is created only on first access, guarded by a per-account lock so
concurrent requests for the same account do not race to initialize it twice.
Failed initialization is never cached, so a transient error does not
permanently disable an account, and a slow or broken account does not block
initialization of unrelated accounts. Each context holds two adapters: one
serialized command connection, and a separate one reserved for long-running
`Ping` calls, so an EventSource long poll never blocks ordinary JMAP calls.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from .backend.eas import EasAdapter
from .config import AccountConfig
from .errors import UnknownAccountError

AdapterFactory = Callable[[AccountConfig], EasAdapter]


class AccountContext:
    """The command and Ping EAS connections held open for one account."""

    def __init__(self, command: EasAdapter, ping: EasAdapter) -> None:
        self.command = command
        self.ping = ping
        # Folder mutations additionally take per-folder locks (not held here)
        # because they consume and replace that folder's SyncKey.
        self.command_lock = threading.Lock()
        # Serializes concurrent EventSource connections' use of the Ping adapter;
        # independent of command_lock so a long Ping never blocks ordinary JMAP calls.
        self.ping_lock = threading.Lock()

    def close(self) -> None:
        self.command.close()
        self.ping.close()


class AccountRegistry:
    """Materializes one `AccountContext` per configured account, on demand."""

    def __init__(
        self,
        accounts: dict[str, AccountConfig],
        *,
        adapter_factory: AdapterFactory = EasAdapter.connect,
    ) -> None:
        self._accounts = accounts
        self._adapter_factory = adapter_factory
        self._contexts: dict[str, AccountContext] = {}
        self._init_locks = {account_id: threading.Lock() for account_id in accounts}

    def get(self, account_id: str) -> AccountContext:
        if account_id not in self._accounts:
            raise UnknownAccountError(account_id)
        context = self._contexts.get(account_id)
        if context is not None:
            return context
        with self._init_locks[account_id]:
            context = self._contexts.get(account_id)
            if context is not None:
                return context
            config = self._accounts[account_id]
            context = AccountContext(
                command=self._adapter_factory(config),
                ping=self._adapter_factory(config),
            )
            self._contexts[account_id] = context
            return context

    def close_all(self) -> None:
        """Close every materialized context. Call during application shutdown."""
        for context in self._contexts.values():
            context.close()
        self._contexts.clear()
