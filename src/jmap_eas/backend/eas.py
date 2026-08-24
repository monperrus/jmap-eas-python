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
from pyactivesync.models import BodyType, FetchedItem, Folder, SyncResult

from ..config import AccountConfig
from ..errors import map_eas_exception


class EasClientProtocol(Protocol):
    """The subset of `pyactivesync.Client` this adapter currently depends on."""

    def provision(self) -> str: ...

    def list_folders(self) -> list[Folder]: ...

    def sync_folder(
        self, folder_id: str, sync_key: str = "0", *, window_size: int = 25, filter_type: str | None = None
    ) -> SyncResult: ...

    def fetch_item(self, folder_id: str, item_id: str, *, body_type: BodyType = BodyType.HTML) -> FetchedItem: ...

    def fetch_attachment(self, file_reference: str) -> bytes: ...

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

    def sync_folder(self, folder_id: str, sync_key: str = "0", *, window_size: int = 100) -> SyncResult:
        try:
            return self._client.sync_folder(folder_id, sync_key, window_size=window_size)
        except EASError as exc:
            raise map_eas_exception(exc) from exc

    def fetch_item(self, folder_id: str, item_id: str, *, body_type: BodyType = BodyType.HTML) -> FetchedItem:
        try:
            return self._client.fetch_item(folder_id, item_id, body_type=body_type)
        except EASError as exc:
            raise map_eas_exception(exc) from exc

    def fetch_attachment(self, file_reference: str) -> bytes:
        try:
            return self._client.fetch_attachment(file_reference)
        except EASError as exc:
            raise map_eas_exception(exc) from exc

    def close(self) -> None:
        self._client.close()
