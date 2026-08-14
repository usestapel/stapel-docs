"""Object-store transactions (audit DOCS-02): two stores, one outcome.

Object storage cannot roll back with the database. The service layer
reconciles them by ordering — writes are compensated when the surrounding
block fails, deletes wait for the commit — so a failed mutation leaves
neither an orphan object nor a row pointing at bytes that are already gone.
"""
import uuid

import pytest
from django.test import override_settings

from stapel_docs import services
from stapel_docs.models import Document, Revision
from stapel_docs.storage import content_hash, get_storage, snapshot_key

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username=f"u-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def workspace_id():
    return uuid.uuid4()


@pytest.fixture
def document(workspace_id, user):
    return services.create_document(
        workspace_id=workspace_id, type="md", title="Notes", body=b"v1", user=user
    )


class Boom(RuntimeError):
    """A failure raised after the object write, inside the transaction."""


def test_failed_save_leaves_no_orphan_object(monkeypatch, document, workspace_id):
    monkeypatch.setattr(
        services.events, "emit_document_updated", lambda *a, **kw: (_ for _ in ()).throw(Boom())
    )
    orphan_key = snapshot_key(workspace_id, document.id, content_hash(b"v2"))

    with pytest.raises(Boom):
        services.save_content(document.pk, b"v2", expected_seq=1)

    assert not get_storage().head_object(orphan_key)[0], (
        "the rolled-back save left its object behind"
    )
    document.refresh_from_db()
    assert document.head_seq == 1


def test_failed_save_does_not_destroy_the_previous_snapshot(
    monkeypatch, document, workspace_id
):
    """The row still points at v2 after the rollback, so v2's bytes must
    still be there — an object deleted for a transaction that never
    committed is unrecoverable data loss.

    v2 is deliberately the head: within the auto-revision interval it is
    NOT revision-referenced, so it is exactly the object the orphan
    collector would have deleted while the save was still in flight."""
    document, _ = services.save_content(document.pk, b"v2", expected_seq=1)
    head_key = document.snapshot_key
    assert not Revision.objects.filter(document=document, storage_key=head_key).exists()
    assert get_storage().head_object(head_key)[0]

    monkeypatch.setattr(
        services.events, "emit_document_updated", lambda *a, **kw: (_ for _ in ()).throw(Boom())
    )
    with pytest.raises(Boom):
        services.save_content(document.pk, b"v3", expected_seq=document.head_seq)

    document.refresh_from_db()
    assert document.snapshot_key == head_key
    assert get_storage().head_object(head_key)[0], "the head object died with a failed save"
    assert services.read_content(document)[0] == b"v2"


def test_failed_purge_keeps_the_objects_its_rows_point_at(monkeypatch, document):
    document_id = document.pk
    keys = {document.snapshot_key} | set(
        Revision.objects.filter(document=document).values_list("storage_key", flat=True)
    )
    monkeypatch.setattr(
        services.events, "emit_storage_changed", lambda *a, **kw: (_ for _ in ()).throw(Boom())
    )

    with pytest.raises(Boom):
        services.purge_document(document)

    assert Document.objects.filter(pk=document_id).exists()
    storage = get_storage()
    for key in keys:
        assert storage.head_object(key)[0], f"purge destroyed {key} without committing"


def test_committed_purge_still_destroys_the_objects(
    document, django_capture_on_commit_callbacks
):
    keys = {document.snapshot_key} | set(
        Revision.objects.filter(document=document).values_list("storage_key", flat=True)
    )
    with django_capture_on_commit_callbacks(execute=True):
        services.purge_document(document)

    storage = get_storage()
    for key in keys:
        assert not storage.head_object(key)[0], f"{key} survived a committed purge"


def test_compensation_spares_a_pre_existing_content_addressed_object(
    monkeypatch, workspace_id, user
):
    """Keys are content addresses: an object another revision already
    stored is history, not this attempt's orphan."""
    doc = services.create_document(
        workspace_id=workspace_id, type="md", title="Notes", body=b"same", user=user
    )
    services.create_named_revision(doc, "keep", user=user)
    shared_key = doc.snapshot_key
    services.save_content(doc.pk, b"other", expected_seq=doc.head_seq, user=user)

    monkeypatch.setattr(
        services.events, "emit_document_updated", lambda *a, **kw: (_ for _ in ()).throw(Boom())
    )
    doc.refresh_from_db()
    with pytest.raises(Boom):
        services.save_content(doc.pk, b"same", expected_seq=doc.head_seq, user=user)

    assert get_storage().head_object(shared_key)[0], (
        "compensation deleted an object that predated the failed attempt"
    )
