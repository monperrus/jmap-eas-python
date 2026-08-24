from __future__ import annotations

from pyactivesync.models import Folder, FolderType, SyncItem

from jmap_eas.backend import mapping
from jmap_eas.models import EmailAddress


def test_map_folder_to_mailbox():
    folder = Folder(id="1", parent_id="0", type=FolderType.INBOX, name="Inbox")
    record = mapping.map_folder_to_mailbox("alice", folder)
    assert record.account_id == "alice"
    assert record.mailbox_id == "1"
    assert record.parent_id == "0"
    assert record.name == "Inbox"
    assert record.folder_type == int(FolderType.INBOX)
    assert record.sync_key == "0"


def test_mailbox_roles_cover_documented_folder_types():
    assert mapping.MAILBOX_ROLES[FolderType.INBOX] == "inbox"
    assert mapping.MAILBOX_ROLES[FolderType.DRAFTS] == "drafts"
    assert mapping.MAILBOX_ROLES[FolderType.DELETED_ITEMS] == "trash"
    assert mapping.MAILBOX_ROLES[FolderType.SENT_ITEMS] == "sent"
    assert mapping.MAILBOX_ROLES[FolderType.OUTBOX] == "outbox"
    assert FolderType.USER_GENERIC not in mapping.MAILBOX_ROLES


def test_normalize_subject_strips_single_prefix():
    assert mapping.normalize_subject("Re: hello") == "hello"
    assert mapping.normalize_subject("Fwd: hello") == "hello"
    assert mapping.normalize_subject("FW: hello") == "hello"
    assert mapping.normalize_subject("Aw: hello") == "hello"


def test_normalize_subject_strips_repeated_prefixes():
    assert mapping.normalize_subject("Re: Re: Fwd: hello") == "hello"


def test_normalize_subject_strips_numbered_prefix():
    assert mapping.normalize_subject("Re[2]: hello") == "hello"


def test_normalize_subject_lowercases_and_trims():
    assert mapping.normalize_subject("  Hello World  ") == "hello world"


def test_normalize_subject_handles_none():
    assert mapping.normalize_subject(None) == ""


def test_thread_key_uses_normalized_subject_when_present():
    assert mapping.thread_key_for_item("1", "9:1", "Re: hello") == "subject:hello"
    assert mapping.thread_key_for_item("1", "9:2", "Hello") == "subject:hello"


def test_thread_key_falls_back_to_per_item_key_when_subject_blank():
    a = mapping.thread_key_for_item("1", "9:1", "")
    b = mapping.thread_key_for_item("1", "9:2", None)
    assert a == "item:1:9:1"
    assert b == "item:1:9:2"
    assert a != b


def test_map_sync_item_to_email_extracts_fields():
    item = SyncItem(
        server_id="9:1",
        fields={
            "Email.Subject": "Hello",
            "Email.From": "Alice <alice@example.com>",
            "Email.To": "Bob <bob@example.com>; carol@example.com",
            "Email.DateReceived": "2026-01-01T00:00:00.000Z",
            "Email.Read": "1",
            "Email.Status": "2",
        },
    )
    record = mapping.map_sync_item_to_email(
        account_id="alice", mailbox_id="1", thread_id="t1", email_id="e1", item=item
    )
    assert record.server_id == "9:1"
    assert record.subject == "Hello"
    assert record.from_addresses == [EmailAddress(email="alice@example.com", name="Alice")]
    assert [a.email for a in record.to_addresses] == ["bob@example.com", "carol@example.com"]
    assert record.received_at == "2026-01-01T00:00:00.000Z"
    assert record.seen is True
    assert record.flagged is True


def test_map_sync_item_to_email_defaults_when_fields_missing():
    item = SyncItem(server_id="9:2", fields={})
    record = mapping.map_sync_item_to_email(
        account_id="alice", mailbox_id="1", thread_id="t1", email_id="e2", item=item
    )
    assert record.subject is None
    assert record.from_addresses == []
    assert record.seen is False
    assert record.flagged is False


def test_parse_address_list_handles_semicolons_and_blank():
    addresses = mapping._parse_address_list("A <a@example.com>; b@example.com")
    assert [a.email for a in addresses] == ["a@example.com", "b@example.com"]
    assert mapping._parse_address_list(None) == []
    assert mapping._parse_address_list("   ") == []
