from __future__ import annotations

import json

from jmap_eas.backend.eas import EasAdapter
from jmap_eas.backend.sync import SyncCoordinator
from jmap_eas.jmap import eventsource
from jmap_eas.models import MailboxRecord
from jmap_eas.registry import AccountContext
from jmap_eas.store import cache, db, state

# -- parse_params -----------------------------------------------------------------------


def test_parse_params_defaults():
    params = eventsource.parse_params({})
    assert params.types == eventsource.ALL_TYPES
    assert params.close_after_state is False
    assert params.ping_interval == 0


def test_parse_params_explicit_types():
    params = eventsource.parse_params({"types": "Mailbox,Email"})
    assert params.types == ("Mailbox", "Email")


def test_parse_params_drops_unknown_types():
    params = eventsource.parse_params({"types": "Mailbox,Bogus"})
    assert params.types == ("Mailbox",)


def test_parse_params_closeafter_state():
    params = eventsource.parse_params({"closeafter": "state"})
    assert params.close_after_state is True
    params2 = eventsource.parse_params({"closeafter": "no"})
    assert params2.close_after_state is False


def test_parse_params_ping_interval():
    assert eventsource.parse_params({"ping": "30"}).ping_interval == 30
    assert eventsource.parse_params({"ping": "not-a-number"}).ping_interval == 0
    assert eventsource.parse_params({"ping": "-5"}).ping_interval == 0


# -- current_states / mail_folder_ids ----------------------------------------------------


def test_current_states_returns_requested_types(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    with database.transaction() as conn:
        state.append_change(conn, "alice", "Email", "e1", "created")
    result = eventsource.current_states(database, "alice", ("Email", "Mailbox"))
    assert result == {"Email": "1", "Mailbox": "0"}


def test_mail_folder_ids(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    with database.transaction() as conn:
        cache.upsert_mailbox(conn, MailboxRecord("alice", "1", "0", "Inbox", 2, "0"))
        cache.upsert_mailbox(conn, MailboxRecord("alice", "2", "0", "Drafts", 3, "0"))
    assert set(eventsource.mail_folder_ids(database, "alice")) == {"1", "2"}


# -- ping_and_sync --------------------------------------------------------------------------


class FakePingClient:
    def __init__(self, *, changed=None):
        self._changed = changed or []
        self.ping_calls = []
        self.sync_calls = []

    def provision(self):
        return "policy-key"

    def list_folders(self):
        return []

    def sync_folder(self, folder_id, sync_key="0", *, window_size=100, filter_type=None):
        self.sync_calls.append(folder_id)
        from pyactivesync.models import SyncResult
        return SyncResult(sync_key="1", added=[], changed=[], deleted=[], more_available=False)

    def fetch_item(self, *a, **k):
        raise NotImplementedError

    def fetch_attachment(self, *a, **k):
        raise NotImplementedError

    def create_folder(self, *a, **k):
        raise NotImplementedError

    def update_folder(self, *a, **k):
        raise NotImplementedError

    def delete_folder(self, *a, **k):
        raise NotImplementedError

    def apply_email_changes(self, *a, **k):
        raise NotImplementedError

    def create_email_draft(self, *a, **k):
        raise NotImplementedError

    def move_item(self, *a, **k):
        raise NotImplementedError

    def send_mail(self, *a, **k):
        raise NotImplementedError

    def ping(self, folder_ids, *, folder_class="Email", heartbeat=60, timeout=None):
        self.ping_calls.append(list(folder_ids))
        from pyactivesync.models import PingResult
        return PingResult(status="2", changed_folder_ids=self._changed)

    def close(self):
        pass


def _context(client) -> AccountContext:
    adapter = EasAdapter(client)
    return AccountContext(command=adapter, ping=adapter)


def test_ping_and_sync_no_changes_does_not_sync(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    sync = SyncCoordinator(database)
    client = FakePingClient(changed=[])
    context = _context(client)
    result = eventsource.ping_and_sync(sync, context, "alice", ["1", "2"])
    assert result == []
    assert client.ping_calls == [["1", "2"]]
    assert client.sync_calls == []


def test_ping_and_sync_syncs_changed_folders(tmp_path):
    database = db.connect(tmp_path / "bridge.sqlite3")
    sync = SyncCoordinator(database)
    with database.transaction() as conn:
        cache.upsert_mailbox(conn, MailboxRecord("alice", "1", "0", "Inbox", 2, "5"))  # already synced once
    client = FakePingClient(changed=["1"])
    context = _context(client)
    result = eventsource.ping_and_sync(sync, context, "alice", ["1"])
    assert result == ["1"]
    assert client.sync_calls == ["1"]


# -- SSE formatting ---------------------------------------------------------------------------


def test_format_state_event():
    frame = eventsource.format_state_event("alice", {"Email": "5"})
    assert frame.startswith("event: state\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload == {"@type": "StateChange", "changed": {"alice": {"Email": "5"}}}


def test_format_ping_event():
    frame = eventsource.format_ping_event(30)
    assert frame.startswith("event: ping\ndata: ")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload == {"interval": 30}
