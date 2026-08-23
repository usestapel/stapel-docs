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
- **Content-addressed object storage** behind a swappable seam
  (`DocsStorage`): identical bodies dedup for free, orphaned snapshots die
  when nothing points at them, per-workspace byte deltas are announced via
  `document.storage_changed`. The library is **body-blind** — only a
  type's own `text_extractor` may parse a body.
- **Trash with irreversible purge**: soft-delete (subtree-wise for
  folders), restore (re-announced), explicit purge (`trash/empty`) and
  retention expiry (`docs_purge_expired`) that destroy rows + journal +
  every historical object, O(document), idempotently.
- A REST surface (folders / documents / content / updates / revisions /
  trash / uploads — 27 operations under `/docs/api/v1/`), presenter-
  canonical and serializer-seamed, with **one authorization choke point**
  (`authz.authorize`, fail-closed via `workspaces.check_capability`).
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

## Extension points (fork-free)

### 🚩 The document-type registry — `DOC_TYPES` (**merge**)

A document is ONE entity with a `type` slug resolved against an open merge
registry: builtins (`txt`, `md`, `csv`, `file`) ← settings overlay
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
`text_extractor` (bytes → str, may be None). Broken overlay entries are a
system-check ERROR (E002). A type whose spec **vanishes** from the
registry degrades to `file` behavior — read-only, never unreadable
(verdict §7.3): revisions still list, snapshots still download,
trash/purge/export still work. **Type is immutable after creation** — a
conversion is a new document created through an explicit flow.

### Storage seam — `STORAGE` (**replace**)

Single-strategy replace seam: dotted path to a `DocsStorage`
implementation. Ships `DjangoStorageBackend` (default — rides Django's
`default_storage`; presigned URLs degrade to served URLs; never assume S3
URL shape) and `S3Backend` (boto3 presigned + native multipart;
`pip install stapel-docs[s3]`; `S3_*` keys). Implement the ABC to target
any store; `get_storage()` resolves + memoizes. Wrong class / unimportable
path is a system-check ERROR (E001). **No `default_storage`/boto3 calls
outside `storage.py`** — lint-enforced (storage-verdict §9.2).

### Exporters — `EXPORTERS` (**merge**)

`{format: dotted-path | None}` merged over the built-in `pdf`
(`PdfExporter`, extra `[pdf]`, fpdf2 + bundled DejaVu fonts). Contract:
`formats: tuple[str, ...]`; `export(document, body, spec) -> (bytes,
mime)`; raise `ExportUnsupportedType` (→ 400) or `ExporterUnavailable`
(missing optional dependency → 503). Broken entries: check ERROR (E020).

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

### Sharing axis — `SHARING` (**closed by default in v1**)

`authorize()` implements exactly the immutable workspace baseline (active
membership + capability `docs.<action>` via `workspaces.check_capability`,
deny-by-default). The additional grant sources are configuration that
exists from day 1 with **closed defaults**, guarded by system checks —
"configured but not implemented" is a LOUD deploy failure, never a silent
no-op:

| Key | v1 default | Opening it in v1 |
|---|---|---|
| `SHARING["MODES"]` | `[]` | unknown mode → E010; known-but-unimplemented → E011 |
| `SHARING["RESOLVERS"]` | `{}` (**merge**, real-but-empty seam) | entries import-validated → E014 |
| `SHARING["LINK"]["ANONYMOUS"]` | `False` | `True` → E012 |
| `SHARING["LINK"]["MAX_LEVEL"]` | `"view"` | above `view` → E013 |
| `SHARING["LINK"]["TTL_DAYS"]` | `30` | — (tuning) |

The algebra is additive and monotonic: sources only ever grant, the
baseline can never be configured away, and `manage` is **never** grantable
by any share source (anti-escalation invariant). The `Principal` form
(`user_id` / `is_anonymous` / `link_token`) is fixed on day 1 so
anonymous-link support later is an additive branch inside `authorize`.

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
| `FOLDER_MAX_DEPTH`, `TRASH_RETENTION_DAYS` | value | tree/trash tuning |
| `MAX_BODY_BYTES`, `MAX_UPDATE_BYTES`, `MAX_UPDATES_PER_REQUEST`, `MAX_UPLOAD_BYTES`, `MAX_EXPORT_BYTES` | value (`0` = ceiling off) | hard resource ceilings on every accepted byte |
| `WORKSPACE_QUOTA_BYTES` | value (ships at 10 GiB; `0` = quota off) | per-workspace stored-byte budget (507 when crossed) |
| `UPLOAD_SESSION_TTL_SECONDS`, `MAX_PENDING_UPLOADS_PER_WORKSPACE` | value | upload-ticket expiry, open-session ceiling |
| `UPLOAD_ALLOWED_MIME_TYPES` | value (ships a real allowlist; `["*/*"]` = any) | which content types may be uploaded at all |
| `INTERNAL_REQUIRE_CALLER`, `INTERNAL_TRUSTED_SERVICES` | value | authority carried by comm callers of `docs.create_document` |
| `TRASH_PURGE_SCHEDULE` | value | cadence of `stapel_docs.tasks.purge_expired_trash` (beat) |
| `SHARING` | axis (closed defaults; `RESOLVERS` **merge**) | sharing beyond the baseline |

