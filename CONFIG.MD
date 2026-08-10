# CONFIG.MD — stapel-docs

Config registry for **stapel-docs** (`static-scaffold-and-config.md` §2).
One row per configuration key the module reads, its **source** (`env` = the
process environment / the `STAPEL_DOCS` settings dict), what it is for,
whether it is required, and its default.

All keys are read through `docs_settings`
(`stapel_docs.conf.AppSettings`, namespace `STAPEL_DOCS`).
Resolution order per key: `settings.STAPEL_DOCS` dict → a flat Django
setting of the same name → environment variable → the default below.
`STORAGE` is resolved with `import_string` (dotted path). `SHARING` is one
nested dict key; its sub-keys are listed as `SHARING.<KEY>` rows below.

## stapel-docs

### Storage seam & presigned URLs (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| STORAGE | env | dotted path to a DocsStorage implementation (REPLACE) — every byte of document content I/O goes through it. | no | stapel_docs.storage.DjangoStorageBackend |
| STORAGE_PREFIX | env | object-key prefix: `{STORAGE_PREFIX}/{workspace_id}/{document_id}/…`. | no | docs |
| UPLOAD_URL_EXPIRES_SECONDS | env | presigned PUT URL lifetime (upload tickets). | no | 900 |
| DOWNLOAD_URL_EXPIRES_SECONDS | env | presigned GET URL lifetime (downloads). | no | 3600 |

### S3Backend (extra `[s3]`; read only when STORAGE points at it) (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| S3_ENDPOINT_URL | env | S3/MinIO endpoint URL (None = AWS default). | no | None |
| S3_PUBLIC_URL | env | public base URL substituted into presigned URLs (split-horizon deployments). | no | None |
| S3_ACCESS_KEY | env | access key id. | no | None |
| S3_SECRET_KEY | env | secret access key. | no | None |
| S3_REGION | env | bucket region. | no | us-east-1 |
| S3_BUCKET | env | bucket name. | no | stapel-docs |
| S3_CONNECT_TIMEOUT | env | botocore connect timeout (seconds) — tight on purpose; a fast failure beats a spinner. | no | 5 |
| S3_READ_TIMEOUT | env | botocore read timeout (seconds). | no | 15 |
| S3_MAX_ATTEMPTS | env | botocore retry ceiling. | no | 2 |

### Registries & seams (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| DOC_TYPES | env | document-type registry overlay merged OVER the txt/md/csv/file builtins ({slug: dotted-path to DocTypeSpec \| None removes}). | no | {} |
| EXPORTERS | env | export-format registry overlay merged OVER the built-in pdf ({format: dotted-path \| None removes}). | no | {} |
| INGEST | env | event-driven ingest ({action_name: dotted-path mapper payload → create_document kwargs}); broken entries raise ImproperlyConfigured at wiring. | no | {} |

### Journal / revisions / tree / trash (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| REPLAY_WINDOW | env | journal rows with seq ≤ snapshot_seq − window are compacted away (chat-pattern replay window). | no | 500 |
| AUTO_REVISION_INTERVAL_SECONDS | env | a snapshot save mints an `auto` Revision when the newest revision is older than this (0 = revision on every save). | no | 300 |
| FOLDER_MAX_DEPTH | env | folder tree depth ceiling (create/move/ingest-path all enforce it). | no | 10 |
| TRASH_RETENTION_DAYS | env | soft-deleted items become purgeable by `docs_purge_expired` after this many days; purge is irreversible. | no | 30 |

### Sharing axis (closed defaults; v1 system-check guards E010-E014) (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| SHARING.MODES | env | additional grant sources over the immutable workspace baseline (subset of whitelist \| link). v1 implements neither — non-empty is a system-check ERROR. | no | [] |
| SHARING.RESOLVERS | env | {ref_kind: dotted-path} whitelist subject-reference resolvers — real-but-empty seam; entries are import-validated at check time (E014). | no | {} |
| SHARING.LINK.ANONYMOUS | env | anonymous link redemption. True in v1 = system-check ERROR (E012). | no | false |
| SHARING.LINK.MAX_LEVEL | env | ceiling for minted link level. Above `view` in v1 = system-check ERROR (E013); `manage` is never grantable by any share source. | no | view |
| SHARING.LINK.TTL_DAYS | env | link TTL; None (perpetual) only ever by explicit host choice. | no | 30 |
