"""Folder tree HTTP surface: listing, create, rename/move, tree rules."""
import uuid

import pytest
from django.test import override_settings

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
    """Client authenticated as a user holding every docs capability."""
    api_client.force_authenticate(user=user)
    grant_capabilities(workspace_id, user.pk)
    return api_client


def _create(actor, workspace_id, name, parent_id=None):
    payload = {"workspace_id": str(workspace_id), "name": name}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    resp = actor.post(f"{API}/folders", payload, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def test_list_requires_workspace_id(actor):
    assert actor.get(f"{API}/folders").status_code == 400


def test_deny_by_default_without_grant(api_client, user, workspace_id):
    api_client.force_authenticate(user=user)  # authenticated, zero grants
    resp = api_client.get(f"{API}/folders", {"workspace_id": str(workspace_id)})
    assert resp.status_code == 403
    assert resp.json()["localizable_error"] == "error.403.docs_forbidden"
    resp = api_client.post(
        f"{API}/folders", {"workspace_id": str(workspace_id), "name": "x"}, format="json"
    )
    assert resp.status_code == 403


def test_create_list_and_detail(actor, workspace_id):
    root = _create(actor, workspace_id, "Meetings")
    child = _create(actor, workspace_id, "2026-08", parent_id=root["id"])

    listing = actor.get(f"{API}/folders", {"workspace_id": str(workspace_id)})
    assert listing.status_code == 200
    assert {f["name"] for f in listing.json()} == {"Meetings", "2026-08"}

    # parent_id= (empty) -> workspace roots only.
    roots = actor.get(f"{API}/folders?workspace_id={workspace_id}&parent_id=")
    assert [f["id"] for f in roots.json()] == [root["id"]]

    children = actor.get(
        f"{API}/folders", {"workspace_id": str(workspace_id), "parent_id": root["id"]}
    )
    assert [f["id"] for f in children.json()] == [child["id"]]

    detail = actor.get(f"{API}/folders/{child['id']}")
    assert detail.status_code == 200
    assert detail.json()["parent_id"] == root["id"]


def test_rename_needs_edit_and_move_needs_manage(
    api_client, user, grant_capabilities, workspace_id
):
    from django.core.cache import cache

    api_client.force_authenticate(user=user)
    grant_capabilities(workspace_id, user.pk)  # all caps to build the tree
    a = _create(api_client, workspace_id, "a")
    b = _create(api_client, workspace_id, "b")

    grant_capabilities(workspace_id, user.pk, "docs.view", "docs.edit")
    cache.clear()  # capability verdicts cache 30 s — drop the stale allow
    renamed = api_client.patch(f"{API}/folders/{a['id']}", {"name": "a2"}, format="json")
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "a2"
    moved = api_client.patch(
        f"{API}/folders/{a['id']}", {"parent_id": b["id"]}, format="json"
    )
    assert moved.status_code == 403

    grant_capabilities(workspace_id, user.pk, "docs.view", "docs.manage")
    cache.clear()  # drop the cached deny from the edit-only attempt
    moved = api_client.patch(
        f"{API}/folders/{a['id']}", {"parent_id": b["id"]}, format="json"
    )
    assert moved.status_code == 200
    assert moved.json()["parent_id"] == b["id"]

    # Move back to the root with an explicit null.
    to_root = api_client.patch(
        f"{API}/folders/{a['id']}", {"parent_id": None}, format="json"
    )
    assert to_root.status_code == 200
    assert to_root.json()["parent_id"] is None


def test_move_under_own_descendant_is_a_cycle(actor, workspace_id):
    a = _create(actor, workspace_id, "a")
    b = _create(actor, workspace_id, "b", parent_id=a["id"])
    resp = actor.patch(f"{API}/folders/{a['id']}", {"parent_id": b["id"]}, format="json")
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_folder_cycle"
    # Under itself is the degenerate cycle.
    resp = actor.patch(f"{API}/folders/{a['id']}", {"parent_id": a["id"]}, format="json")
    assert resp.status_code == 400


def test_depth_limit_on_create_and_move(actor, workspace_id):
    with override_settings(STAPEL_DOCS={"FOLDER_MAX_DEPTH": 2}):
        a = _create(actor, workspace_id, "a")
        b = _create(actor, workspace_id, "b", parent_id=a["id"])
        resp = actor.post(
            f"{API}/folders",
            {"workspace_id": str(workspace_id), "name": "c", "parent_id": b["id"]},
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_folder_depth"

        # Moving a 2-high subtree under a depth-1 folder overflows too.
        c = _create(actor, workspace_id, "c")
        resp = actor.patch(
            f"{API}/folders/{a['id']}", {"parent_id": c["id"]}, format="json"
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_folder_depth"


def test_duplicate_live_sibling_names_refused(actor, workspace_id):
    _create(actor, workspace_id, "same")
    # NULL-parent roots are compared in code — SQL can't (model docstring).
    resp = actor.post(
        f"{API}/folders", {"workspace_id": str(workspace_id), "name": "same"}, format="json"
    )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_duplicate_name"

    parent = _create(actor, workspace_id, "parent")
    _create(actor, workspace_id, "child", parent_id=parent["id"])
    resp = actor.post(
        f"{API}/folders",
        {"workspace_id": str(workspace_id), "name": "child", "parent_id": parent["id"]},
        format="json",
    )
    assert resp.status_code == 400

    # Rename onto a sibling's name is the same refusal.
    other = _create(actor, workspace_id, "other", parent_id=parent["id"])
    resp = actor.patch(f"{API}/folders/{other['id']}", {"name": "child"}, format="json")
    assert resp.status_code == 400


def test_cross_workspace_parent_is_404(
    actor, api_client, user, grant_capabilities, workspace_id
):
    other_ws = uuid.uuid4()
    grant_capabilities(other_ws, user.pk)
    foreign = _create(actor, other_ws, "foreign")
    resp = actor.post(
        f"{API}/folders",
        {"workspace_id": str(workspace_id), "name": "x", "parent_id": foreign["id"]},
        format="json",
    )
    assert resp.status_code == 404
    assert resp.json()["localizable_error"] == "error.404.docs_folder_not_found"


def test_trashed_folder_is_404_on_normal_endpoints(actor, workspace_id):
    folder = _create(actor, workspace_id, "gone")
    assert actor.delete(f"{API}/folders/{folder['id']}").status_code == 204
    assert actor.get(f"{API}/folders/{folder['id']}").status_code == 404
    listing = actor.get(f"{API}/folders", {"workspace_id": str(workspace_id)})
    assert listing.json() == []
