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

The sharing axis is the exception that proves the policy (axis §6): a
grant is not co-produced content, it is a standing permission ABOUT one
person, so the rows naming the erased user as SUBJECT are deleted outright
and the bearer links they sponsored are revoked. Leaving either behind
would keep an erased account's access alive — the link most of all, since
it works in hands nobody can name. Their *provenance* (``granted_by``,
``created_by``) is anonymized like any other authorship: the grant somebody
else still holds keeps existing, made by "somebody, no longer known".

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

    def delete(self, user_id) -> dict:
        # A user.deleted consumer maps to anonymize for this module:
        # documents survive their authors (storage-verdict §3).
        return self.anonymize(user_id)

    def anonymize(self, user_id) -> dict:
        """Null every authorship reference, DELETE the per-user state and the
        subject's own share grants, revoke the links they sponsored, and
        return how many rows changed.

        The two policies are different on purpose. Authorship is workspace
        content and survives anonymized (see the module docstring). Stars
        and recents are not content at all — they are one person's private
        view of the corpus, they mean nothing without that person, and an
        anonymized star would be a bookmark nobody can reach and nobody can
        clear. So they die with the account (drive-spec §3.1/§3.2); the
        CASCADE on the user FK does the same thing when the auth row itself
        goes, and doing it here keeps the receipt honest about what left.

        Idempotent (a nulled row nulls to itself), so at-least-once
        redelivery is harmless — and the second delivery reports zeros,
        which is what a receipt should say about work that was already done.
        The counts are what ``gdpr.section.erased`` carries for an
        ``account`` subject (``erasure.erase_account``); the base
        :class:`GDPRProvider` ignores a return value, so the orchestrator's
        in-process path is unaffected.
        """
        from django.utils import timezone

        from .models import (
            Document,
            DocumentAccess,
            DocumentLink,
            DocumentUpdate,
            Folder,
            RecentEntry,
            Revision,
            Star,
            UploadSession,
        )

        stars_deleted, _ = Star.objects.filter(user_id=user_id).delete()
        recents_deleted, _ = RecentEntry.objects.filter(user_id=user_id).delete()
        # The subject's own access, gone — not anonymized: an ACL row whose
        # subject is nulled would grant to nobody and read as a live share.
        access_deleted, _ = DocumentAccess.objects.filter(
            subject_kind=DocumentAccess.SUBJECT_USER, user_id=user_id
        ).delete()
        # Their links die with them, sponsor-first: authorize() already
        # refuses a link whose creator lost the capability, and this makes
        # the row say so instead of relying on a live check forever.
        links_revoked = DocumentLink.objects.filter(
            created_by_id=user_id, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
        return {
            "stars_deleted": stars_deleted,
            "recents_deleted": recents_deleted,
            "access_deleted": access_deleted,
            "links_revoked": links_revoked,
            "access_anonymized": DocumentAccess.objects.filter(
                granted_by_id=user_id
            ).update(granted_by=None),
            "links_anonymized": DocumentLink.objects.filter(
                created_by_id=user_id
            ).update(created_by=None),
            "updates_anonymized": DocumentUpdate.objects.filter(
                author_id=user_id
            ).update(author_id=None),
            "revisions_anonymized": Revision.objects.filter(
                created_by_id=user_id
            ).update(created_by=None),
            "documents_anonymized": Document.objects.filter(owner_id=user_id).update(
                owner=None
            ),
            "folders_anonymized": Folder.objects.filter(created_by_id=user_id).update(
                created_by=None
            ),
            "upload_sessions_anonymized": UploadSession.objects.filter(
                created_by_id=user_id
            ).update(created_by=None),
        }


__all__ = ["DocsGDPRProvider"]
