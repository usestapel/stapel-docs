# Changelog

All notable changes to stapel-docs are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [Unreleased]

## [0.4.1] — 2026-09-02

### Changed — a markdown document exports as markdown, not as its source

`?format=pdf` on an `md` document printed the body verbatim: literal `#`,
`**`, backticks and pipe-tables, laid out as one wrapped paragraph. The
exporter's own docstring admitted it ("md is rendered verbatim as plain text
in v1"), which is the giveaway — the format was in the supported list, the
endpoint returned 200, the PDF was well-formed and openable, and nothing
anywhere said the output was wrong. A caller could only find out by looking
at one.

- **The md path parses.** Python-Markdown produces HTML, the HTML is
  sanitized (below), and fpdf2's `write_html` renders it: headings sized off
  the 11pt body, `**bold**` and `*italic*` in the real bold and oblique
  faces, `-`/`1.` lists bulleted and numbered, fenced blocks in a monospace
  face, pipe tables as bordered grids, `[text](url)` as a clickable PDF
  annotation, `---` as a rule. Fpdf2's own defaults are overridden where they
  do not survive this font set: `<code>`/`<pre>` default to Courier, and
  headings to a dark red sized for a 12pt Times body.
- **Two fpdf2 defaults needed more than a style override.** A markdown table
  has no syntax for asking for borders, so the sanitizer emits the `border`
  attribute that gets fpdf2's full-grid layout — a table here looks like the
  csv exporter's, not like a rule under the header row. And fpdf2 draws a
  list marker with the PDF's *live* font rather than the one the paragraph
  is about to use, so every list under a heading got a bold 15pt `1.` beside
  11pt text; `HTML2FPDF_CLASS` (fpdf2's own hook) pins the marker to the body
  face, defensively enough that a future fpdf2 loses the fix instead of
  raising mid-export.
- **Three more DejaVu faces ship in `assets/`** — Oblique, BoldOblique and
  SansMono (v2.37; the two already there are v2.35). fpdf2 synthesizes
  neither italics nor a monospace, and its core fonts — Courier included, the
  one `<code>` would otherwise get — are latin-1 only, so a cyrillic
  identifier inside a fenced block had no way to render. The three faces are
  registered on the md path alone: `add_font` re-parses the TTF per document,
  and a txt or csv export has no use for them. ~1.6MB in the wheel, in
  exchange for a renderer that needs nothing installed on the host.
- **No WeasyPrint.** The rendering verdict in the design corpus said
  WeasyPrint; it drags system pango/cairo, and the fleet default is
  zero-infra. The deviation is deliberate and recorded by the track lead:
  fpdf2 + Python-Markdown keeps `[pdf]` pure-python and `pip install`-able
  everywhere the rest of the module is.

### Security — a document body is user input reaching an HTML renderer

Markdown passes raw HTML through untouched, so switching the md path to an
HTML renderer handed whoever typed the body a renderer to aim. `<img>` is the
sharp edge: fpdf2 resolves an image `src` **by fetching it**, which turns
"export this document" into "make this server issue a request of the author's
choosing" — a note containing `<img src="http://169.254.169.254/...">` is a
metadata-service probe with an export button on it. fpdf2 2.8.8's own
resource-access policy blocks private addresses, but that is a second line,
not the design.

- **An allowlist sanitizer sits between the parser and the renderer**
  (`_sanitize_html`). Images are dropped entirely before fpdf2 sees one, alt
  text kept as italics so a figure's caption is not lost; `<script>` and
  `<style>` lose their content, not just their tags; only `http`, `https` and
  `mailto` hrefs become annotations (a `javascript:` anchor keeps its text and
  loses its link); every attribute outside a per-tag allowlist is stripped,
  including a `<font face=…>` naming a font the document never registered,
  which is a 500 written by the body's author; unknown tags keep their text
  and lose their markup; and unbalanced tags are closed here rather than
  crashing fpdf2's parser mid-document.

### Added — `markdown` in the `[pdf]` and `all` extras

`markdown>=3.4`. **Missing, it raises `ExporterUnavailable` → 503**, the same
answer as a missing fpdf2, and explicitly *not* a fallback to the old
verbatim rendering: the fallback would return 200 with a PDF nobody can tell
from a rendered one, and the incomplete install would never be discovered.
txt and csv are untouched by the absence — only the md path imports it, and
a test pins that.

`tests/test_export.py` reads the **text layer** of the PDFs it renders
(pypdf, added to the CI test deps) and asserts the markdown markers are gone
while the content is not, that the bold/oblique/mono faces are actually
embedded, that the link became an annotation, that cyrillic survives inside a
code block, and that a body with a remote image exports without fetching it.
The parse-and-sanitize claims are also asserted directly on the pure
function, so they hold in a checkout without pypdf.

**Patch, not minor**: no public name, setting, error key, endpoint or schema
changed — the same request returns the same media type, rendered properly.
The new dependency is optional and inside an existing extra.

## [0.4.0] — 2026-08-30

### Added — `user.merged`: a guest's documents survive being folded into an account

This module knew one thing about an account's end: erase it. When a visitor
who wrote as a guest signs in with an authenticator an existing account
already holds, stapel-auth folds the two and emits `user.merged` — the
opposite instruction. Nothing here answered it, so the guest's documents kept
an `owner` that could no longer sign in: never listed for the survivor, and
never erased either, because no erasure is ever requested for an account that
was *merged* rather than closed. The failure has no symptom at the seam —
nothing raises, nothing retries, nothing is logged — and the first report of
it is a person saying the notes they wrote are gone.

- **`user.merged` is subscribed in `stapel_docs.actions`** and re-parents
  every column this module keys by a user, in one transaction:
  `Document.owner`, `Folder.created_by`, `Revision.created_by`,
  `DocumentUpdate.author_id` (the CRDT journal's attributed writes) and
  `UploadSession.created_by`, so an in-flight upload can still be finalized
  by the account that now holds the ticket.
- **Re-parent, not anonymize.** The erasure path nulls exactly these columns
  because "nobody wrote this any more"; a merge sets them because somebody
  else did. The two events reach the same tables through the same registry,
  and answering only one of them is a silent wrong answer to the other.
- **An ordering lag is retried; a bad id is not.** A guest who authored
  nothing here is a quiet no-op (also the at-least-once idempotency path); a
  guest who authored rows while the survivor has no user row here *yet*
  raises `MergeTargetNotReady`, so the outbox redelivers instead of marking
  the event delivered and stranding the documents. A malformed or missing id
  is logged and ACKed — `ValidationError` included, which is what Django
  raises for an uncoercible UUID and is not a `ValueError`, the guard a
  poison payload otherwise escapes through and loops on forever.
- `schemas/consumes/user.merged.json` and the MODULE.md / readme action
  tables carry the contract. `tests/test_user_merged.py` pins the rows
  moving, a redelivery moving nothing further, every malformed shape ACKing,
  an event about users with no rows here doing nothing — and
  `stapel_core.lifecycle.E001` returning `[]`, so the pair cannot be broken
  again without a red test.

**Minor, not patch**: a new consumed action is public surface. Requires no
new stapel-core API; the E001 check that names the gap ships in stapel-core
0.52.1.

## [0.3.0] — 2026-08-23

### Added — subject-scoped erasure: docs answers stapel-gdpr for `account`, `workspace` and `document`

stapel-gdpr 0.5.0 generalized account closure into an erasure keyed by a
subject (`{subject_type, subject_key}`) and made owner silence visible: a
declared owner that never answers is named at boot (`gdpr.W006`) instead of
discovered when a request times out. This module was reachable only through
`user.deleted`, i.e. only for accounts — a deleted workspace or a deleted
document left its rows, its journal, its revisions and every object of its
history in place, with nothing that could prove otherwise.

- New `erasure.py`: `erase(subject_type, subject_key, workspace_id=None)`
  returns what it removed, per subject:
  - `document` — the row, the update journal, every `Revision` and every
    storage object of the document's history, through the module's own
    `services.purge_document` (O(document), idempotent, objects deleted
    after commit). Live or trashed alike: an erasure is not a trash
    operation and does not wait out `TRASH_RETENTION_DAYS`. Upload sessions
    still pointing at the document die with their staging objects.
  - `workspace` — every document of the workspace as above, then the folder
    tree and every pending upload session with its staging object.
  - `account` — unchanged policy, now reached through the same entry point:
    authorship is **anonymized, not deleted** (storage-verdict §3), because
    documents are co-produced workspace content and destroying them would
    erase other members' data under the banner of erasing one person's.
- New consumers in `actions.py` (one module, both handlers — that
  co-location is what makes an `alive` answer evidence the erasure path is
  *consumed*): `gdpr.erasure.requested` → the erase above plus a
  `gdpr.section.erased` receipt `{owner: "docs", subject_type, subject_key,
  receipt_id, counts}` emitted in the same transaction; `gdpr.owner.probe` →
  `gdpr.owner.alive {owner, subject_types}`.
- Contracts committed: `schemas/emits/gdpr.section.erased.json`,
  `schemas/emits/gdpr.owner.alive.json` (validated on every emit under
  `VALIDATE_SCHEMAS`) and `schemas/consumes/{gdpr.erasure.requested,
  gdpr.owner.probe,user.deleted}.json` — the consumed half of the comm
  surface was undeclared until now.
- `DocsGDPRProvider.anonymize()` / `.delete()` now return the per-model
  counts they changed (they returned `None`); the base `GDPRProvider`
  ignores a return value, so the orchestrator's in-process path is
  unaffected, and the counts are what an `account` receipt carries.

Refusals are deliberate: a subject type this owner does not claim is
ignored (gdpr opens no part for it), and a request whose `workspace_id`
contradicts the document's row raises instead of receipting zeros — the
part then times out visibly rather than certifying an erasure that never
happened.

Host wiring (one line, no new setting here):

```python
STAPEL_GDPR = {"DATA_OWNERS": {"docs": ["account", "workspace", "document"]}}
```

**Minor, not patch**: new public module (`stapel_docs.erasure`), two new
emitted actions and two new consumed ones — public surface grew.

### Deprecated — `user.deleted`

Still consumed, and now routed through `erase("account", …)` instead of
calling the provider beside it. stapel-gdpr emits it alongside
`gdpr.erasure.requested` for one minor and removes it in its 0.6.0; this
module's handler goes with it. It deliberately emits **no** receipt: the
account erasure that carries a correlation_id arrives as
`gdpr.erasure.requested` and is receipted there.

### Not in this release

Share and mandate grants: v1 has none to erase — every verdict comes from
`workspaces.check_capability` and the sharing axis's own grant rows
(whitelist/link) do not exist yet (`SHARING`, phase 3). They join the
`document`/`workspace` erasures in `erasure.py` when they land.

## [0.2.4] — 2026-08-21

### Changed — `stapel-core` floor raised to 0.27.0

CI installs `stapel-core` off git HEAD (currently 0.32.0), which has grown
`error.503.mandate_unavailable` (+ ru/es catalogs) since this module's floor
was last raised. `docs/errors.json` regenerated against that HEAD now carries
the entry, and the drift gate (`tests/test_contract.py`) went red against the
stale committed artifact until it did. Floor raised to >=0.27.0 (the release
that registers the error) so the committed contract and the declared minimum
agree again; `make contract` re-run to pick it up.

## [0.2.3] — 2026-08-21

### Fixed — `_save_snapshot` mutation and outbox emits shared no atomic block (EMIT003)

`_save_snapshot` (the single snapshot-save path behind `save_content` and
`create_document`) wrote the document/revision rows and then called
`events.emit_document_updated` / `events.emit_storage_changed` relying on the
*caller's* `transaction.atomic()` — invisible to
`stapel_core.lint.emit_check`'s lexical EMIT003 gate and, more importantly,
inconsistent with every other emit site in this module, which all open their
own `transaction.atomic()` around mutation + emit. Wrapped the writes and
both emits in the helper's own `transaction.atomic()`; nesting inside the
callers' wider atomic block is a safe savepoint
(`stapel_core.comm.mutate_and_emit`'s documented nesting guarantee) and now
the helper is self-sufficient under the static gate.

## [0.2.2] — 2026-08-15

### Changed — `stapel-core` floor raised to 0.26.0

`docs/errors.json` carries an `owner` per entry, and only stapel-core 0.26.0
emits it. The floor lagged behind, so a consumer resolving an older core
regenerated an artifact without `owner` and the drift gate went red — the
field was declared but never required. The floor now matches the artifact
that is committed.

## [0.2.1] — 2026-08-15

### Added — the error catalogs this module owns (ru, es)

This module registers 32 `error.*.docs_*` keys and shipped a catalog for
none of them. Since stapel-core 0.23.1 a consumer resolves a key it does
not own from the **owner's** catalog, and since 0.22.0 a writer may only
translate keys it owns — so shipping nothing did not leave the gap open for
someone else to fill legally: it made every consumer render the English
literal, and the one that filled it locally was maintaining a shadow of
this module's canon that nothing here would ever update.

`translations/errors.ru.json` + `translations/errors.es.json` (32 keys
each) now ship, with the `translations/.state.json` provenance sidecar,
and both are in the wheel (`package-data`) — a catalog that reaches only
the repository is a catalog no deployment can read. Languages match what
every other stapel library with error keys promises: en canon in
`errors.py`, ru and es as catalogs.

Provenance is recorded, not implied: the curated stapel-translate builtin
corpus carries none of these keys, so every value is a machine translation
(`origin: llm`, the gate's unreviewed counter) reproduced offline from the
table in `tests/test_error_i18n.py`. `tests/test_error_i18n.py` is also the
gate — coverage scoped to ownership, no foreign keys, placeholders
preserved, byte-stable, no drift.

Strings and packaging only: no code, no schema, no migration changed.

## [0.2.0] — 2026-08-14

### Added

- **Resource invariants** (security audit DOCS-01): hard ceilings on every
  accepted byte — snapshot body, journal update and batch, upload blob and
  exporter input — plus an opt-in per-workspace stored-byte quota, all in
  the `STAPEL_DOCS` namespace (`MAX_BODY_BYTES`, `MAX_UPDATE_BYTES`,
  `MAX_UPDATES_PER_REQUEST`, `MAX_UPLOAD_BYTES`, `MAX_EXPORT_BYTES`,
  `WORKSPACE_QUOTA_BYTES`).
- **Upload sessions are capabilities, not slots**: a ticket carries an
  expiry (`UPLOAD_SESSION_TTL_SECONDS`), belongs to the user who opened it
  (anyone else needs workspace `manage`), accepts an optional declared
  sha256, and is consumed by a conditional state transition. Finalize
  validates the STORED object — existence, ceiling, declared size and
  checksum — before promoting it. Open sessions per workspace are capped
  (`MAX_PENDING_UPLOADS_PER_WORKSPACE`) and uploads can be restricted to a
  MIME allowlist (`UPLOAD_ALLOWED_MIME_TYPES`).

- **The internal create path is scoped to a caller** (audit DOCS-02):
  `docs.create_document` now carries its authority in the payload —
  `actor_id` is authorized through the same choke point as an HTTP caller
  (`docs.edit` in the target workspace), or the calling service must be
  listed in `INTERNAL_TRUSTED_SERVICES`; an `owner_id` other than the actor
  must itself be a workspace member. `INTERNAL_REQUIRE_CALLER` (default on)
  makes an unbound call a refusal, and a workspaces outage is a 503, never
  an allow.
- **Scheduled retention**: `stapel_docs.tasks` with `purge_expired_trash`
  (plain callable; a celery shared task when celery is installed) and
  `get_docs_beat_schedule()` on the configured `TRASH_PURGE_SCHEDULE`, plus
  system check `stapel_docs.W030` for a host whose beat schedule never runs
  the purge.

### Changed

- **Requires stapel-core >= 0.24.0** (was `>=0.15.11`). Not driven by a new
  call: this module declares `import_strings=("STORAGE",)`, and on an older
  core an import-string key was still read from the environment unless the
  module listed it in `no_env` — which this one never did, so a bare
  `STORAGE` environment variable could choose the storage class the package
  loads. The download-URL refusal below is a property of that class
  (`mints_expiring_urls = True`), so on an older core "may this deployment
  hand out permanent public links" is a question the environment can answer.
  Core 0.24.0 makes every `import_strings` key implicitly `no_env`, with
  `env_overridable=` as the per-key opt-out.
- **UPGRADE NOTE — an ownerless upload ticket needs `manage`** (audit
  DOCS-03). The owner binding on upload finalize was skipped entirely when
  `created_by_id` was falsy, which is exactly what GDPR anonymize leaves
  behind, so any workspace editor could spend such a ticket. A ticket with
  no owner now takes the same `docs.manage` escalation as somebody else's
  ticket (403 `error.403.docs_upload_owner` otherwise) — including for the
  user who originally opened it, since the row no longer says they did.
  Hosts that anonymize users mid-upload should expect those pending
  tickets to be finalized by a manager or abandoned to expiry.
- **An upload whose object cannot be measured is refused** (audit
  DOCS-03), with the new 400 `error.400.docs_upload_unmeasurable`. Finalize
  used to fall back to the size the CLIENT declared when the store could
  not report one — and `DjangoStorageBackend.head_object` turned any
  exception from `storage.size()` into "exists, size unknown", so a storage
  fault became an upload that passed the ceiling unchecked and charged the
  workspace quota nothing (zero, for a ticket opened without a declared
  size). `head_object` no longer swallows those errors: a backend that
  cannot size an object raises, and the finalize fails.
- **UPGRADE NOTE — the workspace storage quota ships switched on** (audit
  DOCS-03). `WORKSPACE_QUOTA_BYTES` defaults to **10 GiB** instead of `0`.
  It was the one limit in the hard-invariants block that shipped off, so
  the only thing bounding a workspace's growth was the object store's
  invoice. A workspace already holding more than 10 GiB of heads +
  revisions will start refusing writes with 507
  `error.507.docs_workspace_quota` until the setting is raised. **To keep
  unlimited storage** set `STAPEL_DOCS["WORKSPACE_QUOTA_BYTES"] = 0` — `0`
  remains "no quota", now as a deliberate opt-out rather than as the
  shipped default.
- **UPGRADE NOTE — uploads ship with a real MIME allowlist** (audit
  DOCS-03). `UPLOAD_ALLOWED_MIME_TYPES` no longer defaults to `[]`, and
  `[]` no longer means "anything": an empty allowlist now allows nothing,
  and the shipped default is `stapel_docs.conf.DEFAULT_UPLOAD_MIME_TYPES`
  (text/data, PDF/RTF, Office + OpenDocument, enumerated image types, and
  common audio/video). Deliberately absent: `text/html`,
  `image/svg+xml`, `application/javascript` and executables — active
  content a host serving `MEDIA_URL` inline would run in its own origin.
  An upload that declares **no** `mime_type` is unknown content and is now
  refused too (400 `error.400.docs_upload_mime`), so clients that omitted
  the field must start sending it. **To restore the old
  accept-anything behaviour** set
  `STAPEL_DOCS["UPLOAD_ALLOWED_MIME_TYPES"] = ["*/*"]` — the open position
  is now something a deployment states, not something it inherits from an
  unfilled setting. Widening the list for your own types is the normal
  path.
- **UPGRADE NOTE — download URLs must be able to expire** (audit DOCS-03).
  `GET /documents/{id}/download` and `GET /documents/{id}/revisions/{id}/download`
  now refuse with **503 `error.503.docs_download_url_unavailable`** when the
  configured storage backend cannot honour `DOWNLOAD_URL_EXPIRES_SECONDS`.
  The default `DjangoStorageBackend` cannot: it could only return
  `storage.url(key)` — a permanent, public `MEDIA_URL` link that outlives
  the membership it was minted for and never passes `authorize()` again,
  which contradicted the module's own "there is no second read path".
  Deployments on the default backend lose those two URL endpoints unless
  they act; the authorized `GET …/content` stream serves the same bytes and
  is unchanged, as are uploads, and `S3Backend` (which really signs) is
  unaffected. **To restore the old behaviour** set
  `STAPEL_DOCS["ALLOW_UNEXPIRING_DOWNLOAD_URLS"] = True` — that is now an
  explicit, checked decision to hand out permanent public links (system
  check `stapel_docs.W032`), while `stapel_docs.W031` warns any deployment
  whose download path is refusing. A `DocsStorage` implementation advertises
  its ability by setting `mints_expiring_urls = True`; the fail-closed
  default is `False`, so a host backend that signs must say so.
  `DjangoStorageBackend.presigned_get_url` also no longer swallows storage
  errors into a bare object key returned as if it were a URL.
- **Object-store writes follow the database outcome** (audit DOCS-02):
  snapshot writes are compensated when the surrounding transaction fails
  and object deletes are deferred to `on_commit`, so a rolled-back save
  leaves no orphan and a rolled-back purge cannot destroy bytes a surviving
  row still points at. New seam: `services.storage_transaction()`.
- **`file` bodies have exactly one door.** `DocTypeSpec` gained
  `body_mutable`; the content PUT (and create-with-body) now refuses types
  that own their own write path — `type=file` (upload flow) and types whose
  spec vanished from the registry, which the storage verdict already
  promised were read-only. Revision restore replays already-accepted bytes
  and is unaffected.
- The upload ticket response carries `expires_at`.

## [0.1.1] — 2026-08-10

### Fixed

- **The stapel-core pin was a scaffold placeholder** (`>=0.3.0,<0.4`) that
  never got set: next to any real host (fleet services floor core at
  `>=0.18`) pip either refused to resolve or silently downgraded
  stapel-core to 0.3.2 — observed live while mounting docs into
  iron-recordings. Now `>=0.15.11,<1.0`, a measured floor: the full suite
  is green against 0.15.11, 0.16.0 and 0.18.0 (probed 2026-08-10); older
  cores are unmeasured and unclaimed.

## [0.1.0] — 2026-08-10

First release. Document/folder tree with versioning, exports, and the comm-bus
surface other modules need to attach documents to their own entities.

- **Versioning substrate** — document/folder models, `DOC_TYPES` registry,
  storage seam, authz choke point.
- **HTTP surface** — tree, content with optimistic locking, journal,
  revisions, trash, uploads.
- **Comm surface** — `docs.create_document` action, emitted-actions join
  point for other modules, GDPR anonymize provider, INGEST seam.
- **PDF exporter** — txt/md/csv via fpdf2 with bundled DejaVu unicode fonts.
- Full lifecycle proven end-to-end against a real host project (auth +
  workspaces + docs); contract quintet (`docs/{schema,flows,errors,capabilities,llms}`)
  drift-gated in CI.
