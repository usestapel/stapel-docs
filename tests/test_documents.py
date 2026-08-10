"""Document HTTP surface: create, envelope, listing filters, patch/move,
registry degrade, export error mapping."""
import uuid

import pytest
from django.test import override_settings

from stapel_docs.doc_types import DocTypeSpec, register_doc_type, unregister_doc_type
from stapel_docs.exporters import ExporterUnavailable, ExportUnsupportedType

pytestmark = pytest.mark.django_db

API = "/docs/api/v1"


class UnsupportedExporter:
    """Test exporter refusing the type (maps to 400)."""

    formats = ("unsup",)

    def export(self, document, body, spec):
        raise ExportUnsupportedType(document.type)


class BrokenDepExporter:
    """Test exporter with a missing optional dependency (maps to 503)."""

    formats = ("brokendep",)

    def export(self, document, body, spec):
        raise ExporterUnavailable("not installed")


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
    payload = {"workspace_id": str(workspace_id), "type": "md", "title": "Notes"}
    payload.update(overrides)
    resp = actor.post(f"{API}/documents", payload, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def test_create_with_body_and_envelope(actor, workspace_id):
    doc = _create_doc(actor, workspace_id, body="# hello")
    assert doc["type"] == "md"
    assert doc["head_seq"] == 1
    assert doc["snapshot_seq"] == 1
    assert doc["size_bytes"] == len(b"# hello")
    # Registry-derived presentation rides the envelope (design §7.4 p.1).
    assert doc["editor_hint"] == "markdown"
    assert doc["collab"] == "snapshot"
    assert doc["diffable"] is True
    assert doc["folder_id"] is None
    assert doc["metadata"] == {}

    detail = actor.get(f"{API}/documents/{doc['id']}")
    assert detail.status_code == 200
    assert detail.json()["head_seq"] == 1


def test_create_without_body_stores_nothing(actor, workspace_id):
    doc = _create_doc(actor, workspace_id)
    assert doc["head_seq"] == 0
    assert doc["size_bytes"] == 0


def test_unknown_type_is_400(actor, workspace_id):
    resp = actor.post(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "type": "nope", "title": "x"},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_unknown_type"


def test_listing_filters(actor, workspace_id):
    folder = actor.post(
        f"{API}/folders", {"workspace_id": str(workspace_id), "name": "f"}, format="json"
    ).json()
    _create_doc(actor, workspace_id, title="Alpha report")
    _create_doc(actor, workspace_id, title="beta Report", type="txt")
    in_folder = _create_doc(actor, workspace_id, title="In folder", folder_id=folder["id"])

    all_docs = actor.get(f"{API}/documents", {"workspace_id": str(workspace_id)})
    assert len(all_docs.json()) == 3

    by_folder = actor.get(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "folder_id": folder["id"]},
    )
    assert [d["id"] for d in by_folder.json()] == [in_folder["id"]]

    by_type = actor.get(
        f"{API}/documents", {"workspace_id": str(workspace_id), "type": "txt"}
    )
    assert [d["title"] for d in by_type.json()] == ["beta Report"]

    by_q = actor.get(
        f"{API}/documents", {"workspace_id": str(workspace_id), "q": "report"}
    )
    assert {d["title"] for d in by_q.json()} == {"Alpha report", "beta Report"}


def test_patch_title_is_edit_and_move_is_manage(
    api_client, user, grant_capabilities, workspace_id
):
    from django.core.cache import cache

    api_client.force_authenticate(user=user)
    grant_capabilities(workspace_id, user.pk)
    doc = _create_doc(api_client, workspace_id)
    folder = api_client.post(
        f"{API}/folders", {"workspace_id": str(workspace_id), "name": "f"}, format="json"
    ).json()

    grant_capabilities(workspace_id, user.pk, "docs.view", "docs.edit")
    cache.clear()
    patched = api_client.patch(
        f"{API}/documents/{doc['id']}",
        {"title": "Renamed", "metadata": {"a": 1}},
        format="json",
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Renamed"
    assert patched.json()["metadata"] == {"a": 1}

    moved = api_client.patch(
        f"{API}/documents/{doc['id']}", {"folder_id": folder["id"]}, format="json"
    )
    assert moved.status_code == 403

    grant_capabilities(workspace_id, user.pk, "docs.view", "docs.manage")
    cache.clear()
    moved = api_client.patch(
        f"{API}/documents/{doc['id']}", {"folder_id": folder["id"]}, format="json"
    )
    assert moved.status_code == 200
    assert moved.json()["folder_id"] == folder["id"]


def test_move_to_cross_workspace_folder_is_404(
    actor, user, grant_capabilities, workspace_id
):
    other_ws = uuid.uuid4()
    grant_capabilities(other_ws, user.pk)
    foreign = actor.post(
        f"{API}/folders", {"workspace_id": str(other_ws), "name": "foreign"}, format="json"
    ).json()
    doc = _create_doc(actor, workspace_id)
    resp = actor.patch(
        f"{API}/documents/{doc['id']}", {"folder_id": foreign["id"]}, format="json"
    )
    assert resp.status_code == 404


def test_trash_and_restore(actor, workspace_id):
    doc = _create_doc(actor, workspace_id)
    assert actor.delete(f"{API}/documents/{doc['id']}").status_code == 204
    assert actor.get(f"{API}/documents/{doc['id']}").status_code == 404
    assert actor.get(f"{API}/documents", {"workspace_id": str(workspace_id)}).json() == []

    restored = actor.post(f"{API}/documents/{doc['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None
    listing = actor.get(f"{API}/documents", {"workspace_id": str(workspace_id)})
    assert [d["id"] for d in listing.json()] == [doc["id"]]


def test_vanished_type_degrades_to_file_presentation(actor, workspace_id):
    register_doc_type(
        DocTypeSpec(slug="exotic", label="Exotic", editor_hint="exotic", diffable=True)
    )
    try:
        doc = _create_doc(actor, workspace_id, type="exotic", body="payload")
    finally:
        unregister_doc_type("exotic")

    # Registry entry vanished: read-only, never unreadable (verdict §7.3).
    detail = actor.get(f"{API}/documents/{doc['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["editor_hint"] == ""
    assert body["collab"] == "snapshot"
    assert body["diffable"] is False

    content = actor.get(f"{API}/documents/{doc['id']}/content")
    assert content.status_code == 200
    assert content.content == b"payload"


def test_export_error_mapping(actor, workspace_id):
    doc = _create_doc(actor, workspace_id, body="text")

    resp = actor.get(f"{API}/documents/{doc['id']}/export", {"format": "nope"})
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_export_format"

    # Builtin pdf exporter renders text-ish types.
    resp = actor.get(f"{API}/documents/{doc['id']}/export", {"format": "pdf"})
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")

    overlay = {
        "EXPORTERS": {
            "unsup": "stapel_docs.tests.test_documents.UnsupportedExporter",
            "brokendep": "stapel_docs.tests.test_documents.BrokenDepExporter",
        }
    }
    with override_settings(STAPEL_DOCS=overlay):
        resp = actor.get(f"{API}/documents/{doc['id']}/export", {"format": "unsup"})
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_type_not_editable"
        resp = actor.get(f"{API}/documents/{doc['id']}/export", {"format": "brokendep"})
        assert resp.status_code == 503
