"""Service layer of stapel-docs — every mutation and its invariants.

The decided substrate (storage-verdict §0): content-addressed snapshots in
object storage behind ``get_storage()``, an append-only journal for crdt
types, ``Revision`` pointer rows as history. Invariants I1-I4 (§7.2) are
enforced here: one monotonic ``head_seq`` per document with every write
under ``select_for_update``; revisions always full snapshots; purge =
rows + journal + objects, O(document); emits only inside the mutating
transaction (outbox canon).
"""
from __future__ import annotations

import logging
import uuid as uuid_module
from contextlib import contextmanager
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Min, Sum
from django.utils import timezone
from stapel_core.django.api.errors import ERR_400_BAD_REQUEST, ERR_404_NOT_FOUND

from . import events, realtime
from .conf import docs_settings
from .doc_types import CODEC_YJS, COLLAB_CRDT, get_doc_types
from .errors import (
    ERR_400_DUPLICATE_NAME,
    ERR_400_FOLDER_CYCLE,
    ERR_400_FOLDER_DEPTH,
    ERR_400_INVALID_CRDT_PAYLOAD,
    ERR_400_NOT_TRASHED,
    ERR_400_SHARE_LEVEL,
    ERR_400_SHARE_MODE_DISABLED,
    ERR_400_SHARE_REF_KIND,
    ERR_400_SHARE_SUBJECT,
    ERR_400_THUMBNAIL_TIER,
    ERR_400_THUMBNAIL_UNSUPPORTED,
    ERR_400_TOO_MANY_UPDATES,
    ERR_400_TOO_MANY_UPLOADS,
    ERR_400_TYPE_NOT_EDITABLE,
    ERR_400_UNKNOWN_TYPE,
    ERR_400_UPDATES_NOT_CRDT,
    ERR_400_UPLOAD_EXPIRED,
    ERR_400_UPLOAD_MIME,
    ERR_400_UPLOAD_MISMATCH,
    ERR_400_UPLOAD_STATE,
    ERR_400_UPLOAD_UNMEASURABLE,
    ERR_403_FORBIDDEN,
    ERR_404_DOCUMENT,
    ERR_404_FOLDER,
    ERR_404_REVISION,
    ERR_404_SHARE_ACCESS,
    ERR_404_SHARE_LINK,
    ERR_404_UPLOAD,
    ERR_409_SEQ_CONFLICT,
    ERR_413_BODY_TOO_LARGE,
    ERR_413_UPDATE_TOO_LARGE,
    ERR_413_UPLOAD_TOO_LARGE,
    ERR_503_DOWNLOAD_URL,
    ERR_503_THUMBNAILS,
    ERR_507_WORKSPACE_QUOTA,
)
from .models import (
    Document,
    DocumentUpdate,
    Folder,
    RecentEntry,
    Revision,
    Star,
    Thumbnail,
    UploadSession,
)
from .storage import content_hash, document_prefix, get_storage, snapshot_key

logger = logging.getLogger(__name__)

#: Metadata key marking a document whose upload has not finalized yet —
#: such documents are excluded from listings (removed on finalize).
UPLOAD_PENDING_KEY = "upload_pending"


class DocsError(Exception):
    """Service-level refusal carrying the HTTP mapping for the view layer."""

    def __init__(self, status: int, error_key: str, params: dict | None = None):
        super().__init__(error_key)
        self.status = status
        self.error_key = error_key
        self.params = params or {}


class CallerNotAuthorized(DocsError):
    """A comm caller could not be bound to a workspace-authorized actor.

    The comm surface has no session, so authority has to be carried in the
    payload and checked here; without this an internal caller could create
    documents in any workspace, owned by any user."""

    def __init__(self, params: dict | None = None):
        super().__init__(403, ERR_403_FORBIDDEN, params)


class SeqConflict(DocsError):
    """Optimistic-lock loss: a newer save won. Carries the winning save."""

    def __init__(self, *, head_seq: int, saved_by, saved_at):
        super().__init__(
            409,
            ERR_409_SEQ_CONFLICT,
            {"head_seq": head_seq, "saved_by": saved_by, "saved_at": saved_at},
        )


def effective_spec(document):
    """The document's type spec, or None when it vanished from the registry
    (degrades to file behavior — read-only, never unreadable; verdict §7.3)."""
    return get_doc_types().get(document.type)


def content_mime(document) -> str:
    """Content-Type for serving the body: the spec's mime for editable
    types, the stored original's mime for ``file``/vanished types."""
    spec = effective_spec(document)
    if spec is not None and spec.slug != "file" and spec.mime_type:
        if spec.collab == COLLAB_CRDT and spec.codec == CODEC_YJS:
            # The stored body of a yjs-codec type is the BINARY Y state,
            # not the type's logical text mime — serving it as text/* would
            # mis-render in every client that trusts Content-Type. The
            # human-readable form is the exporters' job.
            return "application/octet-stream"
        mime = spec.mime_type
        # Editable text bodies are utf-8 by contract; without an explicit
        # charset, HTTP clients default text/* to latin-1 and mis-render.
        if mime.startswith("text/"):
            mime += "; charset=utf-8"
        return mime
    return document.mime_type or "application/octet-stream"


# ── Object-store transactions ────────────────────────────────────────
#
# Object storage cannot roll back with the database, so the two stores are
# reconciled by ORDERING, not by hope:
#
#   writes  happen immediately and are COMPENSATED (deleted) when the
#           surrounding block fails — a rolled-back save leaves no orphan;
#   deletes are DEFERRED to ``transaction.on_commit`` — a rollback can then
#           never destroy an object a surviving row still points at.
#
# The asymmetry is deliberate: a leaked object costs storage and is swept
# later, a deleted object a live row points at is unrecoverable data loss.


class StorageTransaction:
    """Object-store side of a database transaction (see the note above)."""

    def __init__(self):
        # Keys this block created (candidates for compensation) and keys it
        # wants gone once the database side is durable.
        self._created: list[str] = []
        self._deferred_deletes: list[str] = []

    def put(self, key: str, data: bytes, *, content_type: str) -> bool:
        """Write an object now. Returns whether it already existed."""
        storage = get_storage()
        existed, _ = storage.head_object(key)
        storage.put_bytes(key, data, content_type=content_type)
        if not existed:
            # Only an object THIS block created may be compensated away:
            # keys are content-addressed, so a pre-existing object is
            # someone else's history, not our orphan.
            self._created.append(key)
        return existed

    def delete(self, key: str) -> None:
        """Schedule an object deletion for after the commit."""
        self._deferred_deletes.append(key)

    def flush_deletes(self) -> None:
        storage = get_storage()
        for key in self._deferred_deletes:
            try:
                storage.delete_object(key)
            except Exception:  # pragma: no cover — best effort after commit
                logger.exception("docs: deferred delete failed for key %s", key)

    def compensate(self) -> None:
        storage = get_storage()
        for key in reversed(self._created):
            try:
                storage.delete_object(key)
            except Exception:  # pragma: no cover — compensation is best effort
                logger.exception("docs: compensation failed for key %s", key)


@contextmanager
def storage_transaction():
    """Run a block whose object-store effects follow the database outcome."""
    tx = StorageTransaction()
    try:
        yield tx
    except Exception:
        tx.compensate()
        raise
    # Outside an atomic block on_commit runs immediately, which is the same
    # guarantee: the database side is already durable.
    transaction.on_commit(tx.flush_deletes)


# ── Resource invariants (limits and quota) ───────────────────────────
#
# Every accepted byte is bounded twice: by a per-object ceiling (a body, an
# update, a blob) and by the workspace's own budget. Limits are settings,
# never literals, and a limit of 0 disables that single ceiling — an
# explicit host decision, not the shipped default.


def resource_limit(name: str) -> int:
    """An integer limit from the STAPEL_DOCS namespace (0 = disabled)."""
    return int(getattr(docs_settings, name))


def assert_body_size(body: bytes) -> None:
    """Ceiling for a snapshot body (content PUT, create-with-body)."""
    limit = resource_limit("MAX_BODY_BYTES")
    if limit and len(body) > limit:
        raise DocsError(413, ERR_413_BODY_TOO_LARGE, {"limit_bytes": limit, "size_bytes": len(body)})


def assert_body_mutable(document) -> None:
    """Refuse a generic body write to a type that owns its own write path.

    ``type=file`` bodies arrive through an upload session (where size, MIME
    and quota policy live), and a type whose spec vanished from the
    registry is read-only by contract (storage-verdict §7.3) — without this
    the content PUT is a second door around both rules.
    """
    spec = effective_spec(document)
    if spec is None or not spec.body_mutable:
        raise DocsError(400, ERR_400_TYPE_NOT_EDITABLE)


