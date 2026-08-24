# jmap-eas-python

A JMAP (RFC 8620 core + RFC 8621 mail) server in front of an Exchange
ActiveSync (EAS) 16.1 mailbox that does not speak JMAP natively. EAS access
goes through [`pyactivesync`](https://github.com/monperrus/pyactivesync)'s
public `Client` API only; this repository implements the JMAP layer,
persistence, and the adapter between the two, not the EAS wire protocol.

See [`plan.md`](plan.md) for the full design and milestone plan. This is a
Mail-only bridge; Contacts and Calendar are out of scope for v1.

## Status

Foundation stage (M0): package layout, configuration, the ASGI application
lifecycle, SQLite connection/migration plumbing, and a small `pyactivesync`
adapter with a lazy per-account registry. No JMAP methods are implemented
yet — that starts with the M1 read path.

## Configuration

The bridge reads a TOML file named by the `JMAP_EAS_CONFIG` environment
variable. The file must be owner-readable only (`chmod 600`) because it can
reference account credentials.

```toml
[server]
host = "127.0.0.1"   # loopback by default; a public listener needs explicit config
port = 8080
db_path = "jmap-eas.sqlite3"

[policy]
allow_moves = true
allow_delete = true
allow_send = true

[accounts.alice]
eas_server = "https://mail.example.com/Microsoft-Server-ActiveSync"
username = "alice@example.com"
password_env = "JMAP_EAS_ALICE_PASSWORD"   # read from the environment, never stored in JMAP responses or logs
device_id = "jmapeas0001"
```

`pyactivesync` supports HTTP Basic authentication only (including
`domain\user` login strings); NTLM and OAuth are not available.

## Known limitations (v1)

- One `mailboxId` per email: EAS does not model JMAP's multi-mailbox
  membership.
- `Email/import` and `Email/copy` (retaining the original) are unsupported:
  EAS 16.1 only allows client-originated `Sync Add` for draft email.
- A submission timeout leaves the outcome unknown; sending is not retried
  automatically.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
mypy src
```

During coordinated development with `pyactivesync`, install it from an
adjacent editable checkout rather than PyPI.
