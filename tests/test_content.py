"""Content path: raw GET with ETag, optimistic-lock PUT, auto-revision
policy, content-addressed dedup and orphan cleanup, download URL."""
import uuid

import pytest
from django.test import override_settings

from stapel_docs.models import Document, Revision
from stapel_docs.storage import content_hash, get_storage, snapshot_key

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


@pytest.fixture
def doc(actor, workspace_id):
    resp = actor.post(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "type": "md", "title": "Notes"},
        format="json",
    )
    assert resp.status_code == 201
    return resp.json()


def _put(actor, doc_id, body, seq):
    return actor.put(
        f"{API}/documents/{doc_id}/content",
        data=body,
        content_type="text/markdown",
        HTTP_IF_MATCH=f'"{seq}"',
    )


def test_empty_document_serves_empty_body_at_seq_zero(actor, doc):
    resp = actor.get(f"{API}/documents/{doc['id']}/content")
    assert resp.status_code == 200
    assert resp.content == b""  # md spec's empty_body
    assert resp["Content-Type"].startswith("text/markdown")
    assert resp["ETag"] == '"0"'
    assert resp["X-Docs-Head-Seq"] == "0"


def test_put_requires_if_match(actor, doc):
    resp = actor.put(
        f"{API}/documents/{doc['id']}/content", data=b"x", content_type="text/plain"
    )
    assert resp.status_code == 412
    assert resp.json()["localizable_error"] == "error.412.docs_missing_if_match"
    # A malformed sequence is the same refusal.
    resp = actor.put(
        f"{API}/documents/{doc['id']}/content",
        data=b"x",
        content_type="text/plain",
        HTTP_IF_MATCH="not-a-seq",
    )
    assert resp.status_code == 412


def test_save_roundtrip_and_stale_conflict(actor, user, workspace_id, doc):
    saved = _put(actor, doc["id"], b"v1", 0)
    assert saved.status_code == 200, saved.content
    body = saved.json()
    assert body["head_seq"] == 1
    assert body["revision_id"]  # first save always mints (no revision yet)

    # The snapshot really sits in storage under its content-addressed key.
    key = snapshot_key(workspace_id, doc["id"], content_hash(b"v1"))
    exists, size = get_storage().head_object(key)
    assert exists and size == 2

    read = actor.get(f"{API}/documents/{doc['id']}/content")
    assert read.content == b"v1"
    assert read["ETag"] == '"1"'

    # Bare (unquoted) If-Match integers are accepted too.
    saved = actor.put(
        f"{API}/documents/{doc['id']}/content",
        data=b"v2",
        content_type="text/markdown",
        HTTP_IF_MATCH="1",
    )
    assert saved.status_code == 200
    assert saved.json()["head_seq"] == 2

    stale = _put(actor, doc["id"], b"v3", 1)
    assert stale.status_code == 409
    conflict = stale.json()
    assert conflict["localizable_error"] == "error.409.docs_seq_conflict"
    assert conflict["params"]["head_seq"] == 2
    assert conflict["params"]["saved_by"] == str(user.pk)
    assert conflict["params"]["saved_at"]
    # The losing body was not applied.
    assert actor.get(f"{API}/documents/{doc['id']}/content").content == b"v2"


def test_auto_revision_interval_and_orphan_cleanup(
    actor, workspace_id, doc, django_capture_on_commit_callbacks
):
    # Default interval (300 s): the first save mints, an immediate second
    # save does not; its predecessor snapshot is revision-referenced and
    # survives, while the unreferenced one is orphan-collected.
    #
    # Object DELETES are deferred to on_commit (a rolled-back save must
    # never destroy an object the surviving row points at), so the
    # collection is observed after the commit — which is when it happens.
    k1 = snapshot_key(workspace_id, doc["id"], content_hash(b"v1"))
    k2 = snapshot_key(workspace_id, doc["id"], content_hash(b"v2"))
    k3 = snapshot_key(workspace_id, doc["id"], content_hash(b"v3"))

    with django_capture_on_commit_callbacks(execute=True):
        assert _put(actor, doc["id"], b"v1", 0).json()["revision_id"]
        assert _put(actor, doc["id"], b"v2", 1).json()["revision_id"] is None
        assert _put(actor, doc["id"], b"v3", 2).status_code == 200

    storage = get_storage()
    assert storage.head_object(k1)[0]  # revision keeps it (I1)
    assert not storage.head_object(k2)[0]  # orphan died with the third save
    assert storage.head_object(k3)[0]  # current head


def test_interval_zero_mints_on_every_save(actor, doc):
    with override_settings(STAPEL_DOCS={"AUTO_REVISION_INTERVAL_SECONDS": 0}):
        assert _put(actor, doc["id"], b"a", 0).json()["revision_id"]
        assert _put(actor, doc["id"], b"b", 1).json()["revision_id"]
    assert Revision.objects.filter(document_id=doc["id"]).count() == 2


def test_identical_bodies_share_one_snapshot_object(actor, workspace_id, doc):
    with override_settings(STAPEL_DOCS={"AUTO_REVISION_INTERVAL_SECONDS": 0}):
        _put(actor, doc["id"], b"same", 0)
        _put(actor, doc["id"], b"same", 1)
    key = snapshot_key(workspace_id, doc["id"], content_hash(b"same"))
    row = Document.objects.get(pk=doc["id"])
    assert row.head_seq == 2
    assert row.snapshot_key == key
    # Both revisions point at the single content-addressed object.
    assert set(
        Revision.objects.filter(document_id=doc["id"]).values_list("storage_key", flat=True)
    ) == {key}


def test_download_url_is_opaque(actor, doc):
    # No body yet -> nothing to download.
    assert actor.get(f"{API}/documents/{doc['id']}/download").status_code == 404
    _put(actor, doc["id"], b"v1", 0)
    resp = actor.get(f"{API}/documents/{doc['id']}/download")
    assert resp.status_code == 200
    assert resp.json()["url"]  # opaque — never assume its shape


def test_viewer_cannot_put(api_client, user, grant_capabilities, workspace_id, actor, doc):
    from django.core.cache import cache

    grant_capabilities(workspace_id, user.pk, "docs.view")
    cache.clear()
    assert actor.get(f"{API}/documents/{doc['id']}/content").status_code == 200
    resp = _put(actor, doc["id"], b"x", 0)
    assert resp.status_code == 403
