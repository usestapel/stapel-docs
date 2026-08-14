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
        # Per-workspace stored-byte budget; 0 = no quota (host opt-in).
        "WORKSPACE_QUOTA_BYTES": 0,

        # ── Upload session invariants ────────────────────────────────
        # A ticket is a capability: it expires, it belongs to the user who
        # opened it, and only that user (or a workspace manager) may spend
        # it exactly once.
        "UPLOAD_SESSION_TTL_SECONDS": 24 * 3600,
        # Ceiling on simultaneously open (pending, unexpired) sessions per
        # workspace — bounds staging objects nobody ever finalizes.
        "MAX_PENDING_UPLOADS_PER_WORKSPACE": 100,
        # Accepted upload MIME types; [] = any. Entries are exact
        # ("image/png") or a type wildcard ("image/*").
        "UPLOAD_ALLOWED_MIME_TYPES": [],

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

__all__ = ["docs_settings", "DEFAULT_SHARING", "DEFAULTS"]
