"""Subject-scoped erasure (GDPR Art. 17) — the slice stapel-docs owns.

stapel-gdpr 0.5.0 generalized account closure into an erasure keyed by a
*subject*: ``{subject_type, subject_key}``. An owner library erases what it
holds about that subject, counts what it removed, and confirms with a
receipt. This module is the whole answer for docs; ``actions.py`` is only
the transport (``gdpr.erasure.requested`` / ``gdpr.owner.probe``).

Three subjects, two different policies — deliberately:

- ``document`` and ``workspace`` are **hard-deleted**: rows, the update
  journal, every historical revision and every object under the document's
  storage prefix, through the module's own purge path (``services.
  purge_document``), which is O(document) and idempotent. Trash state is
  irrelevant — an erasure is not a trash operation, so live and trashed
  rows die alike, and the retention window is not waited out.
- ``account`` is **anonymized**, not deleted (storage-verdict §3): a
  document is co-produced workspace content, not a private user artifact,
  so a member's erasure nulls their authorship and leaves the workspace's
  documents readable. Destroying them would erase other people's data
  under the banner of erasing one person's. This is the policy the
  ``user.deleted`` consumer and the GDPR provider always had; erasure now
  routes *through* the same code instead of beside it.

Every entry point returns a counts mapping — "it says what it did", not
"it says it ran" — and is idempotent: a redelivered request finds nothing
left and receipts zeros.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: This module's name in ``STAPEL_GDPR["DATA_OWNERS"]``. Same string as the
#: GDPR provider's ``section``: one owner, one name, whichever protocol
#: reaches it.
OWNER = "docs"

#: The subject types this owner claims. Must match its row in the host's
#: ``DATA_OWNERS`` map — gdpr creates an ``ErasurePart`` for this owner only
#: for subjects listed here, and ``gdpr.owner.alive`` reports this list.
SUBJECT_TYPES = ("account", "workspace", "document")


def erase(subject_type: str, subject_key, *, workspace_id=None) -> dict:
    """Erase everything docs owns about one subject; return what was removed.

    Unknown subject types are refused with :class:`ValueError` — a typo must
    not receipt as an empty success, which would certify an erasure nobody
    performed.
    """
    if subject_type == "account":
        return erase_account(subject_key)
    if subject_type == "workspace":
        return erase_workspace(subject_key)
    if subject_type == "document":
        return erase_document(subject_key, workspace_id=workspace_id)
    raise ValueError(f"stapel-docs does not own subject type {subject_type!r}")


def erase_account(user_id) -> dict:
    """Anonymize one user's authorship everywhere it appears (see module
    docstring for why this is not a delete). Idempotent — a nulled row
    nulls to itself."""
    from .gdpr import DocsGDPRProvider

    counts = DocsGDPRProvider().anonymize(user_id)
    logger.info("docs: authorship anonymized for account %s (%s)", user_id, counts)
    return counts


def erase_workspace(workspace_id) -> dict:
    """Destroy a workspace's whole corpus: every document (live or trashed)
    with its journal, revisions and objects, then the folder tree and any
    pending upload sessions with their staging objects."""
    from django.db import transaction

    from .models import Document, Folder, UploadSession

    counts = _zero_counts()
    # Materialized: the loop deletes the rows it walks over.
    for document in list(Document.objects.filter(workspace_id=workspace_id)):
        _add(counts, _purge_one(document))

    counts["upload_sessions"] += _purge_upload_sessions(
        UploadSession.objects.filter(workspace_id=workspace_id), counts
    )

    with transaction.atomic():
        # Documents are already gone and `Document.folder` is SET_NULL, so the
        # only rows this cascade reaches are child folders — the count is
        # folders, nothing else.
        _, per_model = Folder.objects.filter(workspace_id=workspace_id).delete()
    counts["folders"] += int(per_model.get("docs.Folder", 0))
    logger.info("docs: workspace %s erased (%s)", workspace_id, counts)
    return counts


def erase_document(document_id, *, workspace_id=None) -> dict:
    """Destroy one document: rows, journal, every revision and every object
    of its history, plus upload sessions that were still pointing at it."""
    from .models import Document, UploadSession

    counts = _zero_counts()
    document = Document.objects.filter(id=document_id).first()
    if document is not None and workspace_id and str(document.workspace_id) != str(
        workspace_id
    ):
        # The request contradicts the row. Erasing anyway would obey a pair
        # nobody vouched for; receipting zeros would certify an erasure that
        # did not happen. Refuse loudly — the part stays open and times out,
        # which is exactly the visibility gdpr's timeout exists for.
        raise ValueError(
            f"document {document_id} is not in workspace {workspace_id}"
        )
    if document is None:
        # Already gone (redelivery, or the host purged it first): an erasure
        # that finds nothing has still erased everything it owns.
        logger.info("docs: document %s already absent — erasure is a no-op", document_id)
        return counts

    sessions = UploadSession.objects.filter(document_id=document.id)
    counts["upload_sessions"] += _purge_upload_sessions(sessions, counts)
    _add(counts, _purge_one(document))
    logger.info("docs: document %s erased (%s)", document_id, counts)
    return counts


# ── internals ────────────────────────────────────────────────────────


def _zero_counts() -> dict:
    return {
        "documents": 0,
        "revisions": 0,
        "updates": 0,
        "folders": 0,
        "upload_sessions": 0,
        "storage_objects": 0,
    }


def _add(counts: dict, more: dict) -> None:
    for key, value in more.items():
        counts[key] = counts.get(key, 0) + value


def _purge_one(document) -> dict:
    """Purge one document through the module's own purge path and report
    what that removed. Counted BEFORE the purge — afterwards there is
    nothing left to count."""
    from .models import DocumentUpdate
    from .services import purge_document

    keys = {
        key
        for key in document.revisions.values_list("storage_key", flat=True)
        if key
    }
    if document.snapshot_key:
        keys.add(document.snapshot_key)
    removed = {
        "documents": 1,
        "revisions": document.revisions.count(),
        "updates": DocumentUpdate.objects.filter(document=document).count(),
        "storage_objects": len(keys),
    }
    purge_document(document)
    return removed


def _purge_upload_sessions(queryset, counts: dict) -> int:
    """Delete pending upload sessions and their staging objects.

    A session's staging key is content the requester uploaded, so it is part
    of the subject's data — leaving it behind would leave the bytes of a
    file that was never finalized in the bucket after the erasure.
    """
    from django.db import transaction

    from .services import storage_transaction

    sessions = list(queryset)
    if not sessions:
        return 0
    with storage_transaction() as stx, transaction.atomic():
        for session in sessions:
            if session.key:
                stx.delete(session.key)
                counts["storage_objects"] += 1
        queryset.delete()
    return len(sessions)


__all__ = [
    "OWNER",
    "SUBJECT_TYPES",
    "erase",
    "erase_account",
    "erase_document",
    "erase_workspace",
]
