"""Settings namespace for stapel-docs.

All configuration is read through ``docs_settings`` (lazily, at call
time) — never via module-level ``os.getenv`` (values would freeze at import).
Resolution order per key: ``settings.STAPEL_DOCS`` dict -> flat Django
setting of the same name -> environment variable -> default below.

Dotted-path keys listed in ``import_strings`` are resolved with
``import_string`` — the fork-free escape hatch for swappable behavior
(the STORAGE strategy seam).

Registry-style keys (``DOC_TYPES``, ``EXPORTERS``, ``INGEST``,
``SHARING["RESOLVERS"]``) MERGE over built-ins (open registries); ``STORAGE``
REPLACES a single strategy (dotted path).
"""
from stapel_core.conf import AppSettings

#: Closed-by-default sharing axis (tasks/sharing-axis-design.md). v1 ships
#: the config surface with closed defaults; opening any of it before the
#: mechanism exists is a loud system-check error, never a silent no-op.
DEFAULT_SHARING = {
    # Additional grant sources over the immutable workspace baseline.
    # Subset of {"whitelist", "link"}; v1 implements neither — non-empty is
    # a system-check error (docs.E010).
    "MODES": [],
    # {ref_kind: dotted-path} resolver registry for whitelist subject=ref.
    # Real-but-empty seam (sharing-axis §11.3); entries are validated for
    # importability at check time.
    "RESOLVERS": {},
    "LINK": {
        # Anonymous link redemption. True in v1 = system-check error.
        "ANONYMOUS": False,
        # Ceiling for minted link level. "edit" in v1 = system-check error.
        "MAX_LEVEL": "view",
        # Link TTL; None (perpetual) only ever by explicit host choice.
        "TTL_DAYS": 30,
    },
}

#: Upload MIME allowlist shipped as the default (see UPLOAD_ALLOWED_MIME_TYPES).
#: An allowlist, never a blocklist: content types nobody enumerated are the
#: ones an attacker reaches for, so an unlisted type is refused rather than
#: waved through. Active content (text/html, application/xhtml+xml,
#: image/svg+xml, application/javascript) and executables are deliberately
#: absent — a host that serves its media inline would run them in its own
#: origin.
DEFAULT_UPLOAD_MIME_TYPES = [
    # Text and data
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "application/xml",
    "text/xml",
    # Portable documents
    "application/pdf",
    "application/rtf",
    # Office (OOXML, legacy binary, OpenDocument)
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    # Images — enumerated, NOT "image/*": that wildcard would admit
    # image/svg+xml, which is a script document wearing a picture's name.
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/avif",
    "image/heic",
    "image/bmp",
    "image/tiff",
    # Recordings and attachments a workspace document links to
    "audio/mpeg",
    "audio/mp4",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "video/mp4",
    "video/webm",
    "video/quicktime",
]

