# stapel-docs — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it
> without forking, and what not to do. Kept in the same PR as any change
> to a seam. See also README.md and CHANGELOG.md.

## What this module provides

- **Google-Drive-style workspace documents**: a `Folder` tree (soft-delete
  aware, depth-capped, live-sibling-name-unique), `Document` rows that are
  each ONE entity with a `type` slug from an open registry, `Revision`
  pointer rows (always full snapshots — I1) and a `DocumentUpdate` journal.
- A **decided versioning substrate** for both collaboration disciplines
  (storage-verdict §7): `collab="snapshot"` types save whole states under
  optimistic lock (`If-Match: "<head_seq>"` — v1's editing model, 409 with
  the winning save's attribution on a lost race); `collab="crdt"` types
  append opaque commutative updates between snapshots, replayed via
  `?since=` with chat-pattern compaction + honest `resync` semantics.
  Exactly two disciplines exist; a third must pass the I1–I4 contract
  before it may be born.
- **The crdt slice** (0.7.0, all optional extras, everything additive):
  builtin yjs-codec types `ymd`/`ytxt` (registered only when pycrdt is
  importable — `[crdt]`), server-side snapshot **assembly** that folds the
  journal via pycrdt without minting a seq, and a **realtime stream**
  `docs:doc:<id>` on the stapel-realtime substrate (`[realtime]`) with the
  same `authorize()` choke point the HTTP surface uses. Polling `?since=`
  stays first-class forever. See *The crdt slice* below.
- **Content-addressed object storage** behind a swappable seam
  (`DocsStorage`): identical bodies dedup for free, orphaned snapshots die
  when nothing points at them, per-workspace byte deltas are announced via
  `document.storage_changed`. The library is **body-blind** — only a
  type's own `text_extractor` may parse a body.
- **Trash with irreversible purge**: soft-delete (subtree-wise for
  folders), restore (re-announced), explicit purge (`trash/empty`) and
  retention expiry (`docs_purge_expired`) that destroy rows + journal +
  every historical object, O(document), idempotently.
- **The drive surfaces** (0.5.0): per-user **starred** documents and
  folders (`is_starred` rides every envelope — `null`, not `false`, for a
  principal with no user), per-user **recents** (upserted by the service
  layer on content read, download-URL issuance and accepted save; capped
  and trimmed on write), workspace-scoped **name search** with
  server-materialized breadcrumbs, and authorized image **thumbnails**
  cached under the document's own storage prefix (invariant I2 — a purge
  takes the pictures with it).
- A REST surface (folders / documents / content / updates / revisions /
  starred / recents / search / thumbnails / trash / uploads — 35 operations
  under `/docs/api/v1/`), presenter-canonical and serializer-seamed, with
  **one authorization choke point** (`authz.authorize`, fail-closed via
  `workspaces.check_capability`).
- A **usage surface** (`docs.usage`): stored bytes live/trash/total, item
  counts and a per-type breakdown for one workspace. `bytes_total` is the
  same sum the 507 quota refuses against, so a meter and a refusal can
  never tell an operator two different stories. Billing composes an
  entitlement ceiling from it; docs owns the measurement, never the price.
- A **comm ingest seam**: the `docs.create_document` Function (the
  canonical product-glue path) plus the `INGEST` action-mapper registry
  for event-driven ingest, and **subject-scoped erasure** (`erasure.py`,
  see *Erasure*): docs answers stapel-gdpr for `account` (anonymize
  authorship — documents are co-produced workspace content and survive
  their authors), `workspace` and `document` (hard purge of rows, journal,
  revisions and objects), receipts what it removed, and answers the owner
  probe from the same subscriber.

**What it delegates (does NOT implement):**

- **Membership & capability verdicts** — `workspaces.check_capability` by
  comm name (stapel-workspaces or any provider). Fail-closed: an outage is
  503, never a silent allow, and never 403-on-outage.
- **Session issuance** — any stapel-core-compatible JWT issuer
  (stapel-auth on the shelf).
- **Object storage** — all content I/O goes through the `STORAGE` seam.

## Drive surfaces (0.5.0)

Four per-user views over the corpus the workspace baseline already shows.
None of them is a second authorization path: each takes a workspace the
caller was authorized for through `authorize()`, and shows only rows a
baseline `docs.view` listing would show.

