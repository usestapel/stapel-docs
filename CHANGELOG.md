# Changelog

All notable changes to stapel-docs are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

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
