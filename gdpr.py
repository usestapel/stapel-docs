"""GDPR data handler for stapel-docs.

Documents are co-produced workspace content, not private user artifacts,
so the policy is **anonymize, not delete** (storage-verdict §3): a user's
erasure inside surviving workspace content nulls their authorship —
``DocumentUpdate.author_id`` (deliberately FK-less; the journal payload
carries the user's typed characters, which stay as workspace content),
``Revision.created_by``, ``Document.owner``, ``Folder.created_by``,
``UploadSession.created_by``. Nothing a workspace still relies on is
destroyed. Content destruction happens only through trash purge /
retention, never through this provider.

Registered as a provider (monolith mode, ``apps.py:ready()``) and driven
by the ``@on_action("user.deleted")`` consumer (``actions.py``).
"""
from __future__ import annotations

import logging

from stapel_core.gdpr import GDPRProvider

logger = logging.getLogger(__name__)


class DocsGDPRProvider(GDPRProvider):
    section = "docs"

    def export(self, user_id) -> dict:
        """Metadata of documents the user owns plus counts of journal rows
        and revisions they authored (the authored *text* is workspace
        content and is not the user's export slice)."""
        from .models import Document, DocumentUpdate, Revision

        rows = Document.objects.filter(owner_id=user_id, deleted_at__isnull=True)
        return {
            "documents": [
                {
                    "id": str(d.id),
                    "workspace_id": str(d.workspace_id),
                    "type": d.type,
                    "title": d.title,
                    "created_at": d.created_at.isoformat(),
                }
                for d in rows
            ],
            "authored_update_count": DocumentUpdate.objects.filter(
                author_id=user_id
            ).count(),
            "authored_revision_count": Revision.objects.filter(
                created_by_id=user_id
            ).count(),
        }

    def delete(self, user_id) -> None:
        # A user.deleted consumer maps to anonymize for this module:
        # documents survive their authors (storage-verdict §3).
        self.anonymize(user_id)

    def anonymize(self, user_id) -> None:
        """Null every authorship reference. Idempotent (a nulled row nulls
        to itself), so at-least-once redelivery is harmless."""
        from .models import Document, DocumentUpdate, Folder, Revision, UploadSession

        DocumentUpdate.objects.filter(author_id=user_id).update(author_id=None)
        Revision.objects.filter(created_by_id=user_id).update(created_by=None)
        Document.objects.filter(owner_id=user_id).update(owner=None)
        Folder.objects.filter(created_by_id=user_id).update(created_by=None)
        UploadSession.objects.filter(created_by_id=user_id).update(created_by=None)


__all__ = ["DocsGDPRProvider"]
