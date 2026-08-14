# Changelog

All notable changes to stapel-docs are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [Unreleased]

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
