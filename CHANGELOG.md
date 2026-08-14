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
