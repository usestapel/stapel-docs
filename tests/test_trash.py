"""Trash: soft-delete lifecycle, folder cascade, purge with verified object
destruction (verdict §3), retention expiry command."""
import io
import uuid
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from stapel_docs.models import Document, DocumentUpdate, Folder, Revision
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


def _create_doc(actor, workspace_id, **overrides):
    payload = {"workspace_id": str(workspace_id), "type": "md", "title": "Doc"}
    payload.update(overrides)
    resp = actor.post(f"{API}/documents", payload, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def _put(actor, doc_id, body, seq):
    return actor.put(
        f"{API}/documents/{doc_id}/content",
        data=body,
        content_type="text/markdown",
        HTTP_IF_MATCH=f'"{seq}"',
    )


def test_full_lifecycle_to_verified_destruction(actor, workspace_id):
    """create -> save(If-Match) -> 409-on-stale -> revisions -> named ->
    restore -> trash -> restore -> empty-trash with object deletion
    verified against storage."""
    with override_settings(STAPEL_DOCS={"AUTO_REVISION_INTERVAL_SECONDS": 0}):
        doc = _create_doc(actor, workspace_id, title="Life")

        assert _put(actor, doc["id"], b"v1", 0).json()["head_seq"] == 1
        assert _put(actor, doc["id"], b"v2", 1).json()["head_seq"] == 2
        stale = _put(actor, doc["id"], b"late", 1)
        assert stale.status_code == 409
        assert stale.json()["params"]["head_seq"] == 2

        named = actor.post(
            f"{API}/documents/{doc['id']}/revisions", {"name": "keep"}, format="json"
        )
        assert named.status_code == 201

        v1_rev = actor.get(f"{API}/documents/{doc['id']}/revisions").json()[-1]
        restored = actor.post(
            f"{API}/documents/{doc['id']}/revisions/{v1_rev['id']}/restore"
        )
        assert restored.status_code == 200
        assert restored.json()["head_seq"] == 3
        assert actor.get(f"{API}/documents/{doc['id']}/content").content == b"v1"

    # Trash: gone from normal endpoints, visible in the trash listing.
    assert actor.delete(f"{API}/documents/{doc['id']}").status_code == 204
    assert actor.get(f"{API}/documents/{doc['id']}").status_code == 404
    trash = actor.get(f"{API}/trash", {"workspace_id": str(workspace_id)})
    assert trash.status_code == 200
    assert [d["id"] for d in trash.json()["documents"]] == [doc["id"]]

    # Restore brings it back whole.
    assert actor.post(f"{API}/documents/{doc['id']}/restore").status_code == 200
    assert actor.get(f"{API}/documents/{doc['id']}/content").content == b"v1"

    # Trash again and purge by id — content is destroyed, not archived.
    actor.delete(f"{API}/documents/{doc['id']}")
    keys = set(
        Revision.objects.filter(document_id=doc["id"]).values_list(
            "storage_key", flat=True
        )
    )
    keys.add(Document.objects.get(pk=doc["id"]).snapshot_key)
    assert keys
    storage = get_storage()
    assert any(storage.head_object(k)[0] for k in keys)

    emptied = actor.post(
        f"{API}/trash/empty",
        {"workspace_id": str(workspace_id), "ids": [doc["id"]]},
        format="json",
    )
    assert emptied.status_code == 200
    assert emptied.json() == {"folders": 0, "documents": 1}

    # O(document), total: rows, journal, revisions and every object gone.
    assert not Document.objects.filter(pk=doc["id"]).exists()
    assert not Revision.objects.filter(document_id=doc["id"]).exists()
    assert not DocumentUpdate.objects.filter(document_id=doc["id"]).exists()
    for key in keys:
        assert not storage.head_object(key)[0], key


def test_folder_trash_cascades_and_restores_subtree(actor, workspace_id):
    parent = actor.post(
        f"{API}/folders", {"workspace_id": str(workspace_id), "name": "p"}, format="json"
    ).json()
    child = actor.post(
        f"{API}/folders",
        {"workspace_id": str(workspace_id), "name": "c", "parent_id": parent["id"]},
        format="json",
    ).json()
    doc = _create_doc(actor, workspace_id, folder_id=child["id"], body="deep")

    assert actor.delete(f"{API}/folders/{parent['id']}").status_code == 204
    assert actor.get(f"{API}/folders/{child['id']}").status_code == 404
    assert actor.get(f"{API}/documents/{doc['id']}").status_code == 404

    trash = actor.get(f"{API}/trash", {"workspace_id": str(workspace_id)}).json()
    assert {f["id"] for f in trash["folders"]} == {parent["id"], child["id"]}
    assert [d["id"] for d in trash["documents"]] == [doc["id"]]

    assert actor.post(f"{API}/folders/{parent['id']}/restore").status_code == 200
    assert actor.get(f"{API}/folders/{child['id']}").status_code == 200
    assert actor.get(f"{API}/documents/{doc['id']}").status_code == 200


def test_empty_trash_without_ids_purges_everything(actor, workspace_id):
    folder = actor.post(
        f"{API}/folders", {"workspace_id": str(workspace_id), "name": "f"}, format="json"
    ).json()
    in_folder = _create_doc(actor, workspace_id, folder_id=folder["id"], body="a")
    loose = _create_doc(actor, workspace_id, title="Loose", body="b")

    actor.delete(f"{API}/folders/{folder['id']}")
    actor.delete(f"{API}/documents/{loose['id']}")

    emptied = actor.post(
        f"{API}/trash/empty", {"workspace_id": str(workspace_id)}, format="json"
    )
    assert emptied.status_code == 200
    assert emptied.json() == {"folders": 1, "documents": 2}
    assert not Folder.objects.filter(workspace_id=workspace_id).exists()
    assert not Document.objects.filter(workspace_id=workspace_id).exists()
    for doc_id in (in_folder["id"], loose["id"]):
        assert not Revision.objects.filter(document_id=doc_id).exists()


def test_not_trashed_id_refuses_the_purge(actor, workspace_id):
    live = _create_doc(actor, workspace_id, body="alive")
    resp = actor.post(
        f"{API}/trash/empty",
        {"workspace_id": str(workspace_id), "ids": [live["id"]]},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_not_trashed"
    assert Document.objects.filter(pk=live["id"]).exists()

    unknown = actor.post(
        f"{API}/trash/empty",
        {"workspace_id": str(workspace_id), "ids": [str(uuid.uuid4())]},
        format="json",
    )
    assert unknown.status_code == 400


def test_trash_endpoints_require_manage(
    api_client, user, grant_capabilities, workspace_id, actor
):
    from django.core.cache import cache

    doc = _create_doc(actor, workspace_id)
    grant_capabilities(workspace_id, user.pk, "docs.view", "docs.edit")
    cache.clear()
    assert actor.delete(f"{API}/documents/{doc['id']}").status_code == 403
    assert (
        actor.get(f"{API}/trash", {"workspace_id": str(workspace_id)}).status_code == 403
    )
    assert (
        actor.post(
            f"{API}/trash/empty", {"workspace_id": str(workspace_id)}, format="json"
        ).status_code
        == 403
    )


def test_purge_expired_command_honors_retention(actor, workspace_id):
    expired = _create_doc(actor, workspace_id, title="Old", body="old")
    fresh = _create_doc(actor, workspace_id, title="New", body="new")
    actor.delete(f"{API}/documents/{expired['id']}")
    actor.delete(f"{API}/documents/{fresh['id']}")

    # Backdate one deletion beyond TRASH_RETENTION_DAYS (default 30).
    Document.objects.filter(pk=expired["id"]).update(
        deleted_at=timezone.now() - timedelta(days=31)
    )

    out = io.StringIO()
    call_command("docs_purge_expired", stdout=out)
    assert "purged 1 documents, 0 folders" in out.getvalue()

    assert not Document.objects.filter(pk=expired["id"]).exists()
    assert Document.objects.filter(pk=fresh["id"]).exists()  # still within retention
