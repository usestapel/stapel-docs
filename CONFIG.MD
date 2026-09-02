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
| ALLOW_UNEXPIRING_DOWNLOAD_URLS | env | accept download URLs that never expire. Off: a backend that cannot sign (the default `DjangoStorageBackend`) makes the download endpoints refuse with 503 `error.503.docs_download_url_unavailable` — the authorized `/content` stream is unaffected. On: every minted URL is a permanent public media link that outlives the membership it was issued for (check `stapel_docs.W032`). | no | False |

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

### Drive surfaces — starred / recents / search (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| RECENTS_MAX_PER_USER | env | newest N documents kept per user in the recents table, trimmed oldest-first on write (0 disables the cap — the table then grows with the corpus, per user). | no | 100 |
| SEARCH_MAX_RESULTS | env | ceiling on hits returned by `GET /search` and by the starred listing. A ceiling, not a page size: this module's listings are capped arrays, not paginated envelopes. | no | 50 |

Image thumbnail tiers are deliberately NOT configurable: the tier is part of
the URL contract a client caches against, and every extra rung is another
rendered copy of every image in the bucket. The ladder is the fixed
`stapel_docs.thumbnails.THUMBNAIL_TIERS` = (160, 480), and rendering needs
the optional `stapel-docs[thumbnails]` extra (absent → 503, never a silently
missing preview).

### Resource limits & quota (hard invariants; 0 disables one ceiling) (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| MAX_BODY_BYTES | env | largest accepted snapshot body (content PUT, create-with-body); over it → 413 `error.413.docs_body_too_large`. | no | 10485760 |
| MAX_UPDATE_BYTES | env | largest accepted single journal update payload → 413 `error.413.docs_update_too_large`. | no | 262144 |
| MAX_UPDATES_PER_REQUEST | env | batch ceiling for one append request → 400 `error.400.docs_too_many_updates`. | no | 200 |
| MAX_UPLOAD_BYTES | env | largest accepted upload blob, checked against the DECLARED size at open and the STORED object at finalize → 413 `error.413.docs_upload_too_large`. | no | 1073741824 |
| MAX_EXPORT_BYTES | env | largest body an exporter is allowed to parse in-process → 413 `error.413.docs_export_too_large`. | no | 5242880 |
| WORKSPACE_QUOTA_BYTES | env | per-workspace stored-byte budget (heads + revisions); over it → 507 `error.507.docs_workspace_quota`. `0` disables the quota — an explicit opt-out. | no | 10737418240 |

### Upload session invariants (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| UPLOAD_SESSION_TTL_SECONDS | env | ticket lifetime; a pending session past it can never be finalized (400 `error.400.docs_upload_expired`). 0 = no expiry. | no | 86400 |
| MAX_PENDING_UPLOADS_PER_WORKSPACE | env | ceiling on simultaneously open (pending, unexpired) sessions per workspace → 400 `error.400.docs_too_many_uploads`. | no | 100 |
| UPLOAD_ALLOWED_MIME_TYPES | env | accepted upload MIME types, exact (`image/png`) or type wildcard (`image/*`). Anything not matched — including an upload that declares no type — is refused with 400 `error.400.docs_upload_mime`; `["*/*"]` accepts anything (explicit), `[]` accepts nothing. | no | `stapel_docs.conf.DEFAULT_UPLOAD_MIME_TYPES` (documents, office, images, audio/video; no html/svg/executables) |

### Internal (comm) callers & retention schedule (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| INTERNAL_REQUIRE_CALLER | env | `docs.create_document` must carry an authorized caller (`actor_id`, or a `caller_service` listed below); off is a deliberate trusted-bus choice. | no | True |
| INTERNAL_TRUSTED_SERVICES | env | service names allowed to call `docs.create_document` without a user actor (narrow delegated capability). | no | [] |
| TRASH_PURGE_SCHEDULE | env | crontab kwargs for `stapel_docs.tasks.purge_expired_trash`, exposed to a host beat schedule by `get_docs_beat_schedule()`. | no | {"hour": 4, "minute": 20} |

### Sharing axis (closed defaults; v1 system-check guards E010-E014) (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| SHARING.MODES | env | additional grant sources over the immutable workspace baseline (subset of whitelist \| link). v1 implements neither — non-empty is a system-check ERROR. | no | [] |
| SHARING.RESOLVERS | env | {ref_kind: dotted-path} whitelist subject-reference resolvers — real-but-empty seam; entries are import-validated at check time (E014). | no | {} |
| SHARING.LINK.ANONYMOUS | env | anonymous link redemption. True in v1 = system-check ERROR (E012). | no | false |
| SHARING.LINK.MAX_LEVEL | env | ceiling for minted link level. Above `view` in v1 = system-check ERROR (E013); `manage` is never grantable by any share source. | no | view |
| SHARING.LINK.TTL_DAYS | env | link TTL; None (perpetual) only ever by explicit host choice. | no | 30 |