def assert_crdt_body(document_or_spec, body: bytes) -> None:
    """Refuse a snapshot body that is not a Y update, for yjs-codec types.

    The snapshot of a crdt document IS the CRDT state: a text body stored
    as "snapshot" would corrupt the discipline — clients holding older Y
    docs could never converge on it, because item identity would be gone.
    Codec-scoped on purpose: snapshot types and host-codec crdt types are
    untouched, and a yjs-codec type registered on a deployment without
    pycrdt (a host's own doing) skips the check it cannot run.
    """
    spec = document_or_spec
    if hasattr(spec, "type"):
        spec = effective_spec(spec)
    if spec is None or spec.collab != COLLAB_CRDT or spec.codec != CODEC_YJS:
        return
    from . import crdt

    if not crdt.available():
        return
    if not crdt.is_valid_update(body):
        raise DocsError(400, ERR_400_INVALID_CRDT_PAYLOAD)


def workspace_usage_bytes(workspace_id) -> int:
    """Stored bytes charged to a workspace: every document head plus every
    revision snapshot (dedup by content hash is not modelled — the quota
    counts the pessimistic figure, which is the one an operator budgets)."""
    heads = Document.objects.filter(workspace_id=workspace_id).aggregate(
        total=Sum("size_bytes")
    )["total"] or 0
    revisions = Revision.objects.filter(
        document__workspace_id=workspace_id
    ).aggregate(total=Sum("size_bytes"))["total"] or 0
    return int(heads) + int(revisions)


def assert_quota(workspace_id, added_bytes: int) -> None:
    """Refuse a write that would push the workspace past its byte budget."""
    quota = resource_limit("WORKSPACE_QUOTA_BYTES")
    if quota <= 0 or added_bytes <= 0:
        return
    used = workspace_usage_bytes(workspace_id)
    if used + added_bytes > quota:
        raise DocsError(
            507,
            ERR_507_WORKSPACE_QUOTA,
            {"quota_bytes": quota, "used_bytes": used},
        )


def _mime_allowed(mime: str) -> bool:
    """Is this declared content type on the upload allowlist?

    An allowlist that is empty allows nothing. "Accept anything" is spelled
    ``["*/*"]`` — a host saying so on purpose — because the alternative
    reading, where the absence of a policy IS the policy, turns a config
    key nobody filled in into an open door for 1 GiB of arbitrary bytes.
    An upload that declares no type is unknown content, and unknown is not
    on any allowlist."""
    allowed = [str(entry).strip().lower() for entry in (docs_settings.UPLOAD_ALLOWED_MIME_TYPES or [])]
    if "*/*" in allowed:
        return True
    mime = (mime or "").split(";")[0].strip().lower()
    if not mime:
        return False
    for entry in allowed:
        if entry == mime:
            return True
        if entry.endswith("/*") and mime.startswith(entry[:-1]):
            return True
    return False


# ── Scoped lookups ───────────────────────────────────────────────────


def get_live_document(document_id) -> Document:
    doc = Document.objects.filter(pk=document_id, deleted_at__isnull=True).first()
    if doc is None:
        raise DocsError(404, ERR_404_DOCUMENT)
    return doc


def get_trashed_document(document_id) -> Document:
    doc = Document.objects.filter(pk=document_id, deleted_at__isnull=False).first()
    if doc is None:
        raise DocsError(404, ERR_404_DOCUMENT)
    return doc


def get_live_folder(folder_id, workspace_id=None) -> Folder:
    qs = Folder.objects.filter(pk=folder_id, deleted_at__isnull=True)
    if workspace_id is not None:
        qs = qs.filter(workspace_id=workspace_id)
    folder = qs.first()
    if folder is None:
        raise DocsError(404, ERR_404_FOLDER)
    return folder


def get_trashed_folder(folder_id) -> Folder:
    folder = Folder.objects.filter(pk=folder_id, deleted_at__isnull=False).first()
    if folder is None:
        raise DocsError(404, ERR_404_FOLDER)
    return folder


def get_revision(document, revision_id) -> Revision:
    revision = document.revisions.filter(pk=revision_id).first()
    if revision is None:
        raise DocsError(404, ERR_404_REVISION)
    return revision


def get_upload_session(upload_id) -> UploadSession:
    session = UploadSession.objects.filter(pk=upload_id).first()
    if session is None:
        raise DocsError(404, ERR_404_UPLOAD)
    return session


# ── Folder tree ──────────────────────────────────────────────────────


def _folder_depth(folder) -> int:
    """Number of nodes from the workspace root down to *folder*, inclusive."""
    depth = 0
    node = folder
    while node is not None:
        depth += 1
        node = node.parent
    return depth


def _subtree_height(folder) -> int:
    """Longest live chain below *folder*, inclusive of it."""
    children = list(folder.children.filter(deleted_at__isnull=True))
    if not children:
        return 1
    return 1 + max(_subtree_height(child) for child in children)


def _subtree_folder_ids(folder, *, live_only: bool) -> list:
    """ids of *folder* and every descendant (BFS over Folder rows)."""
    ids = [folder.id]
    frontier = [folder.id]
    while frontier:
        qs = Folder.objects.filter(parent_id__in=frontier)
        if live_only:
            qs = qs.filter(deleted_at__isnull=True)
        frontier = list(qs.values_list("id", flat=True))
        ids.extend(frontier)
    return ids