| Surface | Endpoints | Notes |
|---|---|---|
| **Starred** | `POST`/`DELETE /documents/<id>/star`, `POST`/`DELETE /folders/<id>/star`, `GET /starred?workspace_id=` | `Star` rows carry exactly one of `document`/`folder` (a `CheckConstraint`, not a convention) and are unique per `(user, target)`. Both verbs answer **204 whatever the previous state was** — an idempotent verb that reports "already done" as a failure forces every client to read before writing. Starring takes **`docs.view`**, not `docs.edit`: a star is a bookmark, and requiring edit would make "keep this handy" an act of authorship. The listing is live-only; a trashed item leaves the listing and keeps its star until purge, so a restore brings the bookmark back |
| **Recents** | `GET /recents?workspace_id=` | `RecentEntry` is upserted by the **service layer**, not by a view, on the three paths that hand a document to a person: `read_content`, `document_download_url` and an accepted `save_content`. A rejected save records nothing (it never happened); a machine read (export, thumbnail rendering, revision replay) passes no user and leaves no trace. Capped by `RECENTS_MAX_PER_USER` (default 100), trimmed oldest-first on write. **No events** — a bus message per document open would be the noisiest topic in the fleet |
| **Search** | `GET /search?workspace_id=&q=[&limit=]`, plus the existing `?q=` on `GET /documents` | Case-insensitive `icontains` over live `Folder.name` and `Document.title`, tree-wide. Each hit carries `kind` (`folder`\|`document`) and a root-first **breadcrumb** built server-side from ONE folder-index query — resolving each hit's ancestry separately is the N+1 that makes the endpoint quadratic in tree depth. An absent or blank `q` is a **400**, never the whole workspace. Deliberately not knowledge search: no FTS, no trigram (revisit with `pg_trgm` when a measured workspace says otherwise) |
| **Thumbnails** | `GET /documents/<id>/thumbnail?tier=` | `type=file` documents with an `image/*` mime only; anything else (and a pending upload with no bytes) is a 400. The tier ladder is the fixed constant `thumbnails.THUMBNAIL_TIERS` = `(160, 480)` — **not** a settings key, because the tier is part of the URL contract a client caches against and every rung is another rendered copy of every image in the bucket. Rendering needs the `[thumbnails]` extra; without Pillow the endpoint answers **503** the way a missing exporter dependency does, so a frontend falls back to a type icon instead of guessing at a silent empty answer |

**Where the thumbnails live, and why there is a `Thumbnail` row.** The
cached image is written through the storage seam under the document's own
prefix — `{PREFIX}/{workspace}/{document}/thumb.{head_seq}.{tier}.jpg` —
so invariant I2 (storage closure) holds for derived bytes too. The
`head_seq` in the key is what makes a stale image *unaddressable* rather
than merely unpreferred: a save bumps the seq, the next request asks for a
key that does not exist yet, and the renderer runs. The `Thumbnail` row
exists because `services.purge_document` deletes **enumerated** keys, not a
key prefix: an unregistered derived object would outlive the document it
depicts. Thumbnail bytes are deliberately absent from the
`document.storage_changed` delta and from the quota sum — they were never
charged, so removing them must not credit anything back.

## The crdt slice (0.7.0)

The deferred week-2 tail of the design (§9): the crdt discipline gets its
first builtin types, server assembly and a socket. Everything is behind
optional extras and everything existing keeps its exact behavior — a
deployment that installs neither extra sees nothing new but a `socket_path:
null` field on document envelopes.

**Builtin yjs-codec types — conditional.** `ymd` ("Markdown (live)",
`editor_hint="markdown.crdt"`) and `ytxt` ("Plain text (live)",
`editor_hint="text.crdt"`) register **only when pycrdt is importable**
(`pip install stapel-docs[crdt]`); without the extra no crdt builtin
exists and zero new dependencies are required. The canonical shared shape
is ONE `Y.Text` named `"content"` (what y-codemirror.next binds), so the
wire is Yjs-compatible — pycrdt is the y-crdt Rust binding.

**The body IS the Y state.** The snapshot of a yjs-codec document is the
binary CRDT state, never extracted text: a text-only snapshot would break
convergence for clients holding older Y docs (item identity must survive).
Consequences, each pinned by a test:

- `GET /content` (and revision content) serves the state as
  `application/octet-stream` — the type's logical mime stays on the spec,
  the wire tells the truth about the bytes;
- the content PUT **apply-validates** the body as a Y update
  (`error.400.docs_invalid_crdt_payload` otherwise), and journal appends to
  yjs-codec types validate each payload the same way — a corrupt payload is
  a 400 at the door, not an assembly that can never complete. Host crdt
  types with their own codec (`codec=""`) stay fully opaque;
- **human-readable export is the exporters' job**: `?format=md` / `?format=txt`
  serve the type's `text_extractor` output, and `?format=pdf` renders the
  extracted markdown/text — "download as markdown" hands a person markdown,
  never Y binary. `text_extractor` (`stapel_docs.crdt.extract_text`) is also
  what feeds search/knowledge indexing.

**Server assembly — a materialization, not a mutation.**
`services.assemble_crdt_snapshot(document_id)` row-locks the document,
folds `snapshot(bytes) + journal rows snapshot_seq+1..head_seq` through
pycrdt and stores the result via the same `_write_snapshot` path every
save uses (content-addressed put, auto revision at the folded seq, orphan
cleanup, compaction) — but **mints no seq**: assembly introduces no
operations, so `head_seq` never moves and `snapshot_seq` catches up to it
(invariant: snapshot == fold of updates `1..snapshot_seq`). It emits
`document.updated` — assembly IS the debounce point; journal appends stay
silent (bus economy, design §6) — plus the `storage_changed` byte delta.
No quota check on purpose: the bytes were each accepted through the update
ceilings, and refusing the fold would only leave them in the journal
forever. Two triggers: an append that leaves the journal
`CRDT_ASSEMBLE_UPDATE_INTERVAL` (default 200, deliberately < REPLAY_WINDOW
500 — W033) rows past the snapshot assembles inline on commit (the repo's
opportunistic-work canon), and the beat task
`assemble_idle_crdt_snapshots` (cadence `CRDT_ASSEMBLE_SCHEDULE`) folds
journals whose newest row is older than `CRDT_ASSEMBLE_IDLE_SECONDS`.

