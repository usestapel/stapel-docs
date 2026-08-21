# Changelog

All notable changes to stapel-docs are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [Unreleased]

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
