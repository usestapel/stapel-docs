# Changelog

All notable changes to stapel-docs are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

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