**The realtime stream — delivery only, store-first.** REST append is the
write path; after the commit one frame per journal row
(`{"update": <base64>, "author_id", "client_id"}`, envelope `seq` = the
row's seq) goes out on `docs:doc:<document_id>` via
`stapel_realtime.delivery.deliver_frame` — lazily imported, best-effort,
never a hard dependency. `DocUpdatesConsumer`
(`ws/docs/<document_id>`, discovered from `routing.py` by
`build_websocket_application`) is a `ResumableStreamConsumer`: resume by
`last_seq`, replay from the durable rows in the same payload shape as live
frames, resync past the window. Its `authorize()` is the SAME
`authz.authorize(action="view", document=doc)` call HTTP makes — a
whitelist grantee works over the socket exactly as over a URL (the 0.6.1
lesson, applied to a new transport before it shipped) — and fail-closed in
both senses: `deny` and `unavailable` alike refuse (a socket has no 503).
Document envelopes carry `socket_path` (`ws/docs/<id>`) when
`stapel_realtime` is in INSTALLED_APPS and `null` otherwise; `[realtime]`
installed without the app is a warning (`stapel_docs.W034`), never an
error — **polling stays first-class**.

**Revoke-to-kick.** Trashing or purging a document revokes its whole
stream; revoking a user-subject whitelist grant kicks that user's sockets.
Honest gaps, bounded by the substrate's authorize cache (30 s TTL,
re-checked on every hello): a **ref-subject** grant names a container, not
a user, so its revocation kicks nobody by name; and membership/capability
loss in stapel-workspaces is not this module's event to observe. The
sharing kill-switch inerts rows the same way — open sockets age out of the
cache rather than being enumerated.

