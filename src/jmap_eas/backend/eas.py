"""Small adapter around the public `pyactivesync.Client` API.

Wraps the calls the bridge needs, translating any exception the client
raises into :class:`jmap_eas.errors.BackendError` -- not just `EASError`
subclasses, so a library bug (protocol edge case, malformed response) is
redacted from client-visible responses exactly like a normal EAS failure
(plan.md section 6). Later milestones extend this surface (sync, fetch,
mutate, send, ...) as their JMAP methods are implemented; this adapter must
never reach into `pyactivesync`'s private `_wbxml`, `_codepages`, `_http`, or
`_mime` modules (plan.md section "Strategy").
"""
from __future__ import annotations

from collections.abc import Iterable
from email.message import Message
from typing import Protocol

from pyactivesync import Client
from pyactivesync.models import (
    BodyType,
    EmailAddResult,
    EmailChange,
    EmailChangesResult,
    FetchedItem,
    Folder,
    FolderType,
    SyncResult,
)

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

    def create_folder(
        self, display_name: str, parent_id: str = "0", type: FolderType = FolderType.USER_GENERIC
    ) -> Folder: ...

    def update_folder(self, folder_id: str, display_name: str, parent_id: str = "0") -> None: ...

    def delete_folder(self, folder_id: str) -> None: ...

    def apply_email_changes(
        self, folder_id: str, sync_key: str, changes: Iterable[EmailChange], *, deletes_as_moves: bool = True
    ) -> EmailChangesResult: ...

    def create_email_draft(
        self, folder_id: str, sync_key: str, message: Message, *, read: bool = False, flagged: bool = False,
        client_id: str | None = None,
    ) -> EmailAddResult: ...

    def move_item(self, item_id: str, src_folder_id: str, dst_folder_id: str) -> str: ...

    def send_mail(self, message: Message, *, save_in_sent_items: bool = True, client_id: str | None = None) -> None: ...

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
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def list_folders(self) -> list[Folder]:
        try:
            return self._client.list_folders()
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def sync_folder(self, folder_id: str, sync_key: str = "0", *, window_size: int = 100) -> SyncResult:
        try:
            return self._client.sync_folder(folder_id, sync_key, window_size=window_size)
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def fetch_item(self, folder_id: str, item_id: str, *, body_type: BodyType = BodyType.HTML) -> FetchedItem:
        try:
            return self._client.fetch_item(folder_id, item_id, body_type=body_type)
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def fetch_attachment(self, file_reference: str) -> bytes:
        try:
            return self._client.fetch_attachment(file_reference)
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def create_folder(
        self, display_name: str, parent_id: str = "0", type: FolderType = FolderType.USER_MAIL
    ) -> Folder:
        try:
            return self._client.create_folder(display_name, parent_id, type)
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def update_folder(self, folder_id: str, display_name: str, parent_id: str = "0") -> None:
        try:
            self._client.update_folder(folder_id, display_name, parent_id)
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def delete_folder(self, folder_id: str) -> None:
        try:
            self._client.delete_folder(folder_id)
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def apply_email_changes(
        self, folder_id: str, sync_key: str, changes: Iterable[EmailChange], *, deletes_as_moves: bool = True
    ) -> EmailChangesResult:
        try:
            return self._client.apply_email_changes(folder_id, sync_key, changes, deletes_as_moves=deletes_as_moves)
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def create_email_draft(
        self, folder_id: str, sync_key: str, message: Message, *, read: bool = False, flagged: bool = False,
        client_id: str | None = None,
    ) -> EmailAddResult:
        try:
            return self._client.create_email_draft(
                folder_id, sync_key, message, read=read, flagged=flagged, client_id=client_id
            )
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def move_item(self, item_id: str, src_folder_id: str, dst_folder_id: str) -> str:
        try:
            return self._client.move_item(item_id, src_folder_id, dst_folder_id)
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def send_mail(self, message: Message, *, save_in_sent_items: bool = True, client_id: str | None = None) -> None:
        """Not automatically safe to retry (plan.md section 6): a transport failure after the
        server already accepted the message is indistinguishable from one before, from here."""
        try:
            self._client.send_mail(message, save_in_sent_items=save_in_sent_items, client_id=client_id)
        except Exception as exc:
            raise map_eas_exception(exc) from exc

    def close(self) -> None:
        self._client.close()