def _assert_sibling_name_free(workspace_id, parent, name, exclude_pk=None):
    """Duplicate live sibling names are refused in code — SQL cannot compare
    NULL parents (workspace roots), the model docstring says why."""
    qs = Folder.objects.filter(
        workspace_id=workspace_id, parent=parent, name=name, deleted_at__isnull=True
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise DocsError(400, ERR_400_DUPLICATE_NAME)


def list_folders(workspace_id, parent_id=..., limit: int = 200, *, user=None):
    qs = Folder.objects.filter(workspace_id=workspace_id, deleted_at__isnull=True)
    if parent_id is None:
        qs = qs.filter(parent__isnull=True)
    elif parent_id is not ...:
        qs = qs.filter(parent_id=parent_id)
    return with_stars(qs.order_by("name"), user, target="folder")[:limit]


def create_folder(*, workspace_id, name, parent_id=None, user=None) -> Folder:
    parent = None
    if parent_id is not None:
        parent = get_live_folder(parent_id, workspace_id=workspace_id)
        if _folder_depth(parent) + 1 > int(docs_settings.FOLDER_MAX_DEPTH):
            raise DocsError(400, ERR_400_FOLDER_DEPTH)
    _assert_sibling_name_free(workspace_id, parent, name)
    return Folder.objects.create(
        workspace_id=workspace_id, parent=parent, name=name, created_by=user
    )


def rename_folder(folder, name) -> Folder:
    _assert_sibling_name_free(folder.workspace_id, folder.parent, name, exclude_pk=folder.pk)
    folder.name = name
    folder.save(update_fields=["name", "updated_at"])
    return folder


def move_folder(folder, parent_id) -> Folder:
    parent = None
    if parent_id is not None:
        parent = get_live_folder(parent_id, workspace_id=folder.workspace_id)
        node = parent
        while node is not None:
            if node.pk == folder.pk:
                raise DocsError(400, ERR_400_FOLDER_CYCLE)
            node = node.parent
        if _folder_depth(parent) + _subtree_height(folder) > int(
            docs_settings.FOLDER_MAX_DEPTH
        ):
            raise DocsError(400, ERR_400_FOLDER_DEPTH)
    _assert_sibling_name_free(folder.workspace_id, parent, folder.name, exclude_pk=folder.pk)
    folder.parent = parent
    folder.save(update_fields=["parent", "updated_at"])
    return folder


def trash_folder(folder) -> None:
    """Soft-delete the whole live subtree, documents included."""
    with transaction.atomic():
        now = timezone.now()
        folder_ids = _subtree_folder_ids(folder, live_only=True)
        docs = Document.objects.filter(
            folder_id__in=folder_ids, deleted_at__isnull=True
        )
        for doc in docs:
            events.emit_document_deleted(doc)
        docs.update(deleted_at=now)
        Folder.objects.filter(id__in=folder_ids, deleted_at__isnull=True).update(
            deleted_at=now
        )


def restore_folder(folder) -> Folder:
    """Untrash the subtree; restored documents are re-announced."""
    with transaction.atomic():
        folder_ids = _subtree_folder_ids(folder, live_only=False)
        Folder.objects.filter(id__in=folder_ids, deleted_at__isnull=False).update(
            deleted_at=None
        )
        docs = Document.objects.filter(
            folder_id__in=folder_ids, deleted_at__isnull=False
        )
        for doc in docs:
            events.emit_document_created(doc)
        docs.update(deleted_at=None)
    folder.refresh_from_db()
    return folder


# ── Documents ────────────────────────────────────────────────────────


def list_documents(
    workspace_id, *, folder_id=None, type=None, q=None, limit: int = 200, user=None
):
    qs = Document.objects.filter(
        workspace_id=workspace_id, deleted_at__isnull=True
    ).exclude(metadata__has_key=UPLOAD_PENDING_KEY)
    if folder_id is not None:
        qs = qs.filter(folder_id=folder_id)
    if type:
        qs = qs.filter(type=type)
    if q:
        qs = qs.filter(title__icontains=q)
    return with_stars(qs.order_by("-created_at"), user, target="document")[:limit]


def _ensure_folder_path(workspace_id, path: str, user=None):
    """Idempotently materialize a ``/Meetings/2026-08`` chain of live
    folders (the ingest seam, design §6). Returns the leaf (None for "/")."""
    parent = None
    depth = 0
    max_depth = int(docs_settings.FOLDER_MAX_DEPTH)
    for name in (segment for segment in path.split("/") if segment):
        depth += 1
        if depth > max_depth:
            raise DocsError(400, ERR_400_FOLDER_DEPTH)
        folder = Folder.objects.filter(
            workspace_id=workspace_id, parent=parent, name=name, deleted_at__isnull=True
        ).first()
        if folder is None:
            folder = Folder.objects.create(
                workspace_id=workspace_id, parent=parent, name=name, created_by=user
            )
        parent = folder
    return parent


def create_document(
    *,
    workspace_id,
    type,
    title,
    folder_id=None,
    folder_path=None,
    metadata=None,
    body=None,
    mime_type="",
    user=None,
    owner=None,
) -> Document:
    """Create a document; HTTP passes ``folder_id``/``user``, the comm
    ingest seam passes ``folder_path``/``owner``. ``body`` may be str or
    bytes; absent means "no body yet" (the spec's empty body serves)."""
    spec = get_doc_types().get(type)
    if spec is None:
        raise DocsError(400, ERR_400_UNKNOWN_TYPE)
    acting = owner if owner is not None else user
    folder = None
    if folder_id is not None:
        folder = get_live_folder(folder_id, workspace_id=workspace_id)
    elif folder_path:
        folder = _ensure_folder_path(workspace_id, folder_path, user=acting)
    if isinstance(body, str):
        body = body.encode("utf-8")
    if body is not None:
        if not spec.body_mutable:
            raise DocsError(400, ERR_400_TYPE_NOT_EDITABLE)
        assert_body_size(body)
        assert_crdt_body(spec, body)
        assert_quota(workspace_id, len(body))
    metadata = dict(metadata or {})
    # Only the upload flow may mark a document pending.
    metadata.pop(UPLOAD_PENDING_KEY, None)
    with storage_transaction() as stx, transaction.atomic():
        document = Document.objects.create(
            workspace_id=workspace_id,
            folder=folder,
            type=type,
            title=title,
            owner=acting,
            mime_type=mime_type or "",
            metadata=metadata,
        )
        if body is not None:
            # Initial body rides the regular save path (snapshot + auto
            # revision); document.created announces it, no separate updated.
            _save_snapshot(document, body, storage_tx=stx, user=acting, emit_updated=False)
        events.emit_document_created(document)
    return document


def update_document(document, *, title=None, metadata=None) -> Document:
    fields = []
    if title is not None:
        document.title = title
        fields.append("title")
    if metadata is not None:
        metadata = dict(metadata)
        metadata.pop(UPLOAD_PENDING_KEY, None)
        document.metadata = metadata
        fields.append("metadata")
    if fields:
        document.save(update_fields=fields + ["updated_at"])
    return document


def move_document(document, folder_id) -> Document:
    folder = None
    if folder_id is not None:
        folder = get_live_folder(folder_id, workspace_id=document.workspace_id)
    document.folder = folder
    document.save(update_fields=["folder", "updated_at"])
    return document


def trash_document(document) -> None:
    with transaction.atomic():
        events.emit_document_deleted(document)
        document.deleted_at = timezone.now()
        document.save(update_fields=["deleted_at", "updated_at"])
        # An open socket loses the row its access rests on: kick everyone
        # (best-effort; a reconnect re-enters authorize() and finds a 404).
        doc_id = document.pk
        transaction.on_commit(
            lambda: realtime.revoke_document(doc_id, reason="document_trashed")
        )


def restore_document(document) -> Document:
    with transaction.atomic():
        document.deleted_at = None
        document.save(update_fields=["deleted_at", "updated_at"])
        events.emit_document_created(document)
    return document


# ── Content (the versioning heart) ───────────────────────────────────


def read_content(document, *, user=None) -> tuple[bytes, str, int]:
    """(body bytes, mime, head_seq). No body yet -> the spec's empty body
    at head_seq 0 (b"" for vanished types).

    ``user`` marks the document as recently opened for that user (drive-spec
    §3.2). The recents upsert lives HERE and not in the view because a
    document is "recent" when its bytes were served, whichever caller served
    them — a second read path added later inherits the behavior instead of
    forgetting it. Callers that read bytes for a machine (export rendering,
    thumbnails, revision replay) pass no user and leave no trace.
    """
    if document.snapshot_key:
        body = get_storage().get_bytes(document.snapshot_key)
    else:
        spec = effective_spec(document)
        body = spec.empty_body if spec is not None else b""
    touch_recent(document, user)
    return body, content_mime(document), document.head_seq


def _auto_revision_due(document) -> bool:
    interval = int(docs_settings.AUTO_REVISION_INTERVAL_SECONDS)
    if interval == 0:
        return True
    newest = document.revisions.order_by("-created_at").first()
    if newest is None:
        return True
    return newest.created_at <= timezone.now() - timedelta(seconds=interval)


def _compact_journal(document) -> None:
    """Chat-pattern compaction: after a snapshot at seq S, journal rows at
    seq <= S - REPLAY_WINDOW are unreachable by replay and die."""
    window = int(docs_settings.REPLAY_WINDOW)
    DocumentUpdate.objects.filter(
        document=document, seq__lte=document.snapshot_seq - window
    ).delete()


def _save_snapshot(
    document,
    body: bytes,
    *,
    storage_tx: StorageTransaction,
    user=None,
    force_revision=False,
    emit_updated=True,
):
    """The single snapshot-save path (PUT content, create-with-body, revision
    restore, all types). Caller holds the row lock (or just created the row)
    and owns the surrounding storage transaction.
    Returns the minted auto Revision or None."""
    return _write_snapshot(
        document,
        body,
        storage_tx=storage_tx,
        seq=document.head_seq + 1,
        advance_head=True,
        user=user,
        force_revision=force_revision,
        emit_updated=emit_updated,
    )


def _write_snapshot(
    document,
    body: bytes,
    *,
    storage_tx: StorageTransaction,
    seq: int,
    advance_head: bool,
    user=None,
    force_revision=False,
    emit_updated=True,
):
    """Store *body* as the snapshot at *seq* — the shared core of
    :func:`_save_snapshot` (a SAVE: ``seq = head_seq + 1``, head advances)
    and :func:`assemble_crdt_snapshot` (a MATERIALIZATION of journal rows
    the head already counts: ``seq`` is an existing journal position and
    the head does not move). Everything else — content-addressed put,
    auto-revision minting, orphan cleanup, compaction, emits — is one code
    path so the two writers can never disagree about the storage rules.
    Returns the minted auto Revision or None."""
    key = snapshot_key(document.workspace_id, document.id, content_hash(body))
    prev_key, prev_size = document.snapshot_key, document.size_bytes

    # Content-addressed: an existing object means zero new stored bytes.
    already_stored = storage_tx.put(key, body, content_type=content_mime(document))
    added = 0 if already_stored else len(body)

    revision = None
    # Mutation + outbox rows share this atomic block (outbox canon — see
    # module docstring). The callers already hold a wider
    # transaction.atomic(); nesting here is a safe savepoint (per
    # stapel_core.comm.mutate_and_emit's documented nesting guarantee) and
    # makes this helper self-sufficient for emit-check's lexical scan.
    with transaction.atomic():
        fields = ["snapshot_seq", "snapshot_key", "size_bytes", "updated_at"]
        if advance_head:
            document.head_seq = seq
            fields.insert(0, "head_seq")
        document.snapshot_seq = seq
        document.snapshot_key = key
        document.size_bytes = len(body)
        document.save(update_fields=fields)

        if force_revision or _auto_revision_due(document):
            revision = Revision.objects.create(
                document=document,
                seq=seq,
                kind=Revision.KIND_AUTO,
                storage_key=key,
                created_by=user,
                size_bytes=len(body),
            )

        # Orphan cleanup: the previous snapshot dies iff no Revision points at
        # it (content-addressed keys make identical bodies dedup for free).
        freed = 0
        if (
            prev_key
            and prev_key != key
            and not Revision.objects.filter(document=document, storage_key=prev_key).exists()
        ):
            storage_tx.delete(prev_key)
            freed = prev_size

        _compact_journal(document)

        if emit_updated:
            events.emit_document_updated(document)
        delta = added - freed
        if delta:
            events.emit_storage_changed(document.workspace_id, delta)
    return revision


def _winning_save(document) -> tuple:
    """(saved_by, saved_at) of the save the caller lost to — best available
    attribution: the newest revision, else the row's own updated_at."""
    newest = document.revisions.order_by("-created_at").first()
    if newest is not None:
        saved_by = str(newest.created_by_id) if newest.created_by_id else None
        return saved_by, newest.created_at.isoformat()
    return None, document.updated_at.isoformat()


def save_content(
    document_id,
    body: bytes,
    *,
    expected_seq=None,
    user=None,
    force_revision=False,
    require_mutable_type=True,
):
    """Optimistic-lock snapshot save. ``expected_seq=None`` skips the check
    (revision restore — it serializes on the same lock). Returns
    (document, revision-or-None).

    ``require_mutable_type=False`` is for replaying bytes this service
    already stored (revision restore): those passed the type's own write
    policy when they were first accepted.
    """
    assert_body_size(body)
    with storage_transaction() as stx, transaction.atomic():
        document = (
            Document.objects.select_for_update()
            .filter(pk=document_id, deleted_at__isnull=True)
            .first()
        )
        if document is None:
            raise DocsError(404, ERR_404_DOCUMENT)
        if require_mutable_type:
            assert_body_mutable(document)
            # A client save into a yjs-codec type must be a Y state; bytes
            # this service already stored (revision restore) passed the
            # check when first accepted.
            assert_crdt_body(document, body)
        assert_quota(document.workspace_id, len(body) - document.size_bytes)
        if expected_seq is not None and expected_seq != document.head_seq:
            saved_by, saved_at = _winning_save(document)
            raise SeqConflict(
                head_seq=document.head_seq, saved_by=saved_by, saved_at=saved_at
            )
        revision = _save_snapshot(
            document, body, storage_tx=stx, user=user, force_revision=force_revision
        )
    # After the commit, and only for an ACCEPTED save: a rejected write
    # (conflict, quota, type) never happened, so it never made the document
    # recent either.
    touch_recent(document, user)
    return document, revision


# ── Update journal (crdt discipline) ─────────────────────────────────


def append_updates(document_id, updates: list[bytes], *, client_id="", client_seq=None, principal=None) -> int:
    """Append a batch of opaque commutative updates at ++head_seq each.
    Journal appends do NOT emit document.updated (bus economy, design §6) —
    the snapshot assembly is the debounce point that announces the document.
    Returns the new head_seq.

    Store-first delivery (0.7.0): AFTER the commit, one realtime frame per
    journal row goes out on ``docs:doc:<id>`` — best-effort, because the
    row is the durable thing and a subscriber that misses a frame replays
    it (``?since=`` or the socket's resume). And when the journal outruns
    ``CRDT_ASSEMBLE_UPDATE_INTERVAL``, the commit also triggers a server
    snapshot assembly for yjs-codec types (inline, the repo's
    opportunistic-work canon — the same posture as recents trimming)."""
    batch_limit = resource_limit("MAX_UPDATES_PER_REQUEST")
    if batch_limit and len(updates) > batch_limit:
        raise DocsError(400, ERR_400_TOO_MANY_UPDATES, {"limit": batch_limit})
    update_limit = resource_limit("MAX_UPDATE_BYTES")
    if update_limit:
        for payload in updates:
            if len(payload) > update_limit:
                raise DocsError(413, ERR_413_UPDATE_TOO_LARGE, {"limit_bytes": update_limit})
    with transaction.atomic():
        document = (
            Document.objects.select_for_update()
            .filter(pk=document_id, deleted_at__isnull=True)
            .first()
        )
        if document is None:
            raise DocsError(404, ERR_404_DOCUMENT)
        spec = effective_spec(document)
        if spec is None or spec.collab != COLLAB_CRDT:
            raise DocsError(400, ERR_400_UPDATES_NOT_CRDT)
        if spec.codec == CODEC_YJS:
            # Apply-validate at the boundary: a corrupt payload accepted
            # here would be a 400 turned into an assembly that can never
            # complete. Host-codec crdt types stay fully opaque.
            for payload in updates:
                assert_crdt_body(spec, payload)
        # Retry hygiene: a batch the client already delivered is a no-op.
        if (
            client_id
            and client_seq is not None
            and DocumentUpdate.objects.filter(
                document=document, client_id=client_id, client_seq=client_seq
            ).exists()
        ):
            return document.head_seq
        author_id = principal.user_id if principal is not None else None
        frames = []
        for payload in updates:
            document.head_seq += 1
            DocumentUpdate.objects.create(
                document=document,
                seq=document.head_seq,
                payload=payload,
                author_id=author_id,
                client_id=client_id or "",
                client_seq=client_seq,
            )
            frames.append(
                (
                    document.head_seq,
                    realtime.update_payload(payload, author_id, client_id or ""),
                )
            )
        document.save(update_fields=["head_seq", "updated_at"])

        doc_id = document.pk
        transaction.on_commit(lambda: realtime.broadcast_updates(doc_id, frames))

        interval = int(docs_settings.CRDT_ASSEMBLE_UPDATE_INTERVAL)
        if (
            spec.codec == CODEC_YJS
            and interval > 0
            and document.head_seq - document.snapshot_seq >= interval
        ):
            transaction.on_commit(lambda: _assemble_after_commit(doc_id))
        return document.head_seq


def _assemble_after_commit(document_id) -> None:
    """Opportunistic assembly, after the append committed. Never raises:
    the journal rows are durable and the idle sweep retries what a failed
    assembly leaves behind."""
    try:
        assemble_crdt_snapshot(document_id)
    except Exception:  # noqa: BLE001 — opportunistic work must not 500 a write
        logger.exception("docs: opportunistic crdt assembly failed for %s", document_id)


def read_updates(document, since: int):
    """Replay feed. Returns ("resync", None) when *since* fell out of the
    compaction window, else ("updates", rows)."""
    spec = effective_spec(document)
    if spec is None or spec.collab != COLLAB_CRDT:
        return "updates", []
    rows_qs = DocumentUpdate.objects.filter(document=document)
    oldest = rows_qs.aggregate(oldest=Min("seq"))["oldest"]
    if oldest is None:
        # Journal fully compacted (or never written): anything behind the
        # head is unreachable by replay.
        if since < document.head_seq:
            return "resync", None
        return "updates", []
    if oldest > since + 1:
        return "resync", None
    return "updates", list(rows_qs.filter(seq__gt=since).order_by("seq"))


def assemble_crdt_snapshot(document_id):
    """Materialize the update journal into the snapshot (yjs-codec types).

    A MATERIALIZATION, not a mutation: the folded state contains exactly
    the operations the journal already counts, so no seq is minted —
    ``head_seq`` never moves, ``snapshot_seq`` catches up to it, and the
    invariant "snapshot == fold of updates 1..snapshot_seq" holds by
    construction. Storage rules (content-addressed put, auto revision,
    orphan cleanup, compaction, emits) are :func:`_write_snapshot`'s — the
    same path every snapshot save takes. ``document.updated`` is emitted
    HERE and not per append: assembly is the debounce point the design
    wanted (§6 bus economy).

    No quota check on purpose: the folded bytes were each accepted through
    the update ceilings already, and refusing the materialization would
    only leave the same bytes in the journal, uncompactable, forever.

    Returns the new ``snapshot_seq``, or None when there was nothing to do
    (missing/trashed document, non-yjs type, journal already folded).
    """
    from . import crdt

    with storage_transaction() as stx, transaction.atomic():
        document = (
            Document.objects.select_for_update()
            .filter(pk=document_id, deleted_at__isnull=True)
            .first()
        )
        if document is None:
            return None
        spec = effective_spec(document)
        if (
            spec is None
            or spec.collab != COLLAB_CRDT
            or spec.codec != CODEC_YJS
            or not crdt.available()
        ):
            return None
        rows = list(
            DocumentUpdate.objects.filter(
                document=document, seq__gt=document.snapshot_seq
            ).order_by("seq")
        )
        if not rows:
            return None
        base = (
            get_storage().get_bytes(document.snapshot_key)
            if document.snapshot_key
            else crdt.EMPTY_STATE
        )
        state = crdt.fold(base, [bytes(row.payload) for row in rows])
        # The newest journal row, not head_seq read separately: under the
        # row lock they are equal, and the row is what the fold covered.
        target_seq = rows[-1].seq
        _write_snapshot(
            document, state, storage_tx=stx, seq=target_seq, advance_head=False
        )
        return target_seq


# ── Revisions ────────────────────────────────────────────────────────


def create_named_revision(document, name, *, user=None) -> Revision:
    """Name the CURRENT head snapshot. Naming the same head twice renames
    the existing named revision (the (document, seq, kind) row is unique)."""
    if not document.snapshot_key:
        raise DocsError(400, ERR_400_BAD_REQUEST)
    with transaction.atomic():
        revision, created = Revision.objects.get_or_create(
            document=document,
            seq=document.head_seq,
            kind=Revision.KIND_NAMED,
            defaults={
                "name": name,
                "storage_key": document.snapshot_key,
                "created_by": user,
                "size_bytes": document.size_bytes,
            },
        )
        if not created and revision.name != name:
            revision.name = name
            revision.save(update_fields=["name"])
    return revision


def revision_content(document, revision) -> tuple[bytes, str]:
    """The revision's FULL bytes — get_bytes alone suffices (I1)."""
    return get_storage().get_bytes(revision.storage_key), content_mime(document)


def restore_revision(document, revision, *, user=None):
    """Restore-as-new-head: the revision's bytes ride the same save path as
    PUT content (no If-Match — restore serializes on the row lock; always
    mints an auto revision). History is NEVER rewritten."""
    body = get_storage().get_bytes(revision.storage_key)
    return save_content(
        document.pk,
        body,
        expected_seq=None,
        user=user,
        force_revision=True,
        require_mutable_type=False,
    )


# ── Trash ────────────────────────────────────────────────────────────


def trash_listing(workspace_id, *, user=None) -> tuple:
    folders = with_stars(
        Folder.objects.filter(
            workspace_id=workspace_id, deleted_at__isnull=False
        ).order_by("name"),
        user,
        target="folder",
    )
    documents = with_stars(
        Document.objects.filter(
            workspace_id=workspace_id, deleted_at__isnull=False
        ).order_by("-created_at"),
        user,
        target="document",
    )
    return folders, documents


def purge_document(document) -> None:
    """Irreversible destruction, O(document), idempotent (verdict §3):
    every distinct storage key of its history + journal + rows, with the
    deletion announced and the byte delta accounted inside the transaction.
    The objects themselves die only after the commit — a purge that rolls
    back must leave the surviving rows readable."""
    with storage_transaction() as stx, transaction.atomic():
        key_sizes: dict[str, int] = {}
        for storage_key, size in document.revisions.values_list("storage_key", "size_bytes"):
            key_sizes.setdefault(storage_key, size)
        if document.snapshot_key:
            key_sizes.setdefault(document.snapshot_key, document.size_bytes)
        # Derived objects (cached thumbnails) are enumerated separately: they
        # die with the document — invariant I2 has no exception for pictures
        # OF the content — but their bytes were never charged to the quota,
        # so they must not appear in the storage_changed delta either.
        derived_keys = [
            key
            for key in document.thumbnails.values_list("storage_key", flat=True)
            if key and key not in key_sizes
        ]

        events.emit_document_deleted(document)

        for storage_key in list(key_sizes) + derived_keys:
            stx.delete(storage_key)

        DocumentUpdate.objects.filter(document=document).delete()
        document.revisions.all().delete()
        total = sum(key_sizes.values())
        workspace_id = document.workspace_id
        doc_id = document.pk
        document.delete()
        if total:
            events.emit_storage_changed(workspace_id, -total)
        transaction.on_commit(
            lambda: realtime.revoke_document(doc_id, reason="document_purged")
        )


def purge_folder(folder) -> tuple[int, int]:
    """Purge a trashed folder with its trashed contents. Live documents that
    still point into the subtree survive at the root (FK SET_NULL).
    Returns (folders_purged, documents_purged)."""
    with transaction.atomic():
        folder_ids = _subtree_folder_ids(folder, live_only=False)
        trashed_docs = list(
            Document.objects.filter(folder_id__in=folder_ids, deleted_at__isnull=False)
        )
        for doc in trashed_docs:
            purge_document(doc)
        deleted, _ = Folder.objects.filter(id__in=folder_ids).delete()
        return deleted, len(trashed_docs)


def empty_trash(workspace_id, ids=None) -> tuple[int, int]:
    """Purge the listed trashed items, or everything trashed in the
    workspace. A non-trashed (or unknown) id refuses the whole request.
    Returns (folders_purged, documents_purged)."""
    trashed_folders = Folder.objects.filter(
        workspace_id=workspace_id, deleted_at__isnull=False
    )
    trashed_docs = Document.objects.filter(
        workspace_id=workspace_id, deleted_at__isnull=False
    )
    with transaction.atomic():
        if ids is None:
            folder_list = list(trashed_folders)
            doc_list = list(trashed_docs)
        else:
            wanted = {uuid_module.UUID(str(i)) for i in ids}
            folder_list = list(trashed_folders.filter(id__in=wanted))
            doc_list = list(trashed_docs.filter(id__in=wanted))
            matched = {f.id for f in folder_list} | {d.id for d in doc_list}
            if wanted - matched:
                raise DocsError(400, ERR_400_NOT_TRASHED)

        folders_purged = 0
        documents_purged = 0
        purged_doc_ids = set()
        for doc in doc_list:
            purge_document(doc)
            purged_doc_ids.add(doc.id)
            documents_purged += 1
        for folder in folder_list:
            if not Folder.objects.filter(pk=folder.pk).exists():
                continue  # already cascaded away by an ancestor's purge
            f_count, d_count = purge_folder(folder)
            folders_purged += f_count
            documents_purged += d_count
        return folders_purged, documents_purged


def purge_expired() -> tuple[int, int]:
    """Retention expiry: purge everything soft-deleted longer than
    TRASH_RETENTION_DAYS ago. Returns (folders_purged, documents_purged)."""
    cutoff = timezone.now() - timedelta(days=int(docs_settings.TRASH_RETENTION_DAYS))
    folders_purged = 0
    documents_purged = 0
    for doc in Document.objects.filter(deleted_at__lt=cutoff):
        purge_document(doc)
        documents_purged += 1
    for folder in Folder.objects.filter(deleted_at__lt=cutoff):
        if not Folder.objects.filter(pk=folder.pk).exists():
            continue
        f_count, d_count = purge_folder(folder)
        folders_purged += f_count
        documents_purged += d_count
    return folders_purged, documents_purged


# ── Drive surfaces: starred, recents, search (drive-spec §3.1-§3.3) ──
#
# Three per-user views over the same corpus. None of them is a second
# authorization path: each takes a workspace the caller was already
# authorized for, and shows only rows a baseline `authorize(view)` listing
# would show — a star on a document is a bookmark, not a grant.


def _user_pk(user):
    """The user's primary key, from a model instance or a bare id.

    ``None`` means "no user in this request" — which is NOT the same as
    "this user starred nothing" (listings canon), and every caller below
    keeps the two apart.
    """
    if user is None:
        return None
    return getattr(user, "pk", user)


def with_stars(queryset, user, *, target: str):
    """Annotate ``is_starred`` on a Folder/Document queryset.

    ``None`` for a principal with no user id: "not applicable" is a third
    answer, and collapsing it into ``False`` tells an anonymous reader it
    un-starred something it never could have starred.
    """
    from django.db.models import BooleanField, Exists, OuterRef, Value

    user_id = _user_pk(user)
    if user_id is None:
        return queryset.annotate(is_starred=Value(None, output_field=BooleanField()))
    return queryset.annotate(
        is_starred=Exists(
            Star.objects.filter(user_id=user_id, **{f"{target}_id": OuterRef("pk")})
        )
    )


def attach_star(instance, user, *, target: str):
    """Set ``is_starred`` on a single already-fetched row (detail envelopes).

    The annotation above cannot reach a row somebody else already
    materialized, and a presenter reading an attribute that is simply absent
    would answer ``None`` for a member who DID star it.
    """
    if instance is None:
        return instance
    user_id = _user_pk(user)
    if user_id is None:
        instance.is_starred = None
    else:
        instance.is_starred = Star.objects.filter(
            user_id=user_id, **{f"{target}_id": instance.pk}
        ).exists()
    return instance


def set_star(*, document=None, folder=None, user, starred: bool) -> bool:
    """Star or unstar one target for one user. Returns whether anything
    changed — the endpoint answers the same status either way, because an
    idempotent verb that reports failure on a repeat is not idempotent.
    """
    user_id = _user_pk(user)
    if user_id is None:
        raise DocsError(403, ERR_403_FORBIDDEN)
    target = document if document is not None else folder
    if target is None:
        raise DocsError(404, ERR_404_NOT_FOUND)
    lookup = {"user_id": user_id}
    lookup["document" if document is not None else "folder"] = target
    if not starred:
        removed, _ = Star.objects.filter(**lookup).delete()
        return bool(removed)
    _, created = Star.objects.get_or_create(
        **lookup, defaults={"workspace_id": target.workspace_id}
    )
    return created


def starred_listing(workspace_id, user) -> tuple:
    """(folders, documents) this user starred in this workspace, live only.

    A trashed item drops out of the listing but KEEPS its star until purge:
    restoring from the trash brings the bookmark back, which is what a user
    who trashed something by accident expects.
    """
    user_id = _user_pk(user)
    if user_id is None:
        return Folder.objects.none(), Document.objects.none()
    limit = max(int(docs_settings.SEARCH_MAX_RESULTS), 1)
    folders = with_stars(
        Folder.objects.filter(
            workspace_id=workspace_id,
            deleted_at__isnull=True,
            stars__user_id=user_id,
        ).order_by("name"),
        user,
        target="folder",
    )[:limit]
    documents = with_stars(
        Document.objects.filter(
            workspace_id=workspace_id,
            deleted_at__isnull=True,
            stars__user_id=user_id,
        )
        .exclude(metadata__has_key=UPLOAD_PENDING_KEY)
        .order_by("-created_at"),
        user,
        target="document",
    )[:limit]
    return folders, documents


def touch_recent(document, user) -> None:
    """Mark *document* as just reached by *user* (upsert, then trim).

    No event: recents are per-user position, not workspace history, and a
    bus message per document open would be the noisiest topic in the fleet.
    """
    user_id = _user_pk(user)
    if user_id is None or document is None:
        return
    RecentEntry.objects.update_or_create(
        user_id=user_id,
        document_id=document.pk,
        defaults={
            "workspace_id": document.workspace_id,
            "accessed_at": timezone.now(),
        },
    )
    _trim_recents(user_id)


def _trim_recents(user_id) -> int:
    """Keep only the newest RECENTS_MAX_PER_USER rows for a user.

    Opportunistic (on write) rather than scheduled: the cap exists so the
    table cannot grow without bound, and a table that is only ever written
    through one function needs no second sweeper to enforce it.
    """
    cap = int(docs_settings.RECENTS_MAX_PER_USER)
    if cap <= 0:
        return 0
    rows = RecentEntry.objects.filter(user_id=user_id)
    if rows.count() <= cap:
        return 0
    stale = list(
        rows.order_by("-accessed_at", "-id").values_list("pk", flat=True)[cap:]
    )
    if not stale:
        return 0
    removed, _ = RecentEntry.objects.filter(pk__in=stale).delete()
    return removed


def recent_documents(workspace_id, user, *, limit: int = 0):
    """Documents this user reached most recently, newest first, live only."""
    user_id = _user_pk(user)
    if user_id is None:
        return Document.objects.none()
    limit = limit or int(docs_settings.RECENTS_MAX_PER_USER) or 100
    return with_stars(
        Document.objects.filter(
            workspace_id=workspace_id,
            deleted_at__isnull=True,
            recents__user_id=user_id,
        )
        .exclude(metadata__has_key=UPLOAD_PENDING_KEY)
        .order_by("-recents__accessed_at"),
        user,
        target="document",
    )[:limit]


def _folder_index(workspace_id) -> dict:
    """``{folder_id: (name, parent_id)}`` for a whole workspace, in ONE query.

    Breadcrumbs are walked in memory off this map. Resolving each hit's
    ancestry with its own queries is the N+1 that makes a search endpoint
    quadratic in tree depth the first time a workspace gets deep.
    """
    return {
        row[0]: (row[1], row[2])
        for row in Folder.objects.filter(workspace_id=workspace_id).values_list(
            "id", "name", "parent_id"
        )
    }


def _breadcrumb(index: dict, folder_id) -> list:
    """Root-first ``[(id, name), …]`` chain of ancestors, folder_id included.

    Defensive against a cycle the tree guards already forbid: a corrupted
    parent chain must not hang the request that reads it.
    """
    chain = []
    seen = set()
    node = folder_id
    while node is not None and node in index and node not in seen:
        seen.add(node)
        name, parent_id = index[node]
        chain.append((node, name))
        node = parent_id
    chain.reverse()
    return chain


def search(workspace_id, q: str, *, user=None, limit: int = 0) -> list:
    """Name search across one workspace's live tree (drive-spec §3.3).

    Case-insensitive substring over ``Folder.name`` and ``Document.title``,
    tree-wide. Returns ``[(kind, row, breadcrumb), …]`` — folders first,
    then documents — where ``breadcrumb`` is the root-first ancestor chain
    of the hit's CONTAINER (for a folder: its parents; for a document: its
    folder chain), so a client renders "where is this" without a second call.

    Deliberately not knowledge search: no FTS, no trigram. A workspace holds
    thousands of names, and ``icontains`` on that is honest — the day a
    measured workspace says otherwise, the index changes behind this
    function and its contract does not.
    """
    q = (q or "").strip()
    if not q:
        return []
    limit = limit or max(int(docs_settings.SEARCH_MAX_RESULTS), 1)
    index = _folder_index(workspace_id)
    folders = with_stars(
        Folder.objects.filter(
            workspace_id=workspace_id, deleted_at__isnull=True, name__icontains=q
        ).order_by("name"),
        user,
        target="folder",
    )[:limit]
    hits = [
        ("folder", folder, _breadcrumb(index, folder.parent_id)) for folder in folders
    ]
    remaining = limit - len(hits)
    if remaining > 0:
        documents = with_stars(
            Document.objects.filter(
                workspace_id=workspace_id,
                deleted_at__isnull=True,
                title__icontains=q,
            )
            .exclude(metadata__has_key=UPLOAD_PENDING_KEY)
            .order_by("title", "-created_at"),
            user,
            target="document",
        )[:remaining]
        hits.extend(
            ("document", document, _breadcrumb(index, document.folder_id))
            for document in documents
        )
    return hits


# ── Usage metering (drive-spec §3.4) ─────────────────────────────────


def workspace_usage(workspace_id) -> dict:
    """Everything ``docs.usage`` reports about one workspace.

    Bytes come from the SAME ``size_bytes`` columns the 507 quota sums
    (invariant I2, one sum): ``bytes_total`` equals
    :func:`workspace_usage_bytes` by construction, so a billing meter and a
    quota refusal can never disagree about how full a workspace is.

    ``documents``/``folders`` and ``by_type`` count the LIVE corpus (what a
    member sees); trashed rows are still charged for their bytes, which is
    what ``bytes_trash`` is for — trash is not a discount.
    """
    live_docs = Document.objects.filter(
        workspace_id=workspace_id, deleted_at__isnull=True
    )
    trashed_docs = Document.objects.filter(
        workspace_id=workspace_id, deleted_at__isnull=False
    )

    def _heads(qs) -> int:
        return int(qs.aggregate(total=Sum("size_bytes"))["total"] or 0)

    def _revisions(deleted: bool) -> int:
        return int(
            Revision.objects.filter(
                document__workspace_id=workspace_id,
                document__deleted_at__isnull=not deleted,
            ).aggregate(total=Sum("size_bytes"))["total"]
            or 0
        )

    bytes_live = _heads(live_docs) + _revisions(False)
    bytes_trash = _heads(trashed_docs) + _revisions(True)

    by_type: dict[str, dict] = {}
    for row in live_docs.values("type").annotate(
        documents=Count("id"), size=Sum("size_bytes")
    ):
        by_type[row["type"]] = {
            "documents": int(row["documents"]),
            "bytes": int(row["size"] or 0),
        }
    for row in (
        Revision.objects.filter(
            document__workspace_id=workspace_id, document__deleted_at__isnull=True
        )
        .values("document__type")
        .annotate(size=Sum("size_bytes"))
    ):
        slug = row["document__type"]
        bucket = by_type.setdefault(slug, {"documents": 0, "bytes": 0})
        bucket["bytes"] += int(row["size"] or 0)

    return {
        "bytes_live": bytes_live,
        "bytes_trash": bytes_trash,
        "bytes_total": bytes_live + bytes_trash,
        "documents": live_docs.count(),
        "folders": Folder.objects.filter(
            workspace_id=workspace_id, deleted_at__isnull=True
        ).count(),
        "by_type": by_type,
    }


# ── Thumbnails (drive-spec §3.6) ─────────────────────────────────────


def thumbnail_key(document, tier: int, seq: int) -> str:
    """Storage key of a cached thumbnail — under the document's OWN prefix.

    The ``seq`` in the key is what makes a stale image unreachable rather
    than merely unpreferred: a save bumps ``head_seq``, so the next request
    addresses a key that does not exist yet and re-renders.
    """
    return f"{document_prefix(document.workspace_id, document.id)}/thumb.{seq}.{tier}.jpg"


def _thumbnailable(document) -> None:
    """Refuse a document that has no image to render (400, never 500)."""
    if document.type != "file" or not (document.mime_type or "").lower().startswith(
        "image/"
    ):
        raise DocsError(400, ERR_400_THUMBNAIL_UNSUPPORTED, {"type": document.type})
    if not document.snapshot_key:
        # A pending upload: the row exists, the bytes do not.
        raise DocsError(400, ERR_400_THUMBNAIL_UNSUPPORTED, {"type": document.type})


def get_thumbnail(document, tier) -> tuple[bytes, str]:
    """(image bytes, mime) for *document* at *tier*, rendering + caching once.

    Bytes in and out travel the storage seam and nothing else, so a preview
    of a private workspace file is exactly as private as the file. The
    cached object lives under the document's prefix and is registered as a
    :class:`~stapel_docs.models.Thumbnail` row, so ``purge_document``
    destroys it with the rest of the document (invariant I2) — purge deletes
    enumerated keys, and an unregistered derived object would survive its
    subject.
    """
    from .thumbnails import (
        THUMBNAIL_MIME,
        THUMBNAIL_TIERS,
        ThumbnailSourceUnusable,
        ThumbnailsUnavailable,
        render,
    )

    try:
        tier = int(tier)
    except (TypeError, ValueError):
        raise DocsError(400, ERR_400_THUMBNAIL_TIER, {"tiers": list(THUMBNAIL_TIERS)})
    if tier not in THUMBNAIL_TIERS:
        raise DocsError(400, ERR_400_THUMBNAIL_TIER, {"tiers": list(THUMBNAIL_TIERS)})
    _thumbnailable(document)

    storage = get_storage()
    cached = Thumbnail.objects.filter(document=document, tier=tier).first()
    if cached is not None and cached.source_seq == document.head_seq:
        exists, _ = storage.head_object(cached.storage_key)
        if exists:
            return storage.get_bytes(cached.storage_key), THUMBNAIL_MIME
        # The row outlived its object (an operator swept the bucket): fall
        # through and re-render rather than serving a 500 for a cache miss.

    source = storage.get_bytes(document.snapshot_key)
    try:
        image = render(source, tier)
    except ThumbnailsUnavailable:
        raise DocsError(503, ERR_503_THUMBNAILS)
    except ThumbnailSourceUnusable as exc:
        raise DocsError(400, ERR_400_THUMBNAIL_UNSUPPORTED, {"reason": str(exc)})

    key = thumbnail_key(document, tier, document.head_seq)
    with storage_transaction() as stx, transaction.atomic():
        stx.put(key, image, content_type=THUMBNAIL_MIME)
        if cached is not None and cached.storage_key and cached.storage_key != key:
            stx.delete(cached.storage_key)
        Thumbnail.objects.update_or_create(
            document=document,
            tier=tier,
            defaults={
                "source_seq": document.head_seq,
                "storage_key": key,
                "size_bytes": len(image),
            },
        )
    return image, THUMBNAIL_MIME


# ── Uploads (type=file via presigned PUT; recordings pattern) ────────


def pending_uploads(workspace_id):
    """Open (pending, unexpired) sessions of a workspace."""
    return UploadSession.objects.filter(
        workspace_id=workspace_id, state=UploadSession.STATE_PENDING
    ).exclude(expires_at__lt=timezone.now())


def create_upload(
    *,
    workspace_id,
    title,
    folder_id=None,
    mime_type="",
    size_bytes=0,
    checksum="",
    user=None,
) -> tuple[UploadSession, str]:
    """Create the Document row immediately (hidden from listings while
    pending) plus its UploadSession. Returns (session, put_url).

    The ticket carries its invariants: a declared size and optional sha256
    the stored object is checked against at finalize, an expiry, and the
    user it belongs to."""
    size_bytes = int(size_bytes or 0)
    upload_limit = resource_limit("MAX_UPLOAD_BYTES")
    if upload_limit and size_bytes > upload_limit:
        raise DocsError(413, ERR_413_UPLOAD_TOO_LARGE, {"limit_bytes": upload_limit})
    if not _mime_allowed(mime_type):
        raise DocsError(400, ERR_400_UPLOAD_MIME, {"mime_type": mime_type or ""})
    assert_quota(workspace_id, size_bytes)
    open_limit = resource_limit("MAX_PENDING_UPLOADS_PER_WORKSPACE")
    if open_limit and pending_uploads(workspace_id).count() >= open_limit:
        raise DocsError(400, ERR_400_TOO_MANY_UPLOADS, {"limit": open_limit})
    folder = None
    if folder_id is not None:
        folder = get_live_folder(folder_id, workspace_id=workspace_id)
    ttl = resource_limit("UPLOAD_SESSION_TTL_SECONDS")
    expires_at = timezone.now() + timedelta(seconds=ttl) if ttl else None
    with transaction.atomic():
        document = Document.objects.create(
            workspace_id=workspace_id,
            folder=folder,
            type="file",
            title=title,
            owner=user,
            mime_type=mime_type or "",
            metadata={UPLOAD_PENDING_KEY: True},
        )
        session_id = uuid_module.uuid4()
        key = f"{document_prefix(workspace_id, document.id)}/upload-{session_id}"
        session = UploadSession.objects.create(
            id=session_id,
            workspace_id=workspace_id,
            folder=folder,
            document=document,
            title=title,
            mime_type=mime_type or "",
            key=key,
            created_by=user,
            size_bytes=size_bytes,
            checksum=(checksum or "").lower(),
            expires_at=expires_at,
        )
    put_url = get_storage().presigned_put_url(
        key,
        expires_seconds=int(docs_settings.UPLOAD_URL_EXPIRES_SECONDS),
        content_type=mime_type or None,
    )
    return session, put_url


def finalize_upload(session) -> Document:
    """Promote the uploaded object to the document's head (seq 1) and
    announce the document. The stored blob is the byte-preserved original —
    never rewritten (verdict §9.4).

    Finalize is where the declaration meets reality: the object must exist,
    fit the ceilings, match the declared size (and sha256 when one was
    declared) and fit the workspace budget. The session is then CONSUMED by
    a conditional state transition, so two concurrent finalizes cannot both
    promote the blob."""
    if session.state != UploadSession.STATE_PENDING or session.document_id is None:
        raise DocsError(400, ERR_400_UPLOAD_STATE)
    if session.expires_at is not None and session.expires_at <= timezone.now():
        raise DocsError(400, ERR_400_UPLOAD_EXPIRED)
    storage = get_storage()
    exists, size = storage.head_object(session.key)
    if not exists:
        raise DocsError(400, ERR_400_UPLOAD_STATE)
    if size is None:
        # An object nobody measured cannot be checked against the ceiling
        # or charged to the quota, and the client's declaration is not a
        # substitute for a measurement — falling back to it (or to the 0 a
        # ticket opened with no declared size carries) is how a storage
        # fault becomes free, unbounded storage.
        raise DocsError(400, ERR_400_UPLOAD_UNMEASURABLE)
    upload_limit = resource_limit("MAX_UPLOAD_BYTES")
    if upload_limit and size > upload_limit:
        raise DocsError(
            413, ERR_413_UPLOAD_TOO_LARGE, {"limit_bytes": upload_limit, "size_bytes": size}
        )
    # A declared size is a promise about the object, not a hint: an object
    # of a different length is a different object than the one authorized.
    if session.size_bytes and size != session.size_bytes:
        raise DocsError(
            400,
            ERR_400_UPLOAD_MISMATCH,
            {"declared_bytes": session.size_bytes, "size_bytes": size},
        )
    if session.checksum:
        if content_hash(storage.get_bytes(session.key)) != session.checksum:
            raise DocsError(400, ERR_400_UPLOAD_MISMATCH, {"checksum": session.checksum})
    assert_quota(session.workspace_id, size)
    with transaction.atomic():
        # Atomic consume: exactly one caller wins the pending -> finalized
        # transition; the loser sees the same 400 a replayed ticket sees.
        consumed = UploadSession.objects.filter(
            pk=session.pk, state=UploadSession.STATE_PENDING
        ).update(state=UploadSession.STATE_FINALIZED, size_bytes=size, updated_at=timezone.now())
        if not consumed:
            raise DocsError(400, ERR_400_UPLOAD_STATE)
        document = (
            Document.objects.select_for_update()
            .filter(pk=session.document_id, deleted_at__isnull=True)
            .first()
        )
        if document is None:
            raise DocsError(404, ERR_404_DOCUMENT)
        document.head_seq = 1
        document.snapshot_seq = 1
        document.snapshot_key = session.key
        document.size_bytes = size
        if session.mime_type:
            document.mime_type = session.mime_type
        metadata = dict(document.metadata or {})
        metadata.pop(UPLOAD_PENDING_KEY, None)
        document.metadata = metadata
        document.save(
            update_fields=[
                "head_seq",
                "snapshot_seq",
                "snapshot_key",
                "size_bytes",
                "mime_type",
                "metadata",
                "updated_at",
            ]
        )
        Revision.objects.create(
            document=document,
            seq=1,
            kind=Revision.KIND_AUTO,
            storage_key=session.key,
            created_by=session.created_by,
            size_bytes=size,
        )
        session.refresh_from_db(fields=["state", "size_bytes", "updated_at"])
        events.emit_document_created(document)
        events.emit_storage_changed(document.workspace_id, size)
    return document


def download_url(storage_key: str) -> str:
    """Mint a time-limited read URL for a stored object.

    A URL that never expires is a second read path: it survives the
    membership that produced it and it never comes back through
    ``authorize()``. So a backend that cannot honour
    DOWNLOAD_URL_EXPIRES_SECONDS (``mints_expiring_urls`` False — the
    default for anything that does not sign) gets no URL minted for it at
    all, unless the deployment explicitly accepted permanent links. Callers
    who need the bytes read them through the authorized content endpoint,
    which is unaffected."""
    if not storage_key:
        raise DocsError(404, ERR_404_NOT_FOUND)
    backend = get_storage()
    if not getattr(backend, "mints_expiring_urls", False) and not (
        docs_settings.ALLOW_UNEXPIRING_DOWNLOAD_URLS
    ):
        raise DocsError(503, ERR_503_DOWNLOAD_URL)
    return backend.presigned_get_url(
        storage_key, expires_seconds=int(docs_settings.DOWNLOAD_URL_EXPIRES_SECONDS)
    )


def document_download_url(document, *, user=None) -> str:
    """Mint a download URL for a document's CURRENT body and record the
    reach (drive-spec §3.2).

    Issuing the URL is the moment the user got the document — the bytes then
    leave through a signed link this service never sees again — so the
    recents upsert belongs here, not at some later read that may never come
    back through the API. A refused URL (503) records nothing: nothing was
    handed over.
    """
    url = download_url(document.snapshot_key)
    touch_recent(document, user)
    return url


# ─────────────────────────────────────────────────────────────────────
# Sharing axis — grant rows, links, redemption (sharing-axis-design)
# ─────────────────────────────────────────────────────────────────────


def _sharing_mode_or_refuse(mode: str, workspace_id) -> None:
    """Refuse to MINT into a disabled mode.

    The kill-switch makes existing rows inert, not deleted (axis §3) — but
    writing a NEW row nothing will ever read is worse than refusing: the
    admin sees a grant in the sheet, the guest sees a 403, and the two
    never meet. Reading and revoking stay possible while the mode is off,
    which is what makes the suspended state operable.
    """
    from .authz import mode_enabled

    if not mode_enabled(mode, workspace_id):
        raise DocsError(400, ERR_400_SHARE_MODE_DISABLED, {"mode": mode})


def mark_sharing_suspended(rows, mode: str, workspace_id):
    """Stamp ``is_suspended`` on share-sheet rows of *mode*.

    An inert grant is SHOWN, never hidden (axis §3): an admin who cannot
    see a row believes it was revoked, and re-enabling the mode then
    restores access nobody remembers granting.
    """
    from .authz import mode_enabled

    suspended = not mode_enabled(mode, workspace_id)
    materialized = list(rows)
    for row in materialized:
        row.is_suspended = suspended
    return materialized


def list_access(document):
    """Whitelist rows of one document, newest first, marked suspended when
    the mode is off."""
    from .models import DocumentAccess

    rows = DocumentAccess.objects.filter(document=document).order_by("-created_at")
    return mark_sharing_suspended(rows, "whitelist", document.workspace_id)


def grant_access(
    document, *, subject_kind: str, user_id=None, ref: str = "", level: str, granted_by
):
    """Create (or raise the level of) one whitelist grant.

    Upsert, not insert: re-granting to the same subject is the share sheet's
    ordinary gesture ("make them an editor"), and answering it with a
    uniqueness error would make the UI carry a special case for a
    conflict-free operation. The unique constraint stays — it is what makes
    one subject have exactly one answer.

    Fail-closed at the WRITE boundary too (axis §11.3): a ref whose kind has
    no registered resolver is refused here, so a row that could only ever
    deny never gets stored.
    """
    from .authz import LEVEL_ORDER, get_ref_resolver, ref_kind
    from .models import DocumentAccess

    _sharing_mode_or_refuse("whitelist", document.workspace_id)
    if level not in LEVEL_ORDER:
        raise DocsError(400, ERR_400_SHARE_LEVEL, {"level": level})

    if subject_kind == DocumentAccess.SUBJECT_USER:
        if not user_id or ref:
            raise DocsError(400, ERR_400_SHARE_SUBJECT)
        lookup = {"user_id": user_id, "ref": ""}
    elif subject_kind == DocumentAccess.SUBJECT_REF:
        if not ref or user_id:
            raise DocsError(400, ERR_400_SHARE_SUBJECT)
        kind = ref_kind(ref)
        if get_ref_resolver(kind) is None:
            raise DocsError(400, ERR_400_SHARE_REF_KIND, {"kind": kind})
        lookup = {"user_id": None, "ref": ref}
    else:
        raise DocsError(400, ERR_400_SHARE_SUBJECT)

    with transaction.atomic():
        row, created = DocumentAccess.objects.get_or_create(
            document=document,
            subject_kind=subject_kind,
            defaults={
                "workspace_id": document.workspace_id,
                "level": level,
                "granted_by": granted_by,
            },
            **lookup,
        )
        if not created and row.level != level:
            row.level = level
            row.granted_by = granted_by
            row.save(update_fields=["level", "granted_by"])
        elif not created:
            return row
        events.emit_share_granted(row)
    return row


def get_access(document, access_id):
    """One grant of this document, or 404 — scoped by document so an id
    from another workspace addresses nothing."""
    from .models import DocumentAccess

    row = DocumentAccess.objects.filter(document=document, id=access_id).first()
    if row is None:
        raise DocsError(404, ERR_404_SHARE_ACCESS)
    return row


def revoke_access(document, access_id) -> None:
    """Delete one grant. Revocation works while the mode is OFF too: an
    operator must always be able to take access away, whatever the axis
    currently says about giving it.

    A user-subject revocation also kicks that user's open sockets on the
    document's stream (chat's revoke-to-kick pattern): the socket's cached
    authorize verdict must not outlive the row it rested on. Ref-subject
    grants name a container, not a user — there is nobody to kick by name,
    and the authorize cache TTL is the honest bound there (MODULE.md)."""
    from .models import DocumentAccess

    row = get_access(document, access_id)
    with transaction.atomic():
        events.emit_share_revoked(row)
        subject_user_id = (
            row.user_id if row.subject_kind == DocumentAccess.SUBJECT_USER else None
        )
        row.delete()
        if subject_user_id is not None:
            doc_id = document.pk
            transaction.on_commit(
                lambda: realtime.revoke_document(
                    doc_id, user_id=subject_user_id, reason="access_revoked"
                )
            )


def list_links(document):
    """Bearer links of one document, newest first, marked suspended when the
    link mode is off."""
    from .models import DocumentLink

    rows = DocumentLink.objects.filter(document=document).order_by("-created_at")
    return mark_sharing_suspended(rows, "link", document.workspace_id)


def link_expiry(now=None):
    """The deadline a link minted now would carry (``LINK["TTL_DAYS"]``).

    ``TTL_DAYS=None`` is the host saying "perpetual"; it becomes a century,
    not a null, because the column is NOT NULL by the invitation canon and
    because a deadline every reader can render beats an absence every
    reader must special-case.
    """
    from .authz import link_settings

    now = now or timezone.now()
    days = link_settings().get("TTL_DAYS")
    if days is None:
        return now + timedelta(days=365 * 100)
    return now + timedelta(days=int(days))


def create_link(document, *, level: str, created_by):
    """Mint one bearer link.

    The level is capped by ``LINK["MAX_LEVEL"]`` — a ceiling the DEPLOYMENT
    owns, refused loudly rather than clamped silently, because a client
    that asked for edit and got view without being told will show the wrong
    thing to the person it hands the link to. The second cap (never above
    the granter's own level) is applied by the caller through
    ``authorize()`` — the choke point, not a second membership check here.
    """
    from secrets import token_urlsafe

    from .authz import LEVEL_ORDER, link_settings
    from .models import DocumentLink

    _sharing_mode_or_refuse("link", document.workspace_id)
    ceiling = link_settings().get("MAX_LEVEL") or "view"
    if level not in LEVEL_ORDER or ceiling not in LEVEL_ORDER:
        raise DocsError(400, ERR_400_SHARE_LEVEL, {"level": level})
    if LEVEL_ORDER[level] > LEVEL_ORDER[ceiling]:
        raise DocsError(400, ERR_400_SHARE_LEVEL, {"level": level, "max_level": ceiling})

    with transaction.atomic():
        link = DocumentLink.objects.create(
            document=document,
            workspace_id=document.workspace_id,
            token=token_urlsafe(32),
            level=level,
            created_by=created_by,
            expires_at=link_expiry(),
        )
        events.emit_link_created(link)
    return link


def get_link(document, link_id):
    """One link of this document, or 404."""
    from .models import DocumentLink

    link = DocumentLink.objects.filter(document=document, id=link_id).first()
    if link is None:
        raise DocsError(404, ERR_404_SHARE_LINK)
    return link


def revoke_link(document, link_id):
    """Withdraw a link. Idempotent: an already-revoked link keeps its first
    ``revoked_at`` and emits nothing a second time."""
    link = get_link(document, link_id)
    if link.revoked_at is not None:
        return link
    with transaction.atomic():
        link.revoked_at = timezone.now()
        link.save(update_fields=["revoked_at"])
        events.emit_link_revoked(link)
    return link


def get_link_by_token(token: str):
    """Resolve a presented token to ``(document, link)``, or 404.

    404 for a token that names nothing and for a trashed document. A token
    that names a DEAD link (expired, revoked, sponsor gone) resolves here
    and is refused by ``authorize()`` — the bearer views render that refusal
    as 404 as well, so the endpoint is never an oracle telling a guesser
    that a token was real. Liveness and LEVEL are decided by ``authorize()``,
    never here: one rule, one place.
    """
    from .models import DocumentLink

    link = (
        DocumentLink.objects.select_related("document")
        .filter(token=token)
        .first()
    )
    if link is None or link.document.deleted_at is not None:
        raise DocsError(404, ERR_404_DOCUMENT)
    return link.document, link


def redeem_link(link):
    """Stamp the first successful presentation and announce it once.

    Called only after ``authorize()`` allowed the request — the stamp is
    evidence that somebody got in, so it must not be written by a
    presentation that was refused. Guarded by a conditional UPDATE, so two
    simultaneous first openings still emit exactly one event.
    """
    from .models import DocumentLink

    if link.first_redeemed_at is not None:
        return link
    now = timezone.now()
    with transaction.atomic():
        stamped = DocumentLink.objects.filter(
            pk=link.pk, first_redeemed_at__isnull=True
        ).update(first_redeemed_at=now)
        if not stamped:
            return link
        link.first_redeemed_at = now
        events.emit_link_redeemed(link)
    return link
