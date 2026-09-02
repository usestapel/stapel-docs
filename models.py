"""Models for stapel-docs — the versioning substrate (storage-verdict §1).

The model IS the decided substrate: an append-only per-document update
journal (``DocumentUpdate``, monotonic ``seq``), content-addressed
snapshots/blobs in object storage behind the storage seam, and
``Revision`` pointer rows as the version history. Git is not a layer.

Cross-type invariants (storage-verdict §7.2) this schema carries:

- I1 self-contained revisions: every ``Revision.storage_key`` yields the
  FULL state via ``get_bytes`` alone — never a delta.
- I2 storage closure: content bytes live in ``DocumentUpdate`` rows and
  under ``{PREFIX}/{workspace_id}/{document_id}/`` — nowhere else.
- I3 single monotonic lineage: one ``head_seq`` per document, every write
  serializes through it, no branches.
- I4 attributed writes: journal rows and revisions carry an author
  (FK-less UUID on the journal — survives user erasure by anonymize).

House rules (docs/library-standard.md §3.8): cross-service references are
UUID fields, not FKs; user model only via ``settings.AUTH_USER_MODEL``;
index names <= 30 chars. Trash is ``deleted_at`` soft-delete over pointer
rows; purge destroys rows + journal + objects without rewriting history.
"""
import uuid

from django.conf import settings
from django.db import models


class Folder(models.Model):
    """A node of the workspace folder tree (metadata rows own the tree —
    single owner, no second tree in any storage layer)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace_id = models.UUIDField(db_index=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    name = models.CharField(max_length=255)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "docs_folder"
        constraints = [
            # Sibling name uniqueness among live folders. NULL parents
            # (workspace roots) are compared per-workspace in service code —
            # SQL NULL never equals NULL.
            models.UniqueConstraint(
                fields=["workspace_id", "parent", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="docs_folder_sibling_name",
            ),
        ]

    def __str__(self):
        return self.name


class Document(models.Model):
    """One entity for every document type (design §3.1) — the type slug
    dispatches codec/editor/extractor through the ``DOC_TYPES`` registry."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace_id = models.UUIDField(db_index=True)
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="documents",
    )
    #: Registry slug. Immutable after creation (storage-verdict §7.3):
    #: "convert" = create a new document via the target type's codec.
    type = models.CharField(max_length=32)
    title = models.CharField(max_length=512)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    #: Last applied update (I3). For snapshot-discipline types every
    #: accepted save is ++head_seq; for crdt types every journal append.
    head_seq = models.BigIntegerField(default=0)
    #: seq at which the current head snapshot was assembled.
    snapshot_seq = models.BigIntegerField(default=0)
    #: Storage key of the current head snapshot / blob ("" until first body).
    snapshot_key = models.CharField(max_length=512, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    #: For type=file: the original upload's MIME type (byte-preserved blob).
    mime_type = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "docs_document"
        indexes = [
            models.Index(fields=["workspace_id", "folder"], name="docs_doc_ws_folder"),
        ]

    def __str__(self):
        return self.title


class DocumentUpdate(models.Model):
    """Append-only journal row — an opaque commutative update (crdt types).

    Payload bytes CONTAIN USER TEXT (CRDT inserts carry the characters), so
    journal rows are content, not mechanics: trash purge deletes them and
    GDPR anonymize nulls ``author_id`` (deliberately FK-less)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="updates")
    seq = models.BigIntegerField()
    payload = models.BinaryField()
    author_id = models.UUIDField(null=True, blank=True)
    #: Client-supplied dedup hint (retry hygiene, not correctness — CRDT
    #: updates are idempotent by data type).
    client_id = models.CharField(max_length=64, blank=True, default="")
    client_seq = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "docs_update"
        constraints = [
            models.UniqueConstraint(fields=["document", "seq"], name="docs_update_doc_seq"),
        ]
        indexes = [
            models.Index(fields=["document", "created_at"], name="docs_upd_doc_created"),
        ]

    def __str__(self):
        return f"{self.document_id}@{self.seq}"


class Revision(models.Model):
    """Version-history pointer row: the document's full state at ``seq``,
    stored as a self-contained content-addressed snapshot (I1)."""

    KIND_AUTO = "auto"
    KIND_NAMED = "named"
    KIND_CHOICES = ((KIND_AUTO, "auto"), (KIND_NAMED, "named"))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="revisions")
    seq = models.BigIntegerField()
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_AUTO)
    name = models.CharField(max_length=255, blank=True, default="")
    storage_key = models.CharField(max_length=512)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    size_bytes = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "docs_revision"
        constraints = [
            models.UniqueConstraint(
                fields=["document", "seq", "kind"], name="docs_revision_doc_seq_kind"
            ),
        ]
        ordering = ["-seq", "-created_at"]

    def __str__(self):
        return f"{self.document_id}@{self.seq} ({self.kind})"


class UploadSession(models.Model):
    """Pending direct-to-storage upload of a ``type=file`` blob
    (presigned PUT / multipart; recordings upload-session pattern).

    Under :class:`~stapel_docs.storage.DjangoStorageBackend` the presigned
    URL degrades to a served URL that is not writable — clients on that
    profile use the server-side content PUT instead; the session flow is
    for object-store profiles."""

    STATE_PENDING = "pending"
    STATE_FINALIZED = "finalized"
    STATE_ABORTED = "aborted"
    STATE_CHOICES = (
        (STATE_PENDING, "pending"),
        (STATE_FINALIZED, "finalized"),
        (STATE_ABORTED, "aborted"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace_id = models.UUIDField(db_index=True)
    folder = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    document = models.ForeignKey(
        Document, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    title = models.CharField(max_length=512)
    mime_type = models.CharField(max_length=255, blank=True, default="")
    #: Staging object key; the finalized blob is re-addressed by content
    #: hash under the created document's prefix.
    key = models.CharField(max_length=512)
    multipart_upload_id = models.CharField(max_length=512, blank=True, default="")
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=STATE_PENDING)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    #: Declared blob size; finalize compares it to the STORED object.
    size_bytes = models.BigIntegerField(default=0)
    #: Declared sha256 of the blob (hex, optional). When set, finalize
    #: hashes the stored object and refuses a mismatch — the ticket then
    #: binds the exact bytes, not merely a slot to write into.
    checksum = models.CharField(max_length=64, blank=True, default="")
    #: The ticket is a capability, so it expires; a pending session past
    #: this instant can never be finalized (UPLOAD_SESSION_TTL_SECONDS).
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "docs_upload_session"

    def __str__(self):
        return f"{self.title} ({self.state})"


class Star(models.Model):
    """A user's bookmark on one document OR one folder (drive-spec §3.1).

    Per-user state, so the FK to the user CASCADEs — a star dies with the
    account that placed it (listings ``Favorite`` precedent). The target is
    exactly one of the two FKs, enforced in SQL rather than in service code,
    because a row with both (or neither) has no meaning any reader could
    render. ``workspace_id`` is denormalized so the starred listing scopes
    without joining the target's table twice.

    NULL never equals NULL in SQL, so ``(user, document)`` unique does not
    constrain folder stars, and vice versa — the pair of partial-by-nature
    uniques is what makes "star twice" a no-op on both target kinds.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="docs_stars"
    )
    document = models.ForeignKey(
        Document, null=True, blank=True, on_delete=models.CASCADE, related_name="stars"
    )
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.CASCADE, related_name="stars"
    )
    workspace_id = models.UUIDField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "docs_star"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(document__isnull=False, folder__isnull=True)
                    | models.Q(document__isnull=True, folder__isnull=False)
                ),
                name="docs_star_one_target",
            ),
            models.UniqueConstraint(
                fields=["user", "document"], name="docs_star_user_document"
            ),
            models.UniqueConstraint(
                fields=["user", "folder"], name="docs_star_user_folder"
            ),
        ]
        indexes = [
            models.Index(fields=["user", "workspace_id"], name="docs_star_user_ws"),
        ]

    def __str__(self):
        return f"{self.user_id} ★ {self.document_id or self.folder_id}"


