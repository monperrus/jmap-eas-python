# jmap-eas-python

A JMAP (RFC 8620 core + RFC 8621 mail) server in front of an Exchange
ActiveSync (EAS) 16.1 mailbox that does not speak JMAP natively. EAS access
goes through [`pyactivesync`](https://github.com/monperrus/pyactivesync)'s
public `Client` API only; this repository implements the JMAP layer,
persistence, and the adapter between the two, not the EAS wire protocol.

See [`plan.md`](plan.md) for the full design and milestone plan. This is a
Mail-only bridge; Contacts and Calendar are out of scope for v1.

## Status

M1, the synchronized read path, is implemented: `GET /.well-known/jmap`
(session resource), `POST /api` (`Core/echo`, `Mailbox/get`/`query`/`changes`,
`Email/get`/`query`/`changes`, `Thread/get`), and `GET /download/...` for
message and attachment blobs. Every request re-syncs the authenticated
account's folders and messages from EAS into the local cache before serving
it. `Email/get`'s cheap properties (subject, addresses, receivedAt, keywords,
mailboxIds, threadId) come straight from that cache; `size`, `preview`,
`hasAttachment`, `attachments`, `textBody`/`htmlBody`, and `bodyValues` need a
live `ItemOperations` fetch and are only fetched when actually requested.
`bcc`, `sender`, `sentAt`, `inReplyTo`, `references`, `messageId`, and
`bodyStructure` aren't derivable from what `pyactivesync`'s `Sync`/`Fetch`
expose today and are always returned `null`.

M2, mutations, is also implemented: `Mailbox/set` (create/rename/reparent/
delete, cascading a folder delete to its cached emails), `Email/set` for
read/flagged keyword changes (`$seen`/`$flagged` only -- any other keyword is
rejected per object, not silently dropped), single-mailbox move, delete, and
draft creation (`mailboxIds` must resolve to exactly one Drafts-role mailbox
and `keywords` must include `$draft: true` -- EAS's `Sync Add` only accepts
drafts), and `POST /upload/{accountId}` for attachment blobs referenced from
a draft. `Email/import` and `Email/copy` are intentionally unregistered, not
stubbed: EAS 16.1 has no arbitrary-folder item creation to implement them
with. Moves and deletes are further gated by `policy.allow_moves`/
`allow_delete` (forbidden per object when disabled, not method-wide, since
e.g. keyword updates must stay available either way); `Email/set` create/
update always commits EAS's returned SyncKey even when the per-item status
isn't `"1"`, so a rejected mutation never desyncs the folder.

M3, sending, is implemented: `Identity/get` synthesizes the account's single
identity from its configured SMTP address (there's no send-as-alias concept
here, so `Identity/set` doesn't exist), and `EmailSubmission/get`/`set`/
`changes` submit an existing cached email (typically a draft created via
`Email/set`) by fetching its full MIME live and passing it unchanged to
`SendMail`. Sending is immediate -- EAS has no delayed-send/Outbox model --
so a created submission's `undoStatus` is always `"final"` and update/destroy
have nothing to change (destroy just forgets the local bookkeeping row; it
never un-sends). The submission record is persisted *before* the send call,
recording EAS's `client_id` for correlation, because a transport failure
after the server already accepted the message is indistinguishable from one
before it from here -- plan.md section 6 is explicit that this must not
trigger an automatic resend, and it doesn't. `EmailSubmission/set` create is
gated by `policy.allow_send` (forbidden per object); unlike the other policy
gates, disabling it also drops the `urn:ietf:params:jmap:submission`
capability (and `Identity/get`'s reason for existing) from that account's
`accountCapabilities` in the session resource, since submission has no other
purpose a disabled account would still need.

M4 is in progress: `Mailbox/queryChanges` and `Email/queryChanges` use a
conservative diff over the plain change log rather than tracking sort
positions -- an object that was destroyed, or created/updated at all
(whether or not it currently matches the query), is reported `removed`;
one that's created/updated and still matches is reported `added` at its
current index. This can occasionally report a redundant `removed` for an id
the client never had, which RFC 8620 explicitly allows (the client just
ignores it), in exchange for never missing a real change. `POST /api`
now validates `using` per RFC 8620 section 3.1 (must include the core
capability; must not name an unrecognized one). The `change_log` table is
pruned to the most recent 100k entries per account after every sync, with a
retention watermark so a `sinceState`/`sinceQueryState` older than the
pruned history correctly raises `cannotCalculateChanges` instead of silently
under-reporting. Structured (JSON) logging and lightweight in-memory
request/error/sync-failure counters are wired in; `GET /healthz` includes a
`metrics` snapshot.

A folder sync caches at most 10 pages (1000 items) per request
(`SyncCoordinator.DEFAULT_MAX_PAGES_PER_CALL`) so one JMAP request can never
block for as long as a large mailbox's full initial sync takes; a folder with
more pending pages just keeps catching up over subsequent requests. Verified
live against a real Exchange mailbox with 1000+ messages per folder,
including empty folders (a real-world empty-`Sync`-response-body bug this
uncovered was fixed upstream in `pyactivesync`).

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
password_env = "JMAP_EAS_ALICE_PASSWORD"   # the EAS mailbox password
api_token_env = "JMAP_EAS_ALICE_TOKEN"     # a separate bridge-only secret JMAP clients authenticate with
device_id = "jmapeas0001"
```

`pyactivesync` supports HTTP Basic authentication to EAS only (including
`domain\user` login strings); NTLM and OAuth are not available. JMAP clients
authenticate to *this bridge* separately, over HTTP Basic auth with the
account id as username and `api_token` as password -- a bridge secret, never
the EAS mailbox password.

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

### Live tests

`tests/integration/test_eas_adapter_live.py` exercises `EasAdapter` and
`AccountRegistry` against a real EAS server. It skips automatically unless
these environment variables are set (never commit real values):

```bash
export JMAP_EAS_LIVE_SERVER=...
export JMAP_EAS_LIVE_USER=...        # e.g. a domain\user login
export JMAP_EAS_LIVE_SMTP_USER=...   # optional, defaults to JMAP_EAS_LIVE_USER
export JMAP_EAS_LIVE_DEVICE_ID=...
export JMAP_EAS_LIVE_PASSWORD=...
pytest tests/integration/test_eas_adapter_live.py
```

It only calls read-only commands (`Provision`, `FolderSync`).