**Honesty (not built, on purpose):** no presence/cursors/awareness channel
yet (the design's §5.3 p.5 ephemeral channel — a later, separate decision);
no write frames over the socket ever (design §5.3 p.6 — chat is the
fleet's documented exception, docs is not one); and revision **restore** of
a yjs-codec document restores the state as a new head exactly like any
other type, which for CRDT semantics means a live client that kept typing
merges the restored state rather than being reset by it — restore is a
snapshot-era gesture, and the honest live-collaboration "undo" is the
editor's own Y undo manager.

## Extension points (fork-free)

### 🚩 The document-type registry — `DOC_TYPES` (**merge**)

A document is ONE entity with a `type` slug resolved against an open merge
registry: builtins (`txt`, `md`, `csv`, `file`; plus `ymd`, `ytxt` when
pycrdt is importable — see *The crdt slice*) ← settings overlay
`STAPEL_DOCS["DOC_TYPES"]` (`{slug: dotted-path to a DocTypeSpec | None
to remove}`) ← runtime `register_doc_type(spec)` calls. `sheet`, `slides`,
office formats are later registry entries, **not schema changes**.

```python
# myproject/docs.py
from stapel_docs.doc_types import DocTypeSpec

SHEET_SPEC = DocTypeSpec(
    slug="sheet", label="Spreadsheet", collab="snapshot", diffable=False,
    editor_hint="sheet", mime_type="application/x-sheet+json",
    extension=".sheet", empty_body=b"{}",
    text_extractor=my_cell_text_extractor,
)

# settings.py
STAPEL_DOCS = {"DOC_TYPES": {"sheet": "myproject.docs.SHEET_SPEC"}}
```

The spec carries everything the library knows about a type: `editor_hint`
(frontend dispatch key, `""` = download-only), `collab` (which write path
is legal), `diffable`, `mime_type`, `extension`, `empty_body`,
`text_extractor` (bytes → str, may be None), and `codec` (crdt types only:
`"yjs"` opts into the server-side pycrdt mechanisms — apply-validation and
snapshot assembly; `""`, the default, keeps the journal fully opaque and
snapshot assembly the client's job). Broken overlay entries are a
system-check ERROR (E002). A type whose spec **vanishes** from the
registry degrades to `file` behavior — read-only, never unreadable
(verdict §7.3): revisions still list, snapshots still download,
trash/purge/export still work. **Type is immutable after creation** — a
conversion is a new document created through an explicit flow.

### Storage seam — `STORAGE` (**replace**)

Single-strategy replace seam: dotted path to a `DocsStorage`
implementation. Ships `DjangoStorageBackend` (default — rides Django's
`default_storage`; presigned GET degrades to the served URL, and upload
tickets carry the module's own signed intake PUT (`accepts_direct_put`
False) since 0.7.1; never assume S3 URL shape) and `S3Backend` (boto3
presigned + native multipart;
`pip install stapel-docs[s3]`; `S3_*` keys). Implement the ABC to target
any store; `get_storage()` resolves + memoizes. Wrong class / unimportable
path is a system-check ERROR (E001). **No `default_storage`/boto3 calls
outside `storage.py`** — lint-enforced (storage-verdict §9.2). Since
0.8.0 the contract carries `get_bytes_range(key, start, length)` — the
ranged read the archive-browsing endpoints list a zip's central
directory through; the ABC ships a get_bytes-backed default, both
builtin backends override it with a real ranged read (S3 `Range:` GET /
file seek), and a custom backend should too.

### Exporters — `EXPORTERS` (**merge**)

`{format: dotted-path | None}` merged over the builtins: `pdf`
(`PdfExporter`, extra `[pdf]`, fpdf2 + bundled DejaVu fonts) and, since
0.7.0, `md` / `txt` (`MarkdownExporter` / `TextExporter`, no extra) — the
type's `text_extractor` output served verbatim under the honest text mime.
They exist because a yjs-codec document's stored body is binary Y state:
"download as markdown" must hand a person markdown, and any type with a
`text_extractor` gets the same door for free. Contract:
`formats: tuple[str, ...]`; `export(document, body, spec) -> (bytes,
mime)`; raise `ExportUnsupportedType` (→ 400) or `ExporterUnavailable`
(missing optional dependency → 503). Broken entries: check ERROR (E020).

`txt` prints verbatim, `csv` as a bordered grid, and **`md` is parsed and
rendered** (Python-Markdown → sanitized HTML → fpdf2 `write_html`):
headings sized, bold/italic in real oblique/bold faces, lists bulleted and
numbered, fenced code in DejaVuSansMono, tables as grids, links as PDF
annotations. All five faces are bundled — fpdf2's core fonts (Courier
included, which is what `<code>` would otherwise get) are latin-1 only, so
cyrillic holds everywhere including inside code blocks. The body is user
input and reaches an HTML renderer, so it passes an allowlist sanitizer
first: **`<img>` is dropped before fpdf2 sees it** (fpdf2 resolves an image
`src` by fetching it — an export must not become a request the document's
author chose; the alt text is kept), `<script>`/`<style>` lose their
content, non-`http(s)`/`mailto` hrefs lose their annotation, unlisted
attributes are stripped, and unbalanced tags are closed here rather than in
fpdf2's parser. No WeasyPrint, i.e. no system pango/cairo: the extra stays
pure-python. Missing `markdown` raises `ExporterUnavailable` (503) rather
than falling back to the pre-0.4.1 verbatim print — a PDF full of literal
`#` and `**` is indistinguishable from a rendered one at the point of use.

### Ingest — `docs.create_document` + `INGEST` (**merge**)

Canonical path: product glue calls the `docs.create_document` comm
Function (`schemas/functions/docs.create_document.json`) — `body` is
utf-8 text (opaque binaries go through the upload flow), `folder_path`
like `/Meetings/2026-08` materializes folders idempotently, an unknown
`type` raises loudly. Event-driven variant: `STAPEL_DOCS["INGEST"]`
(`{action_name: dotted-path mapper}`) — docs subscribes to the named host
actions and routes `mapper(payload) -> create_document kwargs`; docs
never learns a foreign event schema, and an unimportable mapper raises
`ImproperlyConfigured` at wiring, never a log-and-skip. Delivery is
at-least-once and create is not naturally idempotent — dedup is the
mapper/host's contract.

### Sharing axis — `SHARING` (**implemented, closed by default**)

`authorize()` is the single choke point and reads the axis in one place.
Step 1 is the immutable workspace baseline (active membership + capability
`docs.<action>` via `workspaces.check_capability`, deny-by-default). Step 2
is the enabled grant SOURCES, each an independent sufficient reason,
composed as a union with the maximum level. Step 3 is deny.

Both sources ship working since 0.6.0. The axis still ships **shut**: a
document is visible to exactly its workspace until a host writes one line
of settings, because turning sharing on is one line and turning it off
afterwards is a set of links already sent.

| Key | Default | State | Opening it |
|---|---|---|---|
| `SHARING["MODES"]` | `[]` | both modes implemented | unknown mode → E010; known-but-unimplemented → E011 (kept for the next mode) |
| `SHARING["RESOLVERS"]` | `{}` (**merge**) | seam live, no resolver shipped | entries import-validated → E014; unregistered kind refused at mint, unknown/raising resolver denies at read |
| `SHARING["LINK"]["ANONYMOUS"]` | `False` | branch implemented and tested | `True` → **E012**: the owner's §10 verdict keeps deployments shut |
| `SHARING["LINK"]["MAX_LEVEL"]` | `"view"` | ceiling enforced (400 above, never a silent clamp) | `"edit"` → **E013**: edit-BY-LINK awaits an owner decision |
| `SHARING["LINK"]["TTL_DAYS"]` | `30` | mandatory expiry | `None` = perpetual, expressed as a date a century out (the column is NOT NULL) |

Invariants the module refuses to bend:

- **The baseline is immutable.** No mode configures it away, and no grant
  subtracts from it — there are no deny rows in this algebra at all, which
  is why two enabled modes cannot disagree and why disabling one can never
  open anything.
- **`manage` is never grantable.** Deleting, moving and administering
  grants stay mandatory capabilities, so a shared-in principal can read and
  even write the body and still cannot widen the circle of access.
- **An anonymous presenter never writes**, whatever level their link
  carries: the journal and revision history are attributed by design.
- **An outage is not a verdict.** Every unreachable check (the baseline, a
  link's sponsor) answers 503, never 403 and never allow.
- **A kill-switch inerts, it does not delete.** Rows of a disabled mode
  stop granting, stay listed, and are marked `suspended` — an admin who
  cannot see an inert grant reads it as revoked.

Two tables, both cascading off the document: `DocumentAccess` (whitelist —
subject `user` by id, or `ref` resolved by a host resolver) and
`DocumentLink` (the `WorkspaceInvitation` canon: unguessable token,
mandatory `expires_at`, derived status where revoked beats expired beats
active, `first_redeemed_at` stamped once). A link additionally dies the
moment its creator stops holding `docs.share.link` — checked live on every
presentation, because a bearer secret in unknown hands whose sponsor has
left is the leak itself.

The HTTP surface it adds:

| Endpoint | Gate | Notes |
|---|---|---|
| `GET`/`POST /documents/<id>/access` | `docs.share.whitelist` | The sheet lists other people, so reading it is itself an act of sharing administration. `POST` upserts (re-granting is the ordinary "make them an editor" gesture) and refuses a level above the granter's own, a half-named subject, a ref kind with no registered resolver, and a disabled mode |
| `DELETE /documents/<id>/access/<access_id>` | `docs.share.whitelist` **or** `docs.manage` | Wider than minting on purpose: taking access away must never be the thing nobody in the room is allowed to do. Works while the mode is off |
| `GET`/`POST /documents/<id>/links` | `docs.share.link` | The listing carries live tokens — which is why it is gated here and not on `docs.view`, and why no `document.share.*` **event** ever carries one. A level above `LINK["MAX_LEVEL"]` is a **400**, never a silent clamp |
| `DELETE /documents/<id>/links/<link_id>` | `docs.share.link` **or** `docs.manage` | Revocation is terminal and idempotent |
| `GET /shared/<token>` | the token | The **stripped** envelope: title, type, shape, and the level the holder has. No workspace, no folder, no owner, no star state, no revisions — a link grants a document, not a seat, and an old revision can hold text deleted on purpose since |
| `GET /shared/<token>/content` | the token | Read-only by construction; leaves no recents (a bearer is not a member) |
| `GET /shared/<token>/download` | the token | Presigned GET for the current body |

The bearer path answers **401** (`error.401.docs_share_auth_required`) when
`LINK["ANONYMOUS"]` is off and no session is present — "sign in" and "this
is not yours" are different facts, and only the first tells the holder of a
good link what to do. Every refusal after that is **404**, dead token and
unknown token alike: an endpoint that tells a guesser their token was real
once is an oracle.

**Not wired:** per-workspace narrowing
(`Workspace.settings["docs"]["sharing"]["modes"]`, axis §4). stapel-workspaces
exposes no reader for workspace settings, and docs does not reach into
another module's rows; `authz.effective_modes` is the single function that
changes when it does.

### Presenters — `STAPEL_SWAP` keys (**swap**)

Views never build DTOs; every envelope comes from a presenter resolved
through `get_presenter` (§55, SWAP001/002-enforced). Host projects reshape
any endpoint's payload by swapping:

| Swap key | Default | Presents |
|---|---|---|
| `DOCS_FOLDER_PRESENTER` | `stapel_docs.presenters.FolderPresenter` | folder tree node |
| `DOCS_DOCUMENT_PRESENTER` | `stapel_docs.presenters.DocumentPresenter` | document envelope (incl. registry-derived `editor_hint`/`collab`/`diffable`) |
| `DOCS_REVISION_PRESENTER` | `stapel_docs.presenters.RevisionPresenter` | revision pointer |

Model-less envelopes (save results, upload tickets, the journal feed) are
built by the `present_*` functions in `presenters.py`.

### Serializer seams (`views.py`) — per-view (**class override**)

`SerializerSeamMixin` — subclass a view, set `request_serializer_class` /
`response_serializer_class`, remount the URL.

| View | Request serializer | Response serializer |
|---|---|---|
| `FolderListCreateView` | `FolderCreateSerializer` | `FolderSerializer` |
| `FolderDetailView` | `FolderPatchSerializer` | `FolderSerializer` |
| `FolderRestoreView` | — | `FolderSerializer` |
| `DocumentListCreateView` | `DocumentCreateSerializer` | `DocumentSerializer` |
| `DocumentDetailView` | `DocumentPatchSerializer` | `DocumentSerializer` |
| `DocumentRestoreView` | — | `DocumentSerializer` |
| `DocumentContentView` | raw bytes (`RawBodyParser`) | `SaveResultSerializer` |
| `DocumentDownloadView` | — | `DownloadUrlSerializer` |
| `DocumentExportView` | — | raw bytes (exporter mime) |
| `DocumentUpdatesView` | `UpdatesAppendSerializer` | `UpdatesFeedSerializer` / `AppendResultSerializer` |
| `RevisionListCreateView` | `NamedRevisionSerializer` | `RevisionSerializer` |
| `RevisionContentView` | — | raw bytes |
| `RevisionDownloadView` | — | `DownloadUrlSerializer` |
| `RevisionRestoreView` | — | `SaveResultSerializer` |
| `TrashView` | — | composite (folders + documents) |
| `TrashEmptyView` | `TrashEmptySerializer` | `TrashPurgeResultSerializer` |
| `UploadCreateView` | `UploadCreateSerializer` | `UploadTicketSerializer` |
| `UploadFinalizeView` | — | `DocumentSerializer` |

### Settings — `STAPEL_DOCS` namespace (`conf.py`)

Resolution per key: `settings.STAPEL_DOCS[key]` → flat Django setting →
env var → default. Lazy; caches invalidate on `setting_changed`. Full
registry with defaults in [CONFIG.MD](CONFIG.MD).

| Key | Semantics | Customizes |
|---|---|---|
| `STORAGE` | **replace** (dotted path) | object-storage backend |
| `STORAGE_PREFIX`, `S3_*`, `*_URL_EXPIRES_SECONDS` | value | storage tuning |
| `ALLOW_UNEXPIRING_DOWNLOAD_URLS` | value (`False` = refuse) | accept permanent public download URLs from a backend that cannot sign |
| `DOC_TYPES` | **merge** over builtins (`None` removes) | document-type registry |
| `EXPORTERS` | **merge** over builtins (`None` removes) | export formats |
| `INGEST` | **merge** (empty builtin set) | event-driven ingest mappers |
| `REPLAY_WINDOW`, `AUTO_REVISION_INTERVAL_SECONDS` | value | journal/revision cadence |
| `CRDT_ASSEMBLE_UPDATE_INTERVAL`, `CRDT_ASSEMBLE_IDLE_SECONDS`, `CRDT_ASSEMBLE_SCHEDULE` | value | crdt snapshot assembly: the append-triggered interval (< REPLAY_WINDOW, W033), the idle-sweep age, the sweep's beat cadence |
| `FOLDER_MAX_DEPTH`, `TRASH_RETENTION_DAYS` | value | tree/trash tuning |
| `MAX_BODY_BYTES`, `MAX_UPDATE_BYTES`, `MAX_UPDATES_PER_REQUEST`, `MAX_UPLOAD_BYTES`, `MAX_EXPORT_BYTES` | value (`0` = ceiling off) | hard resource ceilings on every accepted byte |
| `WORKSPACE_QUOTA_BYTES` | value (ships at 10 GiB; `0` = quota off) | per-workspace stored-byte budget (507 when crossed) |
| `UPLOAD_SESSION_TTL_SECONDS`, `MAX_PENDING_UPLOADS_PER_WORKSPACE` | value | upload-ticket expiry, open-session ceiling |
| `UPLOAD_ALLOWED_MIME_TYPES` | value (ships a real allowlist; `["*/*"]` = any) | which content types may be uploaded at all |
| `MAX_ARCHIVE_ENTRIES`, `MAX_ARCHIVE_MEMBER_BYTES`, `MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES`, `MAX_ARCHIVE_COMPRESSION_RATIO` | value (`0` = ceiling off) | zip-as-folder browsing ceilings (listing refusal, member extraction, bomb hygiene) |
| `INTERNAL_REQUIRE_CALLER`, `INTERNAL_TRUSTED_SERVICES` | value | authority carried by comm callers of `docs.create_document` |
| `TRASH_PURGE_SCHEDULE` | value | cadence of `stapel_docs.tasks.purge_expired_trash` (beat) |
| `SHARING` | axis (implemented, closed defaults; `RESOLVERS` **merge**) | sharing beyond the baseline: whitelist grants and bearer links |

### Comm surface

Emits happen in the service layer, **inside the mutating transaction**
(outbox canon). Every emitted name has a schema under `schemas/emits/`,
validated in tests (`VALIDATE_SCHEMAS`).

| Kind | Name | Role | Payload / schema |
|---|---|---|---|
| Function (**provides**) | `docs.create_document` | the ingest seam — returns `{"document_id"}`; the payload carries its authority (`actor_id` authorized for `docs.edit`, or a trusted `caller_service`) | `schemas/functions/docs.create_document.json` |
| Function (**provides**) | `docs.usage` | the metering surface — `{bytes_live, bytes_trash, bytes_total, documents, folders, by_type}` for one workspace. Read-only, but workspace data all the same: the SAME caller gate as `docs.create_document`, one capability lower (`docs.view`). "It only reads" is how per-workspace corpus sizes become enumerable by every participant on the bus | `schemas/functions/docs.usage.json` |
| Action (emit) | `document.created` | create, restore (re-announce), upload finalize | `schemas/emits/document.created.json` |
| Action (emit) | `document.updated` | per accepted save / restored revision (journal appends deliberately do NOT emit — bus economy) | `schemas/emits/document.updated.json` |
| Action (emit) | `document.deleted` | "left the visible corpus" — fires on trash AND purge | `schemas/emits/document.deleted.json` |
| Action (emit) | `document.storage_changed` | per-workspace byte delta; whether `Workspace.storage_used_bytes` follows is the host's subscriber decision | `schemas/emits/document.storage_changed.json` |
| Action (emit) | `gdpr.section.erased` | the erasure receipt: `{correlation_id, owner: "docs", subject_type, subject_key, receipt_id, counts}` — emitted in the same transaction as the erasure it reports | `schemas/emits/gdpr.section.erased.json` |
| Action (emit) | `gdpr.owner.alive` | probe answer: `{owner: "docs", subject_types}` — from the *same* subscriber that erases | `schemas/emits/gdpr.owner.alive.json` |
| Action (consume) | `gdpr.erasure.requested` | subject-scoped erasure — `account` \| `workspace` \| `document` (see **Erasure** below) | `schemas/consumes/gdpr.erasure.requested.json` |
| Action (consume) | `gdpr.owner.probe` | answered with `gdpr.owner.alive` | `schemas/consumes/gdpr.owner.probe.json` |
| Action (consume) | `user.deleted` | the pre-0.5.0 account path, routed through the same `erase("account", …)`; deprecated in stapel-gdpr 0.5.0, removed there in 0.6.0 | `schemas/consumes/user.deleted.json` |
| Action (consume) | `user.merged` | the other half of that life cycle, and the opposite instruction: a guest folded into an existing account has its authorship **re-parented**, not anonymized — `Document.owner`, `Folder.created_by`, `Revision.created_by`, `DocumentUpdate.author_id`, `UploadSession.created_by`. `Star` and `RecentEntry` are re-parented with **collision folding** (both are unique per `(user, target)`): a star folds to "still starred", a recent folds to the newer `accessed_at`. A survivor with no user row here yet raises `MergeTargetNotReady` so the outbox redelivers. Idempotent | `schemas/consumes/user.merged.json` |
| Action (consume) | configured `INGEST` names | event-driven ingest via host mappers | host-owned |
| Function (**call**) | `workspaces.check_capability` | every authorization verdict (fail-closed) | provided by **stapel-workspaces** |

### Erasure

This module is a **data owner** in stapel-gdpr's erasure protocol
(deletion-lifecycle §1.3/§2). One subscriber (`actions.py`) handles both
`gdpr.erasure.requested` and `gdpr.owner.probe`; all the erasing itself
lives in `erasure.py` (`erase(subject_type, subject_key, workspace_id=None)
-> counts`), so a host can also call it in process.

Declare it in the host's inventory exactly as it claims itself
(`stapel_docs.erasure.OWNER` / `SUBJECT_TYPES`):

```python
STAPEL_GDPR = {"DATA_OWNERS": {"docs": ["account", "workspace", "document"]}}
```

| Subject | `subject_key` | What is erased | Counts in the receipt |
|---|---|---|---|
| `document` | the document id | the row, its update journal, every `Revision`, every cached thumbnail and **every object of its history** — through `services.purge_document`, the same O(document) purge trash uses. Live or trashed alike: an erasure is not a trash operation and does not wait out `TRASH_RETENTION_DAYS`. Upload sessions still pointing at it die with their staging objects; the stars and recents pointing at it cascade | `documents`, `revisions`, `updates`, `upload_sessions`, `storage_objects`, `stars`, `recents` |
| `workspace` | the workspace id | every document of the workspace (live and trashed) as above, then the whole folder tree (with the stars on it) and every pending upload session with its staging object | same keys, plus `folders` |
| `account` | the user id | **anonymize** the authorship, **delete** the per-user state. `DocumentUpdate.author_id`, `Revision.created_by`, `Document.owner`, `Folder.created_by`, `UploadSession.created_by` are nulled: documents are co-produced workspace content and survive their authors (storage-verdict §3); destroying them would erase other members' data under the banner of erasing one person's. `Star` and `RecentEntry` rows are DELETED instead — they are one person's private view of the corpus, they mean nothing without that person, and an anonymized star is a bookmark nobody can reach and nobody can clear | `documents_anonymized`, `folders_anonymized`, `revisions_anonymized`, `updates_anonymized`, `upload_sessions_anonymized`, `stars_deleted`, `recents_deleted` |

Rules the subscriber keeps:

- **Idempotent.** Delivery is at-least-once; a redelivery finds nothing left
  and receipts zeros. `receipt_id` is `docs:<correlation_id>` — stable, so a
  redelivery does not invent a second erasure in the audit trail.
- **One transaction.** Erasure and receipt commit together (outbox canon):
  the receipt leaves iff the erasure committed, so a half-done purge can
  never complete the request. Objects die after the commit — a purge that
  rolls back must leave surviving rows readable.
- **Silence over false certification.** A subject type this owner does not
  claim is ignored (gdpr opens no part for it); a request whose
  `workspace_id` contradicts the document's row raises instead of receipting
  zeros — the part then times out visibly rather than certifying an erasure
  that never happened.
- **Co-location.** The probe is answered from this same module, which is what
  makes gdpr's `W006` evidence that the erasure path is *consumed* rather
  than that a container is deployed. Do not answer it from anywhere else.

**Sharing rows are the one exception to "anonymize, not delete"** (0.6.0):
an account erasure DELETES the `DocumentAccess` rows naming that user as
subject and REVOKES the links they sponsored, because a standing permission
about a person is not co-produced content and must not outlive them — the
link least of all, since it works in hands nobody can name. Their
*provenance* (`granted_by`, `created_by`) is anonymized like any other
authorship. Document and workspace erasure take both tables with them (FK
CASCADE), counted in the receipt (`access_grants`, `links`). A `user.merged`
carries the guest's grants over with collision folding to the HIGHER level:
folding down would revoke access as a side effect of a merge.

Not owned here: **mandate grants**. Who is a member, who holds
`docs.share.*`, and who is anonymous are all answered by
stapel-workspaces / stapel-auth through existing seams; docs owns only the
object-level rows above.

### Contract emission — the quintet in `docs/`

This module emits its own machine-readable contract per-module
(contract-pipeline.md §2): `docs/schema.json` (drf-spectacular OpenAPI,
canonical `/docs/api/v1` prefix), `docs/flows.json` (`[]` — no
`@flow_step` annotations), `docs/errors.json`, `docs/capabilities.json`
(axes + extension points + the 89-entry usage surface, curated in
`docs/capabilities.meta.json`) and `docs/llms.txt` (budget 9000 — see the
Makefile for the justification). `README.md` is the sixth artifact,
assembled from `docs/readme.md` — never hand-edit `README.md`.

stapel-docs is **not mounted in stapel-example-monolith**, so validation
is standalone (`tests/test_contract.py`): determinism, self-contained
`$ref` closure, canonical-prefix paths, and `JWTCookieAuth` security on
every operation (the harness registers the JWT security-scheme extension
explicitly — the profiles finding). Regenerate after any
serializer/view/url/error/conf change:

    make contract        # PYTHON=<venv>/bin/python make contract-check

then commit `docs/*` + `README.md`.

## Anti-patterns

- **Never bypass `authorize()`** — every read/write verdict (HTTP,
  presigned URLs, future streams) routes through the single choke point.
  A hand-rolled membership check is how a future share mode ships
  half-enforced; an except-branch that allows on outage is how an outage
  becomes a data leak (503, never 403-on-outage, never allow).
- **Never touch storage outside the seam** — every byte goes through
  `get_storage()` / the `save_content` path. No `default_storage`, no
  boto3 client, no hand-built object keys (`document_prefix` /
  `snapshot_key` are what keep purge O(document) and dedup working).
- **Type is immutable after creation** — never rewrite `Document.type`;
  conversion is a new document. A type slug must resolve through the
  registry (`get_doc_types` / `effective_spec`), never through a
  hardcoded enum.
- **Don't parse bodies in the library** — only a type's own
  `text_extractor` may (body-blind substrate, verdict §6). `file` bodies
  are byte-preserved originals — never rewritten or normalized (§9.4).
- **Don't emit `document.*` from host code** — the service layer emits
  inside its own mutating transaction; a second emit double-publishes a
  public event. Go through `services.*` (or the comm Function).
- **Don't import other stapel modules** — cross-module communication is
  comm (Actions/Functions) by string name only.
- **Don't `os.getenv` at import time** — use the `STAPEL_DOCS` namespace.

## App-layer (not in this module)

- **Presence / cursors / awareness** — the ephemeral channel (design §5.3
  p.5). The journal stream ships (0.7.0, *The crdt slice*); the awareness
  channel is a later, separate decision, and its absence is stated to the
  owner rather than half-shipped.
- **Knowledge-chunk indexing / search** — subscribe to `document.updated`
  (which the crdt slice emits at assembly time — the debounce point) and
  read via `text_extractor`-equipped specs.
- **Quota enforcement** — react to `document.storage_changed` in the
  billing/workspaces host layer; docs only accounts and announces.
- **Sharing UI** — the share sheet, the copy-link affordance, the "who has
  access" list. The mechanism and its endpoints are here; how a product
  presents them is not.
- **Ref-subject resolvers** — docs ships none and never will: a resolver
  answers "is this person in that container", which only the host knows.
  Register one under `SHARING["RESOLVERS"]`.

## App-layer override vs upstream contribution — rule of thumb

**App-layer** (host project, no fork) if the change fits a seam: a
settings key, a registered doc type/exporter/ingest mapper, a presenter
swap, a subclass + URL remount, a comm subscriber, a custom storage
backend.

**Upstream** if it needs new model fields/migrations, a new endpoint, a
new settings key or seam, a third collaboration discipline, or changes a
committed schema.

Litmus: if you'd monkeypatch or edit code inside `stapel_docs/` — it's
upstream. If a setting, `register_doc_type`, subclass, receiver or comm
call gets you there — it's app-layer.
