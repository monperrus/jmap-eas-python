# jmap-eas-go: implementation plan

**Goal**: expose JMAP (RFC 8620 core + RFC 8621 mail) over an Exchange
ActiveSync (EAS) backend that doesn't speak JMAP natively — same product
shape as `../jmap-proxy-go`, which does this over IMAP+SMTP, but for the
huge population of mail servers (Exchange on-prem, Office 365, and a long
tail of EAS-speaking hosted mail like `webmail.kth.se`) that expose EAS but
may not expose IMAP at all, or gate it behind extra approval.

This plan is grounded in two things: a full read of `../jmap-proxy-go`'s
source (architecture, package boundaries, what's genuinely reusable), and a
hand-rolled EAS client (`../eas-experiment/eas_test.py`) that validated 17
of 19 EAS commands against a real Exchange server this session — including
every command this bridge needs (`Provision`, `FolderSync`, `Sync`,
`ItemOperations`, `SendMail`, `MoveItems`, `FolderCreate/Update/Delete`,
`GetItemEstimate`, `Ping`, `Settings`). The Go section
7 below carries forward every hard-won protocol fact from that session so
this plan doesn't repeat the same wrong guesses in Go.

## Strategy: fork, don't depend

`jmap-proxy-go`'s transport/protocol layer (`internal/jmapserver`) is
genuinely backend-agnostic already — `CallContext.Values map[string]any`,
a `BlobBackend` interface, an `EmailSender` interface. But
`internal/jmapmail`'s Email/Mailbox read-write handlers are **not** behind
an interface: `context.go` does `ctx.Values[connKey].(*imapsync.Conn)`, a
concrete-type assertion, throughout every method file. And both packages
live under `internal/`, which Go's compiler refuses to let any module
other than `jmap-proxy-go` itself import — so "depend on jmap-proxy-go as a
library, only implement the EAS backend" isn't available as an option
without first patching `jmap-proxy-go` to promote those packages out of
`internal/` and introduce a backend interface upstream.

That upstream refactor is real work with no payoff until a second backend
exists to justify it — classic premature abstraction. So: **copy
`jmap-proxy-go` into `jmap-eas-go`, rename the module, delete
`internal/imapsync` and `internal/smtpsync`, and replace them with a new
`internal/eassync`.** `internal/jmapserver` and `internal/store` carry over
close to verbatim (see §2). `internal/jmapmail`'s method-handler files
carry over structurally (same RFC 8621 method, same JSON shapes, same
filter/sort/paginate logic) but every place they call into `imapsync.Conn`
gets rewritten against `eassync`'s API — not a mechanical rename, because
the data models genuinely differ (uint32 UID vs string ServerId, mailbox
*name* vs folder *ServerId*, CONDSTORE modseq vs EAS SyncKey). §4 has the
file-by-file breakdown.

If a second non-IMAP backend ever shows up, promoting `jmapserver` +
`jmapmail` to a shared public module and revisiting this fork is the right
next move — not now.

## 1. What EAS actually gives us (from this session's findings)

Verified against a real Exchange server (see `../eas-experiment/README.md`
for the full table):

| EAS command | Confirmed working | JMAP-relevant use |
|---|---|---|
| `Provision` | ✅ | auth handshake; needs `Settings.DeviceInformation` on request 1 or the server returns `Status=165` |
| `FolderSync` | ✅ | full folder hierarchy → `Mailbox/get`/`query` |
| `Sync` (per folder) | ✅ | incremental item list (`Add`/`Change`/`Delete`) → `Email/get`/`query`/`changes` |
| `GetItemEstimate` | ✅ | cheap folder item count |
| `ItemOperations` (Fetch body) | ✅ | `Email/get`'s `bodyValues`/`textBody`/`htmlBody` |
| `ItemOperations` (Fetch attachment via `FileReference`) | ✅ | blob download for attachments |
| `SendMail` | ✅ (WBXML `ComposeMail` wrapper, not raw MIME, at protocol v14.1) | `EmailSubmission/set` — **replaces `smtpsync` entirely**, no separate SMTP leg needed |
| `Settings` (`Oof` Get) | ✅ | optional: vacation-responder passthrough (not in jmap-proxy-go's scope; skip for v1) |
| `Ping` | ✅ | push/polish milestone — could back `GET /eventsource` more cheaply than polling `FolderSync`/`Sync` on a timer |
| `FolderCreate`/`Update`/`Delete` | ✅ | `Mailbox/set` create/update/destroy |
| `MoveItems` | ✅ | `Email/set` `mailboxIds` patch (single-mailbox move) |
| `ResolveRecipients` | ✅ (real MS-ASWBXML token table, not guessed) | not needed for Mail-only scope; parked |
| `Search` (GAL scope) | ✅ | not needed for Mail-only scope; parked |
| `Search` (Mailbox scope, structured query, e.g. `GreaterThan`/date) | ✅ | works fine — `Status=1`, real results; not needed for Mail-only scope, parked alongside GAL |
| `Search` (Mailbox scope, `FreeText`/full-text) | ❌ confirmed server-side bug (`Store.Status=110` on every attempt, isolated to the `FreeText` condition specifically — see §7) | **not usable**; JMAP `Email/query` text filtering doesn't need it anyway (§3), and this isn't a client-side gap to work around |
| `MeetingResponse`, `ValidateCert` | not executed (real side effects / no certs) | out of scope (Mail-only, no Calendar in v1) |

Also confirmed and directly relevant to the id/state design (§3):
Contacts and Calendar folders exist and sync exactly like Email folders
(`Sync` against `FolderHierarchy.Type` 8/9), so a post-Mail-only extension
to JSContact/JSCalendar is a `Sync`-against-a-different-folder-class
problem, not a new-protocol problem — noted in Roadmap (§8), not in v1
scope, matching `jmap-proxy-go`'s own "Mail only" scope discipline.

## 2. What carries over from `jmap-proxy-go` largely unmodified

| Package | Disposition | Why |
|---|---|---|
| `internal/jmapserver` | **Keep as-is.** Transport, JSON-RPC dispatch, method-call batching, `#creationId` refs, session doc, blob upload/download HTTP handlers, SSE. Zero IMAP-specific code in this package already. | Already backend-agnostic by design (`Values map[string]any`, `BlobBackend`/`EmailSender` interfaces). |
| `internal/store` (`accounts.go`, `changelog.go`, `blobs.go`, `submissions.go`) | **Keep, minor schema changes.** `changelog.go`/`blobs.go`/`submissions.go` are already backend-agnostic (deliberately: `email_cache.go`'s own doc comment says "independent of any IMAP library type"). `accounts.go` needs new columns (§4). | State-transition log, blob store, and submission tracking don't know or care what protocol fetched the data. |
| `internal/store/mailbox_ids.go` | **Delete.** EAS `FolderHierarchy.ServerId` is already a stable, server-minted, rename-invariant identifier (verified this session: renaming a folder via `FolderUpdate` left its `ServerId` unchanged) — exactly the property this file exists to fake on top of IMAP's renameable mailbox *names*. Use the EAS ServerId directly as the JMAP Mailbox id. | Simplification EAS gives us for free; IMAP can't. |
| `internal/jmapmail/thread.go`, `filter.go`, `paginate.go`, `mime_build.go`, `idresolver.go`, `state.go`, `set.go`, `omitempty.go` | **Keep as-is.** Operate purely on already-fetched Go structs (envelope fields, MIME construction from JSON `EmailBodyPart` trees, generic filter-tree evaluation) — no backend calls in any of these. | Confirmed by reading each file: `evalFilter` in `filter.go` walks an in-memory `[]Email`-shaped slice; `Email/query`'s text search is evaluated client-side, not delegated to a backend SEARCH command — so EAS mailbox-scope `Search`'s broken `FreeText` condition (§1, §7) is a non-issue, not a gap: this bridge was never going to call it. |
| `internal/jmapmail/identity.go` | **Keep as-is**, same "one Identity, synthesized from the account address" design `jmap-proxy-go` already uses (EAS has no separate identity-management concept either). | |
| `cmd/difftest`, `internal/difftest` | **Keep as-is.** Fires identical JMAP requests at a reference server and this implementation, diffs structurally. Entirely backend-agnostic — exercises the HTTP/JMAP surface, never touches `imapsync`/`eassync` directly. | Reuse for regression-testing `jmap-eas-go` against a reference JMAP server exactly like `jmap-proxy-go` does. |
| `internal/credentials` | **Keep as-is.** AES-256-GCM at rest, password-in/password-out. EAS auth is still a username+password (Basic or NTLM), so the encryption layer doesn't change. | |

## 3. Data model: EAS → JMAP

| JMAP concept | IMAP-backed (`jmap-proxy-go`) | EAS-backed (`jmap-eas-go`) |
|---|---|---|
| Mailbox id | minted random id, persisted, mapped from renameable IMAP name (`store.MailboxID`) | **EAS `FolderHierarchy.ServerId` directly** — stable across rename, no mapping table needed (§2) |
| Mailbox role | RFC 6154 SPECIAL-USE attrs, name-fallback heuristic (`imapsync/role.go`) | `FolderHierarchy.Type` code, directly enumerated (2=Inbox, 3=Drafts, 4=DeletedItems→trash, 5=SentItems→sent, 6=Outbox, 7=Tasks, 8=Calendar, 9=Contacts, 10=Notes, 11=Journal, 12=Other/user-created — verified this session against a real `FolderSync` response) — simpler than IMAP's optional-extension-plus-name-guessing dance, no fallback layer needed |
| Email id | `mailboxName + UID` (documented as *not* stable across a mailbox rename — `jmap-proxy-go` README "Design notes") | `CollectionId:ItemId`, e.g. `"9:1"` (EAS's own `ServerId` format). Stable across folder *rename* (folder ServerId doesn't change), **not** stable across a `MoveItems` move to another folder (verified: `9:1` → `20:1` after `MoveItems`) — same fundamental limitation `jmap-proxy-go` already documents for IMAP UID+COPY, not a regression |
| Incremental sync (`Email/changes`) | CONDSTORE `CHANGEDSINCE` modseq diff + one `UID SEARCH` for expunge detection (`imapsync/condstore.go`) | **EAS `Sync`'s own opaque `SyncKey`** is already exactly RFC 8620's "opaque state token you hand back to get a delta" model — arguably a *better* fit than CONDSTORE. Persist one `SyncKey` per (account, folder) in `store`; a `Sync` call with that key returns `Add`/`Change`/`Delete` directly translatable to JMAP's `created`/`updated`/`destroyed`. No UID-set diffing needed — EAS reports deletes explicitly. |
| Body/attachment fetch | `FETCH BODYSTRUCTURE` + `FETCH BODY[part]`, transfer-decoded client-side | `ItemOperations` Fetch with `AirSyncBase.BodyPreference` (body) and `FileReference` (attachments) — server returns base64-decoded content directly (verified: attachment round-tripped byte-for-byte via base64) |
| Send (`EmailSubmission/set`) | separate SMTP leg (`smtpsync.Send`) | **`SendMail`** (WBXML `ComposeMail` wrapper — raw-MIME POST only works at EAS protocol ≤12.1, not the 14.1 this bridge targets). One less protocol to implement than `jmap-proxy-go`. |
| Mailbox create/rename/delete | IMAP `CREATE`/`RENAME`/`DELETE` | `FolderCreate`/`FolderUpdate`/`FolderDelete` — confirmed cascading delete (deleting a folder deletes its contents, like IMAP) |
| Move between mailboxes | IMAP `COPY` + expunge source | `MoveItems` (atomic, single command — no copy+delete pair) |
| Draft creation (`Email/import` / initial `Email/set create`) | IMAP `APPEND` | **No EAS equivalent of APPEND.** EAS has no "inject an arbitrary RFC 5322 message into a folder" command outside `SendMail`. Two options, pick per method (§6): (a) `SendMail` with `SaveInSentItems` semantics doesn't apply to Drafts — no; (b) some EAS servers accept a `Sync` `Add` command with a full `Email` WBXML `ApplicationData` block including a MIME body — needs a real device to confirm since this wasn't in this session's tested set. **Flagged as an open risk, see §7.** |
| Quota | IMAP `GETQUOTAROOT` (RFC 9208) | **No EAS quota command exists in MS-ASCMD.** `Quota/get` has nothing to source from; either omit the capability or report `unlimited`/synthesize from `GetItemEstimate` sums (weak). Documented gap, see §6 M4. |

## 4. New package: `internal/eassync`

Mirrors `internal/imapsync`'s file layout so the mapping in §5 stays easy
to follow, but every file is a real reimplementation, not a port — EAS's
wire format (WBXML) and command shapes have nothing syntactically in
common with IMAP.

```
internal/eassync/
  wbxml.go          WBXMLWriter/WBXMLReader — port of eas_test.py's codec 1:1,
                     this part is genuinely mechanical (Python → Go), the
                     token tables and header-offset/OPAQUE-handling bugs are
                     already found and fixed, see §7
  codepages.go       CODEPAGES/PAGE_INDEX tables — copy verbatim from
                     eas_test.py, cross-check ResolveRecipients/Search
                     against the MS-ASWBXML spec URLs already collected
                     this session before reusing those two (parked, not
                     needed for v1, but keep the verified tables since
                     they cost real effort to get right)
  transport.go       http.Client wrapper, Provision handshake + PolicyKey
                     cache (no persistent connection/session needed at all
                     unlike imapsync.Conn — EAS is just HTTP request/response
                     with a Basic-auth header and a PolicyKey header; this
                     is a real simplification over IMAP's stateful
                     dial/login/SELECT/logout lifecycle)
  folder.go          FolderSync -> []Mailbox, Type-code role table (§3)
  folder_write.go    FolderCreate/FolderUpdate/FolderDelete
  email.go           Sync(folder, syncKey) -> Add/Change/Delete, generic
                      leaf-flattening decode (eas_test.py's `leaves()`)
  email_write.go     SetFlags (Sync Change w/ Email.Read), MoveItems,
                      delete (Sync Delete), draft creation (§3 open risk)
  body.go            ItemOperations Fetch: body + FileReference attachment
                      fetch, base64 decode
  send.go            ComposeMail SendMail — implements the same
                      `EmailSender` interface shape jmapmail/submission.go
                      already declares
  estimate.go         GetItemEstimate
```

## 5. `internal/jmapmail` changes, file by file

Every file that imports `imapsync` needs its backend calls repointed at
`eassync`. Grouped by how much actually changes:

**Mechanical repoint (same shape, different backend type)**
- `context.go` — `connKey`'s value type becomes `*eassync.Conn`; `WithConn`/`connFromContext` change one type reference each.
- `mailbox_methods.go`, `mailbox.go`, `mailbox_set.go` — `conn.ListMailboxes()` → same method name, different return type (`eassync.Mailbox` with `ServerId string` instead of `imapsync.Mailbox` with `Name string` as primary key) — every call site that did `byName[mb.Name]` becomes `byID[mb.ServerId]`, i.e. delete the name→id indirection everywhere, not just in `store` (§2).
- `email_flags.go` — `SetFlags` call becomes a `Sync` `Change` command instead of IMAP `STORE`.
- `email_copy.go` — becomes `MoveItems`-based (EAS has no copy-without-move primitive; `Email/copy`'s "keep original" semantics need a `Fetch`-then-re-`Add`-as-new-item fallback if no direct copy exists — check against a real server before committing to this, flagged in §7).
- `quota.go` — see §3/§6 M4, likely becomes a stub returning no quota resources rather than a real translation.

**Real rewrite (data model differs enough that logic changes)**
- `email.go`, `email_cache.go` — `loadAllEmailsFromContext`'s account-wide scan changes from "list every mailbox, `FETCH` everything, cache by UIDVALIDITY" to "list every folder, `Sync` each with its persisted `SyncKey`, cache by folder `ServerId`" — this is the incremental-sync heart of the bridge and deserves its own design pass, not a mechanical port. `CachedEmail.UID uint32` → `CachedEmail.ItemID string`.
- `email_set.go`, `email_import.go` — depends on how draft creation resolves (§3 open risk); can't finalize until that's answered.
- `submission.go` — mostly stays (it already only depends on the `EmailSender` interface), but the `EmailSender` implementation becomes `eassync.SendMail`, and `smtpsync` disappears from `cmd/jmap-eas-go/main.go`'s wiring entirely.

**No change needed**
- `filter.go`, `thread.go`, `paginate.go`, `mime_build.go`, `idresolver.go`, `ids.go`, `identity.go`, `state.go`, `set.go`, `omitempty.go`, `get.go`, `changes.go`, `querychanges.go`, `blob.go` (once `eassync`'s `ItemOperations` attachment fetch feeds the same `BlobBackend` interface `jmapserver` already defines).

## 6. Milestones (mirrors `jmap-proxy-go`'s own M1–M4 structure)

- **M0 — protocol plumbing.** Port `eas_test.py`'s WBXML codec and codepage tables to Go (`internal/eassync/wbxml.go`, `codepages.go`). Unit-test the codec against the exact byte sequences captured this session (Provision request/response, FolderSync, Sync bootstrap+delta, SendMail ComposeMail wrapper) as golden fixtures — no live server needed for this layer.
- **M1 — read path.** `Provision` → `FolderSync` → `Sync` (bootstrap + incremental) → `ItemOperations` Fetch (body + attachment). Wires up `Mailbox/get`/`query`/`changes`, `Email/get`/`query`/`changes`, `Thread/get`. This is the bulk of the value and the least risky part — every command it needs was validated end-to-end this session.
- **M2 — write path.** `Sync` Change (flags), `MoveItems`, `FolderCreate`/`Update`/`Delete`. Resolve the draft-creation open risk (§7) before starting `Email/set create`/`Email/import`/`Email/copy` — this is the one real unknown blocking M2.
- **M3 — send path.** `ComposeMail` `SendMail` as the `EmailSender` implementation. `Identity/get` synthesized same as `jmap-proxy-go`. This is lower-risk than `jmap-proxy-go`'s M3 was — `SendMail` was fully validated this session, no separate SMTP protocol to get working.
- **M4 — push & polish.** `GET /eventsource`, `*/queryChanges` (reuse `jmapmail`'s conservative RFC 8620 §5.6 strategy as-is), `Quota/get` (likely a documented no-op — see §3). Consider backing `/eventsource` with EAS `Ping` (a real long-poll primitive, confirmed working) instead of `jmap-proxy-go`'s fixed-cadence poll — genuine improvement opportunity, not required for parity.

## 7. Protocol gotchas to carry over verbatim (don't re-discover these)

All confirmed empirically against a real Exchange server this session
(`../eas-experiment/`); reproducing them here so the Go port doesn't repeat
the debugging:

- **Codepage numbers must match the real MS-ASWBXML spec, not an arbitrary local enum order.** First implementation used `enumerate()` order for `SWITCH_PAGE` values and silently sent wrong-namespace requests that the server "successfully" parsed as garbage. Real numbers: AirSync=0, Contacts=1, Email=2, AirNotify=3, Calendar=4, Move=5, ItemEstimate=6, FolderHierarchy=7, MeetingResponse=8, Tasks=9, ResolveRecipients=10, ValidateCert=11, Contacts2=12, Ping=13, Provision=14, Search=15, GAL=16, AirSyncBase=17, Settings=18, DocumentLibrary=19, ItemOperations=20, ComposeMail=21, Email2=22.
- **WBXML header is 3 bytes (version, publicid, charset), then a separate mb\_uint string-table length** — off-by-one here (treating it as a fixed 4-byte header) silently misaligns every subsequent byte and produces plausible-looking-but-wrong parses.
- **OPAQUE (`0xC3`) binary fields must be handled explicitly** (mb\_uint length + raw bytes) or the parser misinterprets binary GUIDs (`ConversationId`, timezone blobs) as nested tag structure and desyncs deep in the tree, eventually crashing on a UTF-8 decode of binary garbage.
- **`Provision` needs a `Settings.DeviceInformation` block on the very first request**, or the server returns `Status=165` (`DeviceInformationRequired`) and gives no `PolicyKey`.
- **`SendMail` must be the WBXML `ComposeMail` wrapper at protocol v14.1**, not a raw `message/rfc822` POST body — that only works at protocol ≤12.1 and gets `Status=101` (`InvalidContent`) otherwise.
- **Don't hand-guess WBXML token tables past the well-known ones.** `ResolveRecipients.To` was guessed as `0x1A` (wrong — that's `Picture`; real value `0x10`), and the `Search` codepage was off-by-one throughout. Pulled from the official spec this session (`learn.microsoft.com/en-us/openspecs/exchange_server_protocols/ms-aswbxml/`, TOC at `.../ms-aswbxml/toc.json`) — use those verified tables (§4's `codepages.go`), don't re-derive by trial and error.
- **Mailbox-scope `Search` works fine in general — it's specifically the `FreeText` (full-text) condition that's broken, and it's an EAS-specific bug, not a content-indexing outage.** A structured query (`GreaterThan`/date range) against the same account/folder/backend server returns `Status=1` with correct results; swapping in `FreeText` for the exact same request (same backend server, confirmed via `X-BEServer` response headers on both) gets `Store.Status=110` (`ServerError`) every time, alone or combined with a working condition. The obvious hypothesis — Exchange Search content indexing down for that mailbox/database — was directly ruled out: EWS's equivalent full-text search (`QueryString`/AQS, the same engine and same content index) against the identical mailbox returns correct, accurate results (real counts, zero for a nonsense keyword, matching for a real one). So the index is healthy; the fault is isolated to ActiveSync's own `Search` command handling. Filed as a precise bug report to KTH IT (`../eas-experiment/BUG-REPORT.md`) rather than left as an unexplained flake. Doesn't block anything in scope (§3) either way — `Email/query` doesn't call server-side search at all.
- **Untested, flagged as risk**: EAS has no direct analogue of IMAP `APPEND` (inject an arbitrary message into a folder without sending it). Drafts/`Email/import`/`Email/copy`-with-original-kept all likely need `Sync` `Add` with a full `ApplicationData` MIME body — needs verification against a real server before M2 can be scoped precisely. If it doesn't work, the fallback is "drafts round-trip through `SendMail` to a drafts-only pseudo-recipient" or similar, both worse — confirm early.

## 8. Roadmap (post-v1, not blocking)

- Contacts (JSContact) and Calendar (JSCalendar) — both folder classes already confirmed reachable via the exact same `FolderSync`/`Sync` mechanism as Email (§1); this is "point the same sync engine at `Type=8`/`Type=9` folders and write a different WBXML→JSON field mapping," not a new integration.
- `Ping`-backed `/eventsource` instead of polling (§6 M4).
- Promote `jmapserver`+`jmapmail` out of `internal/` in `jmap-proxy-go` and have `jmap-eas-go` depend on it as a real module, once/if a third backend ever justifies the interface work (§ Strategy).