#: AppSettings-shaped literal dict (capability-config.md §2): a top-level
#: DEFAULTS lets the capabilities.json emitter introspect axis keys/kinds
#: without re-parsing the AppSettings() call.
DEFAULTS = {
        # ── Storage seam ─────────────────────────────────────────────
        # Single-strategy replace seam: every byte of document content
        # I/O goes through this backend (storage-verdict §9.2 — no
        # default_storage/boto3 calls outside storage.py, lint-enforced).
        "STORAGE": "stapel_docs.storage.DjangoStorageBackend",
        # Object-key prefix: {STORAGE_PREFIX}/{workspace_id}/{document_id}/…
        "STORAGE_PREFIX": "docs",
        # S3Backend settings (extra [s3]).
        "S3_ENDPOINT_URL": None,
        "S3_PUBLIC_URL": None,
        "S3_ACCESS_KEY": None,
        "S3_SECRET_KEY": None,
        "S3_REGION": "us-east-1",
        "S3_BUCKET": "stapel-docs",
        "S3_CONNECT_TIMEOUT": 5,
        "S3_READ_TIMEOUT": 15,
        "S3_MAX_ATTEMPTS": 2,
        # Presigned URL lifetimes (opaque to clients — DjangoStorageBackend
        # degrades them to served URLs; never assume S3 URL shape).
        "UPLOAD_URL_EXPIRES_SECONDS": 900,
        "DOWNLOAD_URL_EXPIRES_SECONDS": 3600,
        # A backend that cannot sign a URL can only offer a permanent public
        # one (Django's ``storage.url``): a second read path that outlives
        # the membership it was minted for and never re-enters authorize().
        # Download URLs are therefore REFUSED (503) when the configured
        # backend cannot honour DOWNLOAD_URL_EXPIRES_SECONDS, unless the
        # host says in so many words that its media URLs may act as
        # capabilities. The authorized /content stream serves the same bytes
        # either way, so the closed default costs no functionality.
        "ALLOW_UNEXPIRING_DOWNLOAD_URLS": False,

        # ── Resource limits (hard invariants) ────────────────────────
        # Every byte path has a ceiling the service refuses to cross, so a
        # single caller can neither exhaust the object store nor park an
        # unbounded body in worker memory. 0 disables an individual limit
        # (an explicit host decision, never the shipped default).
        # Largest accepted snapshot body (content PUT, create-with-body).
        "MAX_BODY_BYTES": 10 * 1024 * 1024,
        # Largest accepted single journal update payload, and the batch cap
        # per append request — the crdt feed is otherwise unbounded.
        "MAX_UPDATE_BYTES": 256 * 1024,
        "MAX_UPDATES_PER_REQUEST": 200,
        # Largest accepted upload blob (declared at open, re-checked
        # against the STORED object at finalize).
        "MAX_UPLOAD_BYTES": 1024 * 1024 * 1024,
        # Bodies above this are refused by the export renderer: exporters
        # parse content in-process, so their input needs its own ceiling.
        "MAX_EXPORT_BYTES": 5 * 1024 * 1024,
        # Per-workspace stored-byte budget (document heads + revisions).
        # The only limit in this block that used to ship off, which made a
        # single workspace's growth bounded by the object store's invoice
        # instead of by anything the service enforces. 10 GiB is a ceiling
        # a real workspace does not reach by accident and an operator
        # raises on purpose; 0 disables the quota entirely — an explicit
        # opt-out, never the shipped default.
        "WORKSPACE_QUOTA_BYTES": 10 * 1024 * 1024 * 1024,

        # ── Upload session invariants ────────────────────────────────
        # A ticket is a capability: it expires, it belongs to the user who
        # opened it, and only that user (or a workspace manager) may spend
        # it exactly once.
        "UPLOAD_SESSION_TTL_SECONDS": 24 * 3600,
        # Ceiling on simultaneously open (pending, unexpired) sessions per
        # workspace — bounds staging objects nobody ever finalizes.
        "MAX_PENDING_UPLOADS_PER_WORKSPACE": 100,
        # Accepted upload MIME types. Entries are exact ("image/png") or a
        # type wildcard ("image/*"); an upload that declares no type at all
        # is unknown content and is refused like any other type outside the
        # list. The shipped list is the documents-and-attachments set a
        # workspace actually stores; what it leaves out is what a host
        # serving MEDIA_URL inline would execute in its own origin
        # (text/html, image/svg+xml, application/javascript) or hand a user
        # to run (installers, archives of them). Widen it deliberately —
        # ["*/*"] accepts anything, an explicit host decision, and [] (or
        # any list without a match) accepts nothing.
        "UPLOAD_ALLOWED_MIME_TYPES": DEFAULT_UPLOAD_MIME_TYPES,

        # ── Internal (comm) callers ──────────────────────────────────
        # docs.create_document writes into a workspace on somebody's
        # behalf, so the payload must name that somebody: `actor_id` is
        # authorized exactly like an HTTP caller (docs.edit in the target
        # workspace, through the same choke point). A service with no user
        # actor is only ever accepted when the host lists it below — a
        # narrow delegated capability, never an open door. Turning
        # REQUIRE_CALLER off is a deliberate single-tenant/trusted-bus
        # decision, and it is the host's to make, not the default.
        "INTERNAL_REQUIRE_CALLER": True,
        "INTERNAL_TRUSTED_SERVICES": [],

        # ── Retention schedule ───────────────────────────────────────
        # Trash retention only exists if something runs it: this is the
        # cron for stapel_docs.tasks.purge_expired_trash, exposed to a host
        # beat schedule by get_docs_beat_schedule().
        "TRASH_PURGE_SCHEDULE": {"hour": 4, "minute": 20},

        # ── Document types (open merge registry) ─────────────────────
        # {slug: dotted-path to a DocTypeSpec | None to remove a builtin}.
        "DOC_TYPES": {},

        # ── Journal / revisions ──────────────────────────────────────
        # Journal rows with seq <= snapshot_seq - REPLAY_WINDOW are
        # compacted away (chat-pattern replay window).
        "REPLAY_WINDOW": 500,
        # A snapshot save mints an `auto` Revision when the newest revision
        # is older than this many seconds (0 = revision on every save).
        # Named revisions are always minted on request.
        "AUTO_REVISION_INTERVAL_SECONDS": 300,

        # ── Tree / trash ─────────────────────────────────────────────
        "FOLDER_MAX_DEPTH": 10,
        # Soft-deleted items become purgeable after this many days; purge
        # destroys rows, journal and objects irreversibly (verdict §9.3).
        "TRASH_RETENTION_DAYS": 30,

        # ── Exporters (open merge registry) ──────────────────────────
        # {format: dotted-path}; builtin: "pdf" (extra [pdf], fpdf2).
        "EXPORTERS": {},

        # ── Ingest seam (open merge registry) ────────────────────────
        # {action_name: dotted-path mapper payload -> create_document
        # kwargs} for hosts that want event-driven ingest without writing
        # a subscriber. Canonical week-1 path remains product glue calling
        # the docs.create_document Function.
        "INGEST": {},

        # ── Sharing axis (closed defaults, v1 guards) ────────────────
        "SHARING": DEFAULT_SHARING,
}

docs_settings = AppSettings(
    "STAPEL_DOCS",
    defaults=DEFAULTS,
    import_strings=("STORAGE",),
)

__all__ = [
    "docs_settings",
    "DEFAULT_SHARING",
    "DEFAULT_UPLOAD_MIME_TYPES",
    "DEFAULTS",
]
