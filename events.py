"""Emitted actions of stapel-docs (transactional outbox, at-least-once).

Call sites are the service layer, always inside the mutating transaction
(outbox canon). Payload schemas live in ``schemas/emits/`` and are
enforced in tests via ``VALIDATE_SCHEMAS``.

``document.updated`` is emitted per accepted SAVE / revision event — for
snapshot-discipline types that is naturally save-grained. When crdt types
arrive, journal appends must NOT emit per update (the bus would drown in
typing); the snapshot policy debounces there.

``document.deleted`` means "this document left the visible corpus" — it
fires on trash AND on purge (consumers are idempotent, at-least-once);
restore re-announces the document via ``document.created``.
"""
from __future__ import annotations

from stapel_core.comm import emit


def emit_document_created(document) -> None:
    emit("document.created", _document_payload(document))


def emit_document_updated(document) -> None:
    emit(
        "document.updated",
        {
            "document_id": str(document.id),
            "workspace_id": str(document.workspace_id),
            "head_seq": document.head_seq,
        },
    )


def emit_document_deleted(document) -> None:
    emit(
        "document.deleted",
        {
            "document_id": str(document.id),
            "workspace_id": str(document.workspace_id),
        },
    )


def emit_storage_changed(workspace_id, delta_bytes: int) -> None:
    """Docs keeps its own byte accounting and announces deltas; whether
    ``Workspace.storage_used_bytes`` follows is the host's subscriber
    decision (design §4 — docs never writes another module's model)."""
    emit(
        "document.storage_changed",
        {"workspace_id": str(workspace_id), "delta_bytes": int(delta_bytes)},
    )


def _document_payload(document) -> dict:
    return {
        "document_id": str(document.id),
        "workspace_id": str(document.workspace_id),
        "type": document.type,
        "title": document.title,
        "folder_id": str(document.folder_id) if document.folder_id else None,
        "owner_id": str(document.owner_id) if document.owner_id else None,
    }


__all__ = [
    "emit_document_created",
    "emit_document_updated",
    "emit_document_deleted",
    "emit_storage_changed",
]