class RecentEntry(models.Model):
    """One row per (user, document) carrying when that user last reached it
    (drive-spec §3.2). Folders are not recents — Drive parity.

    Upserted, never appended: recents are a *position*, not a log, so there
    is nothing here to audit and nothing to keep. The per-user cap
    (``RECENTS_MAX_PER_USER``) is trimmed on write, oldest first.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="docs_recents"
    )
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="recents"
    )
    workspace_id = models.UUIDField(db_index=True)
    accessed_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "docs_recent"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "document"], name="docs_recent_user_document"
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-accessed_at"], name="docs_recent_user_seen"),
        ]

    def __str__(self):
        return f"{self.user_id} @ {self.document_id}"


class Thumbnail(models.Model):
    """A cached server-side image thumbnail of a ``type=file`` document
    (drive-spec §3.6) — derived bytes, never a source of truth.

    The row exists so that invariant I2 (storage closure) survives derived
    objects: ``services.purge_document`` deletes ENUMERATED keys, not a key
    prefix, so a thumbnail nobody registered would outlive the document it
    depicts. ``source_seq`` pins the version the cached image was rendered
    from — a document saved since then re-renders instead of serving a
    picture of content that no longer exists.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="thumbnails"
    )
    #: Longest-edge pixel tier (``thumbnails.THUMBNAIL_TIERS``).
    tier = models.IntegerField()
    #: ``Document.head_seq`` the cached image was rendered from.
    source_seq = models.BigIntegerField(default=0)
    storage_key = models.CharField(max_length=512)
    size_bytes = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "docs_thumbnail"
        constraints = [
            models.UniqueConstraint(
                fields=["document", "tier"], name="docs_thumb_doc_tier"
            ),
        ]

    def __str__(self):
        return f"{self.document_id}@{self.tier}"