### Comm surface

Emits happen in the service layer, **inside the mutating transaction**
(outbox canon). Every emitted name has a schema under `schemas/emits/`,
validated in tests (`VALIDATE_SCHEMAS`).

| Kind | Name | Role | Payload / schema |
|---|---|---|---|
| Function (**provides**) | `docs.create_document` | the ingest seam — returns `{"document_id"}`; the payload carries its authority (`actor_id` authorized for `docs.edit`, or a trusted `caller_service`) | `schemas/functions/docs.create_document.json` |
| Action (emit) | `document.created` | create, restore (re-announce), upload finalize | `schemas/emits/document.created.json` |
| Action (emit) | `document.updated` | per accepted save / restored revision (journal appends deliberately do NOT emit — bus economy) | `schemas/emits/document.updated.json` |
| Action (emit) | `document.deleted` | "left the visible corpus" — fires on trash AND purge | `schemas/emits/document.deleted.json` |
| Action (emit) | `document.storage_changed` | per-workspace byte delta; whether `Workspace.storage_used_bytes` follows is the host's subscriber decision | `schemas/emits/document.storage_changed.json` |
| Action (emit) | `gdpr.section.erased` | the erasure receipt: `{correlation_id, owner: "docs", subject_type, subject_key, receipt_id, counts}` — emitted in the same transaction as the erasure it reports | `schemas/emits/gdpr.section.erased.json` |
| Action (emit) | `gdpr.owner.alive` | probe answer: `{owner: "docs", subject_types}` — from the *same* subscriber that erases | `schemas/emits/gdpr.owner.alive.json` |
| Action (consume) | `gdpr.erasure.requested` | subject-scoped erasure — `account` \| `workspace` \| `document` (see **Erasure** below) | `schemas/consumes/gdpr.erasure.requested.json` |
| Action (consume) | `gdpr.owner.probe` | answered with `gdpr.owner.alive` | `schemas/consumes/gdpr.owner.probe.json` |
| Action (consume) | `user.deleted` | the pre-0.5.0 account path, routed through the same `erase("account", …)`; deprecated in stapel-gdpr 0.5.0, removed there in 0.6.0 | `schemas/consumes/user.deleted.json` |
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
| `document` | the document id | the row, its update journal, every `Revision` and **every object of its history** — through `services.purge_document`, the same O(document) purge trash uses. Live or trashed alike: an erasure is not a trash operation and does not wait out `TRASH_RETENTION_DAYS`. Upload sessions still pointing at it die with their staging objects | `documents`, `revisions`, `updates`, `upload_sessions`, `storage_objects` |
| `workspace` | the workspace id | every document of the workspace (live and trashed) as above, then the whole folder tree and every pending upload session with its staging object | same keys, plus `folders` |
| `account` | the user id | **anonymize, not delete** — `DocumentUpdate.author_id`, `Revision.created_by`, `Document.owner`, `Folder.created_by`, `UploadSession.created_by` are nulled. Documents are co-produced workspace content and survive their authors (storage-verdict §3); destroying them would erase other members' data under the banner of erasing one person's | `documents_anonymized`, `folders_anonymized`, `revisions_anonymized`, `updates_anonymized`, `upload_sessions_anonymized` |

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

Not owned here: **share and mandate grants**. v1 implements only the
immutable workspace baseline — every verdict comes from
`workspaces.check_capability`, and the sharing axis's own grant rows
(whitelist/link) do not exist yet (`SHARING`, phase 3). When they land, they
join the `document`/`workspace` erasures in this module; today the grants an
erased subject held are the membership rows stapel-workspaces erases.

### Contract emission — the quintet in `docs/`

This module emits its own machine-readable contract per-module
(contract-pipeline.md §2): `docs/schema.json` (drf-spectacular OpenAPI,
canonical `/docs/api/v1` prefix), `docs/flows.json` (`[]` — no
`@flow_step` annotations), `docs/errors.json`, `docs/capabilities.json`
(axes + extension points + the 47-entry usage surface, curated in
`docs/capabilities.meta.json`) and `docs/llms.txt` (budget 5500 — see the
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

- **Realtime collaborative editing transport** (websocket fan-out, edge
  awareness) — v1 is snapshot-save + journal replay over HTTP; a realtime
  layer authorizes through the same `authorize()`.
- **Knowledge-chunk indexing / search** — subscribe to `document.updated`
  and read via `text_extractor`-equipped specs.
- **Quota enforcement** — react to `document.storage_changed` in the
  billing/workspaces host layer; docs only accounts and announces.
- **Sharing UI** and the whitelist/link mechanisms themselves — phase 3,
  behind the closed axis.

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
