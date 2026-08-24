"""Small adapter around the public `pyactivesync.Client` API.

Wraps the calls the bridge needs, translating `pyactivesync` exceptions into
:class:`jmap_eas.errors.BackendError`. Later milestones extend this surface
(sync, fetch, mutate, send, ...) as their JMAP methods are implemented; this
adapter must never reach into `pyactivesync`'s private `_wbxml`, `_codepages`,
`_http`, or `_mime` modules (plan.md section "Strategy").
"""
from __future__ import annotations

from typing import Protocol

from pyactivesync import Client
from pyactivesync.exceptions import EASError
from pyactivesync.models import Folder

from ..config import AccountConfig
from ..errors import map_eas_exception


class EasClientProtocol(Protocol):
    """The subset of `pyactivesync.Client` this adapter currently depends on."""

    def provision(self) -> str: ...

    def list_folders(self) -> list[Folder]:
        ...

    def close(self) -> None: ...


class EasAdapter:
    """One EAS connection for one account, wrapped for JMAP-facing error handling."""

    def __init__(self, client: EasClientProtocol) -> None:
        self._client = client

    @classmethod
    def connect(cls, config: AccountConfig) -> EasAdapter:
        client = Client(
            config.eas_server,
            config.username,
            config.resolve_password(),
            device_id=config.device_id,
            user=config.user,
            verify_ssl=config.verify_ssl,
            timeout=config.timeout,
        )
        return cls(client)

    def provision(self) -> str:
        try:
            return self._client.provision()
        except EASError as exc:
            raise map_eas_exception(exc) from exc

    def list_folders(self) -> list[Folder]:
        try:
            return self._client.list_folders()
        except EASError as exc:
            raise map_eas_exception(exc) from exc

    def close(self) -> None:
        self._client.close()
