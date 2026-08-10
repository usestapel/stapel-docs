"""GDPR: export metadata + authored counts; erasure ANONYMIZES authorship
and never destroys surviving workspace content (storage-verdict §3 — the
journal payload carries the user's typed characters, which stay)."""
import uuid

import pytest
from stapel_core.comm import emit

from stapel_docs.gdpr import DocsGDPRProvider
from stapel_docs.models import (
    Document,
    DocumentUpdate,
    Folder,
    Revision,
    UploadSession,
)


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username="alice", email="alice@example.com", password="x"
    )


def _corpus(user):
    """One of every authorship-bearing row, all pointing at *user*."""
    ws = uuid.uuid4()
    folder = Folder.objects.create(workspace_id=ws, name="Root", created_by=user)
    doc = Document.objects.create(
        workspace_id=ws, folder=folder, type="md", title="Shared notes", owner=user
    )
    update = DocumentUpdate.objects.create(
        document=doc, seq=1, payload=b"crdt-insert: user text", author_id=user.pk
    )
    revision = Revision.objects.create(
        document=doc, seq=1, storage_key=f"docs/{ws}/{doc.id}/h.snap", created_by=user
    )
    session = UploadSession.objects.create(
        workspace_id=ws, title="up", key=f"docs/{ws}/staging", created_by=user
    )
    return doc, folder, update, revision, session


@pytest.mark.django_db
class TestExport:
    def test_export_shape(self, user):
        doc, *_ = _corpus(user)
        data = DocsGDPRProvider().export(user.pk)
        assert data["documents"] == [
            {
                "id": str(doc.id),
                "workspace_id": str(doc.workspace_id),
                "type": "md",
                "title": "Shared notes",
                "created_at": doc.created_at.isoformat(),
            }
        ]
        assert data["authored_update_count"] == 1
        assert data["authored_revision_count"] == 1

    def test_export_empty_for_stranger(self, user, db):
        _corpus(user)
        data = DocsGDPRProvider().export(uuid.uuid4())
        assert data["documents"] == []
        assert data["authored_update_count"] == 0
        assert data["authored_revision_count"] == 0


@pytest.mark.django_db
class TestErasure:
    def test_anonymize_nulls_authorship_keeps_content(self, user):
        doc, folder, update, revision, session = _corpus(user)

        DocsGDPRProvider().anonymize(user.pk)

        doc.refresh_from_db()
        folder.refresh_from_db()
        update.refresh_from_db()
        revision.refresh_from_db()
        session.refresh_from_db()
        assert doc.owner_id is None
        assert folder.created_by_id is None
        assert update.author_id is None
        assert revision.created_by_id is None
        assert session.created_by_id is None
        # the co-produced content survives — rows AND the journal's user text
        assert Document.objects.filter(id=doc.id).exists()
        assert bytes(update.payload) == b"crdt-insert: user text"

    def test_delete_maps_to_anonymize(self, user):
        doc, *_ = _corpus(user)
        DocsGDPRProvider().delete(user.pk)
        doc.refresh_from_db()
        assert doc.owner_id is None
        assert Document.objects.filter(id=doc.id).exists()

    def test_erasure_is_idempotent(self, user):
        _corpus(user)
        provider = DocsGDPRProvider()
        provider.delete(user.pk)
        provider.delete(user.pk)  # at-least-once redelivery is harmless
        assert Document.objects.count() == 1

    def test_user_deleted_consumer(self, user):
        doc, *_ = _corpus(user)
        emit("user.deleted", {"user_id": str(user.pk)})
        doc.refresh_from_db()
        assert doc.owner_id is None
        assert Document.objects.filter(id=doc.id).exists()


class TestRegistration:
    def test_provider_registered(self):
        from stapel_core.gdpr import gdpr_registry

        assert "docs" in gdpr_registry.sections
