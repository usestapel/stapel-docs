"""comm surface — docs.create_document via call() + emit schema validation.

VALIDATE_SCHEMAS is on (conftest), so these tests enforce the committed
contracts in ``schemas/``: events.py payloads against ``schemas/emits/``,
the Function payload against ``schemas/functions/docs.create_document.json``.
"""
import uuid
from types import SimpleNamespace

import pytest
from django.test import override_settings
from stapel_core.comm import call
from stapel_core.comm.exceptions import FunctionCallError, SchemaValidationError

from stapel_docs import events


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username="alice", email="alice@example.com", password="x"
    )


def _fake_document(**overrides):
    """Duck-typed stand-in matching the attrs events.py reads — lets the
    emit payloads be exercised against their schemas without services."""
    base = dict(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        type="md",
        title="Standup notes",
        folder_id=None,
        owner_id=None,
        head_seq=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestEmitSchemas:
    """events.py payloads must match schemas/emits/ exactly."""

    def test_document_created_nullable_and_filled(self):
        events.emit_document_created(_fake_document())
        events.emit_document_created(
            _fake_document(folder_id=uuid.uuid4(), owner_id=uuid.uuid4())
        )

    def test_document_updated(self):
        events.emit_document_updated(_fake_document())

    def test_document_deleted(self):
        events.emit_document_deleted(_fake_document())

    def test_storage_changed_signed_delta(self):
        events.emit_storage_changed(uuid.uuid4(), 1024)
        events.emit_storage_changed(uuid.uuid4(), -1024)

    def test_bad_emit_payload_rejected(self):
        from stapel_core.comm import emit

        with pytest.raises(SchemaValidationError):
            emit("document.updated", {"document_id": "x"})  # missing fields


@pytest.mark.django_db
class TestCreateDocumentFunction:
    def test_schema_rejects_bad_payload(self):
        with pytest.raises(SchemaValidationError):
            call("docs.create_document", {"workspace_id": str(uuid.uuid4())})

    def test_unknown_type_is_loud(self):
        with pytest.raises(FunctionCallError, match="DocTypeNotRegistered"):
            call(
                "docs.create_document",
                {"workspace_id": str(uuid.uuid4()), "type": "nope", "title": "t"},
            )

    def test_end_to_end_create(self, tmp_path, user):
        pytest.importorskip("stapel_docs.services")
        from stapel_docs.models import Document, Folder
        from stapel_docs.storage import get_storage

        ws = uuid.uuid4()
        body = "# notes\nhello"
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            res = call(
                "docs.create_document",
                {
                    "workspace_id": str(ws),
                    "type": "md",
                    "title": "Standup notes",
                    "folder_path": "/Meetings/2026-08",
                    "body": body,
                    "metadata": {"source": "ironmemo"},
                    "owner_id": str(user.pk),
                },
            )
            doc = Document.objects.get(id=res["document_id"])
            assert doc.workspace_id == ws
            assert doc.type == "md"
            assert doc.title == "Standup notes"
            assert doc.owner_id == user.pk
            assert doc.metadata == {"source": "ironmemo"}
            # folder_path created the chain and attached the document
            parent = Folder.objects.get(workspace_id=ws, name="Meetings")
            child = Folder.objects.get(workspace_id=ws, name="2026-08", parent=parent)
            assert doc.folder_id == child.id
            # body round-trips through the storage seam
            assert doc.snapshot_key
            assert get_storage().get_bytes(doc.snapshot_key) == body.encode("utf-8")

    def test_folder_path_idempotent(self, tmp_path, db):
        pytest.importorskip("stapel_docs.services")
        from stapel_docs.models import Folder

        ws = uuid.uuid4()
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            for title in ("a", "b"):
                call(
                    "docs.create_document",
                    {
                        "workspace_id": str(ws),
                        "type": "txt",
                        "title": title,
                        "folder_path": "/Meetings/2026-08",
                    },
                )
            assert Folder.objects.filter(workspace_id=ws).count() == 2

    def test_vanished_owner_degrades_to_none(self, tmp_path, db):
        pytest.importorskip("stapel_docs.services")
        from stapel_docs.models import Document

        ws = uuid.uuid4()
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            res = call(
                "docs.create_document",
                {
                    "workspace_id": str(ws),
                    "type": "txt",
                    "title": "orphan",
                    "owner_id": str(uuid.uuid4()),  # no such user (erased)
                },
            )
        assert Document.objects.get(id=res["document_id"]).owner_id is None
