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

import uuid as uuid_module
from datetime import timedelta

from django.db import transaction
from django.db.models import Min
from django.utils import timezone
from stapel_core.django.api.errors import ERR_400_BAD_REQUEST, ERR_404_NOT_FOUND

from . import events
from .conf import docs_settings
from .doc_types import COLLAB_CRDT, get_doc_types
from .errors import (
    ERR_400_DUPLICATE_NAME,
    ERR_400_FOLDER_CYCLE,
    ERR_400_FOLDER_DEPTH,
    ERR_400_NOT_TRASHED,
    ERR_400_UNKNOWN_TYPE,
    ERR_400_UPDATES_NOT_CRDT,
    ERR_400_UPLOAD_STATE,
    ERR_404_DOCUMENT,
    ERR_404_FOLDER,
    ERR_404_REVISION,
    ERR_404_UPLOAD,
    ERR_409_SEQ_CONFLICT,
)
from .models import Document, DocumentUpdate, Folder, Revision, UploadSession
from .storage import content_hash, document_prefix, get_storage, snapshot_key

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
        mime = spec.mime_type
        # Editable text bodies are utf-8 by contract; without an explicit
        # charset, HTTP clients default text/* to latin-1 and mis-render.
        if mime.startswith("text/"):
            mime += "; charset=utf-8"
        return mime
    return document.mime_type or "application/octet-stream"


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


def list_folders(workspace_id, parent_id=..., limit: int = 200):
    qs = Folder.objects.filter(workspace_id=workspace_id, deleted_at__isnull=True)
    if parent_id is None:
        qs = qs.filter(parent__isnull=True)
    elif parent_id is not ...:
        qs = qs.filter(parent_id=parent_id)
    return qs.order_by("name")[:limit]


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


def list_documents(workspace_id, *, folder_id=None, type=None, q=None, limit: int = 200):
    qs = Document.objects.filter(
        workspace_id=workspace_id, deleted_at__isnull=True
    ).exclude(metadata__has_key=UPLOAD_PENDING_KEY)
    if folder_id is not None:
        qs = qs.filter(folder_id=folder_id)
    if type:
        qs = qs.filter(type=type)
    if q:
        qs = qs.filter(title__icontains=q)
    return qs.order_by("-created_at")[:limit]


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
    metadata = dict(metadata or {})
    # Only the upload flow may mark a document pending.
    metadata.pop(UPLOAD_PENDING_KEY, None)
    with transaction.atomic():
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
            _save_snapshot(document, body, user=acting, emit_updated=False)
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


def restore_document(document) -> Document:
    with transaction.atomic():
        document.deleted_at = None
        document.save(update_fields=["deleted_at", "updated_at"])
        events.emit_document_created(document)
    return document


# ── Content (the versioning heart) ───────────────────────────────────


def read_content(document) -> tuple[bytes, str, int]:
    """(body bytes, mime, head_seq). No body yet -> the spec's empty body
    at head_seq 0 (b"" for vanished types)."""
    if document.snapshot_key:
        body = get_storage().get_bytes(document.snapshot_key)
    else:
        spec = effective_spec(document)
        body = spec.empty_body if spec is not None else b""
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


