"""The docs socket — delivery only, on the fleet's substrate.

One consumer, one stream (:mod:`stapel_docs.realtime`):
``docs:doc:<document_id>``, the document's update journal, resumable by the
journal's own ``seq``. The substrate
(:class:`stapel_realtime.consumers.ResumableStreamConsumer`) owns
authentication, the envelope, heartbeat, backpressure and revoke-to-kick;
this class supplies the two journal hooks and the authorization gate.

**No write frames.** The design settles it (§5.3 p.6): update writes are
REST POSTs, the socket is downstream only. Chat is the fleet's documented
exception; docs is not one.

**One choke point.** ``authorize()`` here is the SAME
``stapel_docs.authz.authorize`` call the HTTP views make — action ``view``,
WITH the document — so a whitelist grantee works over the socket exactly as
over HTTP, and the 0.6.1 lesson (a rule is only as enforced as its worst
call site) does not repeat on a new transport. Fail-closed in both senses:
``deny`` and ``unavailable`` both refuse the subscription — a socket has no
503 to answer, and an outage must not become an open stream.

Channels is an optional extra of ``stapel-realtime``. Importing this module
without it raises a clear ImportError; it is never imported at app-ready
time — a polling-only host pays nothing.
"""
from __future__ import annotations

try:
    from channels.db import database_sync_to_async
except ImportError as exc:  # pragma: no cover - exercised via optional-dep test
    raise ImportError(
        "stapel_docs.consumers requires the optional 'channels' dependency. "
        "Install it with:\n    pip install 'stapel-docs[realtime]'"
    ) from exc

from stapel_realtime.consumers import JournalRow, ResumableStreamConsumer

from .realtime import doc_stream, update_payload

# ── sync helpers (each runs in a thread) ─────────────────────────────


def _may_view(document_id, user) -> bool:
    from django.core.exceptions import ValidationError

    from .authz import ALLOW, Principal, authorize
    from .models import Document

    try:
        document = Document.objects.filter(
            pk=document_id, deleted_at__isnull=True
        ).first()
    except (ValidationError, ValueError):
        return False
    if document is None:
        return False
    principal = Principal(
        user_id=getattr(user, "pk", None),
        is_anonymous=bool(getattr(user, "is_anonymous_account", False)),
    )
    return (
        authorize(
            workspace_id=document.workspace_id,
            principal=principal,
            action="view",
            document=document,
        )
        == ALLOW
    )


def _server_seq(document_id) -> int:
    from .models import Document

    row = Document.objects.filter(pk=document_id).values("head_seq").first()
    return row["head_seq"] if row else 0


def _replay(document_id, after_seq: int, limit: int) -> list:
    from .models import DocumentUpdate

    return [
        JournalRow(
            seq=row.seq,
            payload=update_payload(bytes(row.payload), row.author_id, row.client_id),
        )
        for row in DocumentUpdate.objects.filter(
            document_id=document_id, seq__gt=after_seq
        ).order_by("seq")[:limit]
    ]


# ── the consumer ─────────────────────────────────────────────────────


class DocUpdatesConsumer(ResumableStreamConsumer):
    """One socket ↔ one document's update journal. Read-only, resumable.

    The envelope's ``seq`` is the journal cursor (``DocumentUpdate.seq``);
    the payload is the same ``{update, author_id, client_id}`` shape live
    and replayed, so a client folds both through one code path.
    """

    module = "docs"
    scope_type = "doc"
    stream_key_kwarg = "document_id"

    async def get_stream_key(self) -> str:
        kwargs = (self.scope.get("url_route") or {}).get("kwargs") or {}
        self.document_id = str(kwargs["document_id"])
        return doc_stream(self.document_id)

    async def authorize(self, scope, stream_key) -> bool:
        """The HTTP rule, verbatim: live document + ``authorize(view)``
        WITH the document, so every grant source the axis enables works
        over the socket too. ``unavailable`` refuses like ``deny`` — an
        accept has no 503, and fail-closed is the substrate's contract."""
        return await database_sync_to_async(_may_view)(
            self.document_id, scope.get("user")
        )

    # ── journal hooks ────────────────────────────────────────────────

    async def get_server_seq(self) -> int:
        return await database_sync_to_async(_server_seq)(self.document_id)

    async def get_replay_rows(self, after_seq: int, limit: int):
        return await database_sync_to_async(_replay)(
            self.document_id, after_seq, limit
        )


__all__ = ["DocUpdatesConsumer"]
