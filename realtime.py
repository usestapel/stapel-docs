"""Fan-out: the frames docs puts on the wire, and where it puts them.

One stream per document — ``docs:doc:<document_id>`` — carrying the update
journal, resumable by the journal's own ``seq``. Store-first is the whole
design (§5.2): REST append is the write path, the row owns the sequence,
and this module only tells live subscribers about a fact the database
already holds. Delivery is :func:`~stapel_realtime.delivery.deliver_frame`
from ``transaction.on_commit``, best-effort by contract: no channel layer,
no substrate installed, redis down — nothing raises, because a client
recovers by replaying ``?since=`` (polling) or by the socket's resume.

Unlike chat, realtime is OPTIONAL here (the ``[realtime]`` extra): polling
``?since=`` stays a first-class mode forever (design §5.3 p.7 — self-host
without ASGI). That is why the import of the substrate is lazy and why the
wiring check is a warning (``stapel_docs.W034``), never an error.

Wire payloads are JSON, so the binary Y update travels base64-encoded.
There is no separate frame shape for replay: the socket's catch-up rows
carry exactly this payload, which is what lets a client treat live and
replayed updates identically.
"""
from __future__ import annotations

import base64
import logging

logger = logging.getLogger(__name__)

#: Canonical stream-key module segment.
STREAM_MODULE = "docs"
#: Scope type of the per-document stream.
DOC_SCOPE = "doc"


def doc_stream(document_id) -> str:
    """``docs:doc:<id>`` — the resumable journal stream of one document."""
    from stapel_core.comm import stream_key

    return stream_key(STREAM_MODULE, DOC_SCOPE, str(document_id))


def socket_path(document_id) -> str:
    """Where a client opens the stream, relative to the deployment's
    WebSocket prefix (the chat canon: the envelope carries the address of
    its own live path)."""
    return f"ws/docs/{document_id}"


def socket_available() -> bool:
    """Whether this deployment serves the docs socket at all.

    ``stapel_realtime`` in INSTALLED_APPS is the host's declaration that
    sockets are part of the deployment (it is what registers the transport
    and arms the substrate's own checks). The document envelope's
    ``socket_path`` is null otherwise — a polling-only host must not hand
    clients an address nothing answers on.
    """
    from django.apps import apps

    return apps.is_installed("stapel_realtime")


def update_payload(payload: bytes, author_id, client_id: str) -> dict:
    """The one journal-update wire shape — live frames and socket replay
    rows alike (the ``?since=`` feed keeps its own richer envelope)."""
    return {
        "update": base64.b64encode(bytes(payload)).decode("ascii"),
        "author_id": str(author_id) if author_id else None,
        "client_id": client_id or "",
    }


def _deliver_frame(stream: str, payload: dict, seq: int) -> bool:
    try:
        from stapel_realtime.delivery import deliver_frame
    except ImportError:  # pragma: no cover - exercised via optional-dep test
        logger.debug("docs: stapel-realtime transport unavailable")
        return False
    return deliver_frame(stream, payload, seq=seq)


def broadcast_updates(document_id, frames) -> None:
    """Push freshly journaled updates to the document stream.

    ``frames`` is ``[(seq, payload dict), …]`` — one frame per row, the
    row's own seq in the envelope. Called from ``transaction.on_commit``:
    the rows are durable, so a subscriber that misses this replays them.
    """
    stream = doc_stream(document_id)
    for seq, payload in frames:
        _deliver_frame(stream, payload, seq=seq)


def revoke_document(document_id, user_id=None, reason: str = "access_revoked") -> None:
    """End open subscriptions now, when the right to watch ends mid-socket.

    ``user_id=None`` revokes the whole stream (the document left the
    visible corpus — trash, purge); a specific user is kicked when their
    grant row dies. Best-effort like all delivery.
    """
    try:
        from stapel_realtime.delivery import revoke
    except ImportError:  # pragma: no cover
        return
    revoke(doc_stream(document_id), user_id, reason=reason)


__all__ = [
    "DOC_SCOPE",
    "STREAM_MODULE",
    "broadcast_updates",
    "doc_stream",
    "revoke_document",
    "socket_available",
    "socket_path",
    "update_payload",
]
