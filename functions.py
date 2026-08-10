"""comm surface of stapel-docs (Functions).

Every Function carries a JSON schema in ``schemas/functions/`` — tests run
with ``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails
loudly. Registration happens on import from ``apps.py:ready()``; re-imports
are no-ops.

Provided: ``docs.create_document`` — the main ingest seam (design §6):
ironmemo dumps transcripts/summaries through it. The event-driven variant
is the ``STAPEL_DOCS["INGEST"]`` registry (``actions.py``). Emitted actions
live in ``events.py``.
"""
from stapel_core.comm import function


@function("docs.create_document")
def create_document(payload):
    """Create a document. Output: ``{"document_id": str}``.

    ``body`` is a utf-8 string (the Function payload is JSON — opaque
    binaries go through the upload-session flow, not this seam);
    ``folder_path`` like ``/Meetings/2026-08`` creates folders idempotently.
    An unknown ``type`` raises :class:`~stapel_docs.doc_types.DocTypeNotRegistered`
    — loud, so a caller never silently loses content into a mistyped slug.
    """
    from .doc_types import get_doc_type

    get_doc_type(payload["type"])  # unknown type -> DocTypeNotRegistered

    owner = None
    if payload.get("owner_id"):
        from django.contrib.auth import get_user_model

        # A vanished owner degrades to None: documents are workspace
        # content and authorship is optional (same shape GDPR anonymize
        # leaves behind), so ingest never fails over an erased user.
        owner = get_user_model().objects.filter(pk=payload["owner_id"]).first()

    body = payload.get("body")

    from . import services  # lazy: the comm surface must import alone

    document = services.create_document(
        workspace_id=payload["workspace_id"],
        type=payload["type"],
        title=payload["title"],
        folder_path=payload.get("folder_path"),
        body=body.encode("utf-8") if body is not None else None,
        mime_type=payload.get("mime_type") or "",
        metadata=payload.get("metadata"),
        owner=owner,
    )
    return {"document_id": str(document.id)}