def _save_snapshot(document, body: bytes, *, user=None, force_revision=False, emit_updated=True):
    """The single snapshot-save path (PUT content, create-with-body, revision
    restore, all types). Caller holds the row lock (or just created the row).
    Returns the minted auto Revision or None."""
    storage = get_storage()
    new_seq = document.head_seq + 1
    key = snapshot_key(document.workspace_id, document.id, content_hash(body))
    prev_key, prev_size = document.snapshot_key, document.size_bytes

    # Content-addressed: an existing object means zero new stored bytes.
    already_stored, _ = storage.head_object(key)
    storage.put_bytes(key, body, content_type=content_mime(document))
    added = 0 if already_stored else len(body)

    document.head_seq = new_seq
    document.snapshot_seq = new_seq
    document.snapshot_key = key
    document.size_bytes = len(body)
    document.save(
        update_fields=["head_seq", "snapshot_seq", "snapshot_key", "size_bytes", "updated_at"]
    )

    revision = None
    if force_revision or _auto_revision_due(document):
        revision = Revision.objects.create(
            document=document,
            seq=new_seq,
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
        storage.delete_object(prev_key)
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


def save_content(document_id, body: bytes, *, expected_seq=None, user=None, force_revision=False):
    """Optimistic-lock snapshot save. ``expected_seq=None`` skips the check
    (revision restore — it serializes on the same lock). Returns
    (document, revision-or-None)."""
    with transaction.atomic():
        document = (
            Document.objects.select_for_update()
            .filter(pk=document_id, deleted_at__isnull=True)
            .first()
        )
        if document is None:
            raise DocsError(404, ERR_404_DOCUMENT)
        if expected_seq is not None and expected_seq != document.head_seq:
            saved_by, saved_at = _winning_save(document)
            raise SeqConflict(
                head_seq=document.head_seq, saved_by=saved_by, saved_at=saved_at
            )
        revision = _save_snapshot(
            document, body, user=user, force_revision=force_revision
        )
    return document, revision


# ── Update journal (crdt discipline) ─────────────────────────────────


def append_updates(document_id, updates: list[bytes], *, client_id="", client_seq=None, principal=None) -> int:
    """Append a batch of opaque commutative updates at ++head_seq each.
    Journal appends do NOT emit document.updated (bus economy, design §6).
    Returns the new head_seq."""
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
        document.save(update_fields=["head_seq", "updated_at"])
        return document.head_seq


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
    return save_content(document.pk, body, expected_seq=None, user=user, force_revision=True)


# ── Trash ────────────────────────────────────────────────────────────


def trash_listing(workspace_id) -> tuple:
    folders = Folder.objects.filter(
        workspace_id=workspace_id, deleted_at__isnull=False
    ).order_by("name")
    documents = Document.objects.filter(
        workspace_id=workspace_id, deleted_at__isnull=False
    ).order_by("-created_at")
    return folders, documents


def purge_document(document) -> None:
    """Irreversible destruction, O(document), idempotent (verdict §3):
    every distinct storage key of its history + journal + rows, with the
    deletion announced and the byte delta accounted inside the transaction."""
    with transaction.atomic():
        key_sizes: dict[str, int] = {}
        for storage_key, size in document.revisions.values_list("storage_key", "size_bytes"):
            key_sizes.setdefault(storage_key, size)
        if document.snapshot_key:
            key_sizes.setdefault(document.snapshot_key, document.size_bytes)

        events.emit_document_deleted(document)

        storage = get_storage()
        for storage_key in key_sizes:
            storage.delete_object(storage_key)

        DocumentUpdate.objects.filter(document=document).delete()
        document.revisions.all().delete()
        total = sum(key_sizes.values())
        workspace_id = document.workspace_id
        document.delete()
        if total:
            events.emit_storage_changed(workspace_id, -total)


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


# ── Uploads (type=file via presigned PUT; recordings pattern) ────────


def create_upload(
    *, workspace_id, title, folder_id=None, mime_type="", size_bytes=0, user=None
) -> tuple[UploadSession, str]:
    """Create the Document row immediately (hidden from listings while
    pending) plus its UploadSession. Returns (session, put_url)."""
    folder = None
    if folder_id is not None:
        folder = get_live_folder(folder_id, workspace_id=workspace_id)
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
            size_bytes=size_bytes or 0,
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
    never rewritten (verdict §9.4)."""
    if session.state != UploadSession.STATE_PENDING or session.document_id is None:
        raise DocsError(400, ERR_400_UPLOAD_STATE)
    storage = get_storage()
    exists, size = storage.head_object(session.key)
    if not exists:
        raise DocsError(400, ERR_400_UPLOAD_STATE)
    size = size or session.size_bytes or 0
    with transaction.atomic():
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
        session.state = UploadSession.STATE_FINALIZED
        session.size_bytes = size
        session.save(update_fields=["state", "size_bytes", "updated_at"])
        events.emit_document_created(document)
        events.emit_storage_changed(document.workspace_id, size)
    return document


def download_url(storage_key: str) -> str:
    if not storage_key:
        raise DocsError(404, ERR_404_NOT_FOUND)
    return get_storage().presigned_get_url(
        storage_key, expires_seconds=int(docs_settings.DOWNLOAD_URL_EXPIRES_SECONDS)
    )
