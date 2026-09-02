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

The ``document.share.*`` family is the sharing axis's audit trail (§6):
mint, revoke and FIRST redemption of every grant source. **No payload here
ever carries a link token** — an event is copied into an outbox, a broker,
a log aggregator and somebody's dashboard, and a bearer secret that travels
that far has been leaked by its own audit trail. Consumers address a link
by its id.
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


def emit_share_granted(access) -> None:
    """A whitelist grant was created or its level was raised."""
    emit("document.share.granted", _access_payload(access))


def emit_share_revoked(access) -> None:
    """A whitelist grant was withdrawn (the row is gone; this is the record)."""
    emit("document.share.revoked", _access_payload(access))


def emit_link_created(link) -> None:
    emit("document.share.link_created", _link_payload(link))


def emit_link_revoked(link) -> None:
    emit("document.share.link_revoked", _link_payload(link))


def emit_link_redeemed(link) -> None:
    """FIRST successful presentation only (``first_redeemed_at`` stamped).

    Not a per-hit event: a link is checked on every request, so emitting per
    presentation would put a request log on the bus. "Somebody opened this"
    is the auditable fact, and it happens once.
    """
    emit("document.share.link_redeemed", _link_payload(link))


def _access_payload(access) -> dict:
    return {
        "access_id": str(access.id),
        "document_id": str(access.document_id),
        "workspace_id": str(access.workspace_id),
        "subject_kind": access.subject_kind,
        "subject": access.subject,
        "level": access.level,
        "granted_by": str(access.granted_by_id) if access.granted_by_id else None,
    }


def _link_payload(link) -> dict:
    return {
        # The token is deliberately absent — see the module docstring.
        "link_id": str(link.id),
        "document_id": str(link.document_id),
        "workspace_id": str(link.workspace_id),
        "level": link.level,
        "status": link.status,
        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        "created_by": str(link.created_by_id) if link.created_by_id else None,
    }


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
    "emit_share_granted",
    "emit_share_revoked",
    "emit_link_created",
    "emit_link_revoked",
    "emit_link_redeemed",
]
