"""Revisions: listing, named revisions (manage), self-contained snapshot
reads (I1), restore-as-new-head (history never rewritten)."""
import uuid

import pytest
from django.test import override_settings

from stapel_docs.models import Revision

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
        {"workspace_id": str(workspace_id), "type": "txt", "title": "Story"},
        format="json",
    )
    assert resp.status_code == 201
    return resp.json()


def _put(actor, doc_id, body, seq):
    resp = actor.put(
        f"{API}/documents/{doc_id}/content",
        data=body,
        content_type="text/plain",
        HTTP_IF_MATCH=f'"{seq}"',
    )
    assert resp.status_code == 200, resp.content
    return resp.json()


def test_listing_orders_newest_first(actor, doc):
    with override_settings(STAPEL_DOCS={"AUTO_REVISION_INTERVAL_SECONDS": 0}):
        _put(actor, doc["id"], b"v1", 0)
        _put(actor, doc["id"], b"v2", 1)
    resp = actor.get(f"{API}/documents/{doc['id']}/revisions")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["seq"] for r in rows] == [2, 1]
    assert all(r["kind"] == "auto" for r in rows)


def test_named_revision_requires_body_and_manage(
    actor, user, grant_capabilities, workspace_id, doc
):
    from django.core.cache import cache

    # No body yet -> nothing to name.
    resp = actor.post(
        f"{API}/documents/{doc['id']}/revisions", {"name": "empty"}, format="json"
    )
    assert resp.status_code == 400

    _put(actor, doc["id"], b"v1", 0)

    grant_capabilities(workspace_id, user.pk, "docs.view", "docs.edit")
    cache.clear()
    resp = actor.post(
        f"{API}/documents/{doc['id']}/revisions", {"name": "milestone"}, format="json"
    )
    assert resp.status_code == 403

    grant_capabilities(workspace_id, user.pk)
    cache.clear()
    resp = actor.post(
        f"{API}/documents/{doc['id']}/revisions", {"name": "milestone"}, format="json"
    )
    assert resp.status_code == 201, resp.content
    named = resp.json()
    assert named["kind"] == "named"
    assert named["name"] == "milestone"
    assert named["seq"] == 1
    assert named["created_by"] == str(user.pk)


def test_naming_same_head_twice_renames(actor, doc):
    _put(actor, doc["id"], b"v1", 0)
    first = actor.post(
        f"{API}/documents/{doc['id']}/revisions", {"name": "a"}, format="json"
    ).json()
    second = actor.post(
        f"{API}/documents/{doc['id']}/revisions", {"name": "b"}, format="json"
    ).json()
    assert second["id"] == first["id"]
    assert second["name"] == "b"
    assert Revision.objects.filter(document_id=doc["id"], kind="named").count() == 1


def test_revision_content_is_self_contained(actor, doc):
    with override_settings(STAPEL_DOCS={"AUTO_REVISION_INTERVAL_SECONDS": 0}):
        _put(actor, doc["id"], b"version one", 0)
        _put(actor, doc["id"], b"version two", 1)
    revisions = actor.get(f"{API}/documents/{doc['id']}/revisions").json()
    oldest = revisions[-1]
    # I1: the revision's storage key yields the FULL state on its own.
    resp = actor.get(
        f"{API}/documents/{doc['id']}/revisions/{oldest['id']}/content"
    )
    assert resp.status_code == 200
    assert resp.content == b"version one"
    assert resp["Content-Type"].startswith("text/plain")

    # Download URLs are refused unless the deployment accepts non-expiring
    # links from the default backend (tests/test_content.py pins that rule).
    with override_settings(STAPEL_DOCS={"ALLOW_UNEXPIRING_DOWNLOAD_URLS": True}):
        download = actor.get(
            f"{API}/documents/{doc['id']}/revisions/{oldest['id']}/download"
        )
    assert download.status_code == 200
    assert download.json()["url"]


def test_restore_is_a_new_head_never_a_rewrite(actor, workspace_id, doc):
    with override_settings(STAPEL_DOCS={"AUTO_REVISION_INTERVAL_SECONDS": 0}):
        _put(actor, doc["id"], b"v1", 0)
        _put(actor, doc["id"], b"v2", 1)
        revisions = actor.get(f"{API}/documents/{doc['id']}/revisions").json()
        v1_revision = revisions[-1]

        resp = actor.post(
            f"{API}/documents/{doc['id']}/revisions/{v1_revision['id']}/restore"
        )
        assert resp.status_code == 200, resp.content
        result = resp.json()
        assert result["head_seq"] == 3
        assert result["revision_id"]  # restore always mints

    content = actor.get(f"{API}/documents/{doc['id']}/content")
    assert content.content == b"v1"
    assert content["ETag"] == '"3"'

    # History intact: v1, v2 and the restore-minted revision all present.
    seqs = [r["seq"] for r in actor.get(f"{API}/documents/{doc['id']}/revisions").json()]
    assert seqs == [3, 2, 1]


def test_restore_requires_edit(api_client, user, grant_capabilities, workspace_id, actor, doc):
    from django.core.cache import cache

    _put(actor, doc["id"], b"v1", 0)
    revision = actor.get(f"{API}/documents/{doc['id']}/revisions").json()[0]
    grant_capabilities(workspace_id, user.pk, "docs.view")
    cache.clear()
    resp = actor.post(
        f"{API}/documents/{doc['id']}/revisions/{revision['id']}/restore"
    )
    assert resp.status_code == 403


def test_unknown_revision_is_404(actor, doc):
    _put(actor, doc["id"], b"v1", 0)
    resp = actor.get(
        f"{API}/documents/{doc['id']}/revisions/{uuid.uuid4()}/content"
    )
    assert resp.status_code == 404
    assert resp.json()["localizable_error"] == "error.404.docs_revision_not_found"
