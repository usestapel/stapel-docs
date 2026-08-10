"""Uploads: presigned upload sessions for type=file — pending documents
hidden from listings, finalize promotes the blob to seq 1."""
import uuid

import pytest
from django.test import override_settings

from stapel_docs.models import Document, Revision, UploadSession
from stapel_docs.storage import get_storage

pytestmark = pytest.mark.django_db

API = "/docs/api/v1"


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
def actor(api_client, user, grant_capabilities, workspace_id):
    api_client.force_authenticate(user=user)
    grant_capabilities(workspace_id, user.pk)
    return api_client


def _open_upload(actor, workspace_id, **overrides):
    payload = {
        "workspace_id": str(workspace_id),
        "title": "meeting.mp4",
        "mime_type": "video/mp4",
    }
    payload.update(overrides)
    resp = actor.post(f"{API}/uploads", payload, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def test_open_upload_creates_hidden_pending_document(actor, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    assert set(ticket) == {"upload_id", "document_id", "key", "put_url"}
    assert ticket["key"].endswith(f"upload-{ticket['upload_id']}")

    row = Document.objects.get(pk=ticket["document_id"])
    assert row.type == "file"
    assert row.metadata == {"upload_pending": True}
    assert row.head_seq == 0

    # Pending documents are EXCLUDED from listings.
    listing = actor.get(f"{API}/documents", {"workspace_id": str(workspace_id)})
    assert listing.json() == []
    # But the envelope endpoint sees them (the client polls its own upload).
    assert actor.get(f"{API}/documents/{ticket['document_id']}").status_code == 200


def test_finalize_without_object_is_upload_state_400(actor, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_upload_state"


def test_finalize_promotes_blob_to_head(actor, user, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    # Under DjangoStorageBackend the presigned PUT degrades to a served URL
    # (not writable) — the client-side PUT is simulated straight into the
    # seam, which is exactly what the S3 profile's presigned PUT would do.
    get_storage().put_bytes(ticket["key"], b"raw video bytes", content_type="video/mp4")

    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 200, resp.content
    envelope = resp.json()
    assert envelope["head_seq"] == 1
    assert envelope["snapshot_seq"] == 1
    assert envelope["size_bytes"] == len(b"raw video bytes")
    assert envelope["mime_type"] == "video/mp4"
    assert envelope["metadata"] == {}
    assert envelope["editor_hint"] == ""  # file: download-only
    assert envelope["collab"] == "snapshot"

    revision = Revision.objects.get(document_id=ticket["document_id"])
    assert revision.seq == 1
    assert revision.kind == "auto"
    assert revision.storage_key == ticket["key"]

    session = UploadSession.objects.get(pk=ticket["upload_id"])
    assert session.state == "finalized"

    listing = actor.get(f"{API}/documents", {"workspace_id": str(workspace_id)})
    assert [d["id"] for d in listing.json()] == [ticket["document_id"]]

    # Byte-preserved original (verdict §9.4): download-and-compare.
    content = actor.get(f"{API}/documents/{ticket['document_id']}/content")
    assert content.content == b"raw video bytes"
    assert content["Content-Type"].startswith("video/mp4")


def test_finalize_twice_is_upload_state_400(actor, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    get_storage().put_bytes(ticket["key"], b"x")
    assert actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize").status_code == 200
    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_upload_state"


def test_upload_requires_edit(api_client, user, grant_capabilities, workspace_id):
    api_client.force_authenticate(user=user)
    grant_capabilities(workspace_id, user.pk, "docs.view")
    resp = api_client.post(
        f"{API}/uploads",
        {"workspace_id": str(workspace_id), "title": "f.bin"},
        format="json",
    )
    assert resp.status_code == 403


def test_unknown_upload_is_404(actor):
    resp = actor.post(f"{API}/uploads/{uuid.uuid4()}/finalize")
    assert resp.status_code == 404
    assert resp.json()["localizable_error"] == "error.404.docs_upload_not_found"
