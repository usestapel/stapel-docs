"""INGEST seam: ``STAPEL_DOCS["INGEST"]`` routes a host action into a
created document through a host-owned mapper (docs never learns the
foreign event schema — design §2). ready() already wired the (empty)
default; tests re-run :func:`wire_ingest` after overriding settings."""
import uuid

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from stapel_core.comm import emit

from stapel_docs.actions import wire_ingest

MAPPER_PATH = "stapel_docs.tests.test_ingest.transcript_mapper"


def transcript_mapper(payload):
    """Fake host mapper: meeting.completed payload -> create_document kwargs."""
    return {
        "workspace_id": payload["workspace_id"],
        "type": "md",
        "title": payload["title"],
        "folder_path": "/Meetings",
        "body": payload["transcript"].encode("utf-8"),
        "metadata": {"meeting_id": payload["meeting_id"]},
    }


not_a_mapper = "just a string, not callable"


@pytest.fixture
def rewire():
    """Restore the default (empty) INGEST wiring after the test."""
    yield
    wire_ingest()


class TestWireIngest:
    def test_broken_mapper_import_raises(self, rewire):
        with override_settings(
            STAPEL_DOCS={"INGEST": {"x.y": "no.such.module.mapper"}}
        ):
            with pytest.raises(ImproperlyConfigured, match="cannot be imported"):
                wire_ingest()

    def test_non_callable_mapper_raises(self, rewire):
        with override_settings(
            STAPEL_DOCS={
                "INGEST": {"x.y": "stapel_docs.tests.test_ingest.not_a_mapper"}
            }
        ):
            with pytest.raises(ImproperlyConfigured, match="not callable"):
                wire_ingest()


@pytest.mark.django_db
class TestIngestRouting:
    def test_mapped_action_creates_document(self, tmp_path, rewire):
        pytest.importorskip("stapel_docs.services")
        from stapel_docs.models import Document, Folder

        ws = uuid.uuid4()
        with override_settings(
            MEDIA_ROOT=str(tmp_path),
            STAPEL_DOCS={"INGEST": {"meeting.completed": MAPPER_PATH}},
        ):
            wire_ingest()
            emit(
                "meeting.completed",
                {
                    "workspace_id": str(ws),
                    "title": "Weekly sync",
                    "transcript": "we shipped",
                    "meeting_id": "m-1",
                },
            )
            doc = Document.objects.get(workspace_id=ws)
            assert doc.type == "md"
            assert doc.title == "Weekly sync"
            assert doc.metadata == {"meeting_id": "m-1"}
            assert doc.folder_id == Folder.objects.get(workspace_id=ws, name="Meetings").id

    def test_rewire_does_not_stack_duplicates(self, tmp_path, rewire):
        pytest.importorskip("stapel_docs.services")
        from stapel_docs.models import Document

        ws = uuid.uuid4()
        with override_settings(
            MEDIA_ROOT=str(tmp_path),
            STAPEL_DOCS={"INGEST": {"meeting.completed": MAPPER_PATH}},
        ):
            wire_ingest()
            wire_ingest()  # e.g. ready() ran again — must stay single-shot
            emit(
                "meeting.completed",
                {
                    "workspace_id": str(ws),
                    "title": "Once",
                    "transcript": "x",
                    "meeting_id": "m-2",
                },
            )
            assert Document.objects.filter(workspace_id=ws).count() == 1

    def test_stale_subscription_is_inert_after_rewire(self, db, rewire):
        from stapel_docs.models import Document

        with override_settings(
            STAPEL_DOCS={"INGEST": {"meeting.completed": MAPPER_PATH}}
        ):
            wire_ingest()
        wire_ingest()  # back to the empty default — mapper dropped
        # the dispatcher stays subscribed (no unsubscribe exists) but must
        # be a no-op: a live mapper would KeyError on this payload
        emit("meeting.completed", {"unrelated": True})
        assert Document.objects.count() == 0
