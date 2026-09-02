"""Search by name — workspace-scoped, tree-wide (drive-spec §3.3).

Pinned here: hits carry their kind and a server-materialized breadcrumb,
workspace A never sees workspace B, an absent query is a 400 rather than a
free full-workspace scan, the ancestor chains cost a bounded number of
queries (the N+1 this endpoint would otherwise be), and the same ``?q=``
filter stays available on the documents listing for the in-folder case.
"""
import uuid

import pytest
from django.test import override_settings

from stapel_docs import services

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


def _folder(actor, workspace_id, name, parent_id=None):
    payload = {"workspace_id": str(workspace_id), "name": name}
    if parent_id:
        payload["parent_id"] = parent_id
    resp = actor.post(f"{API}/folders", payload, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def _doc(actor, workspace_id, title, folder_id=None):
    payload = {"workspace_id": str(workspace_id), "type": "md", "title": title}
    if folder_id:
        payload["folder_id"] = folder_id
    resp = actor.post(f"{API}/documents", payload, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


@pytest.fixture
def tree(actor, workspace_id):
    """/Projects/Alpha/notes.md plus a root-level decoy."""
    projects = _folder(actor, workspace_id, "Projects")
    alpha = _folder(actor, workspace_id, "Alpha reports", projects["id"])
    deep = _doc(actor, workspace_id, "Alpha notes", alpha["id"])
    shallow = _doc(actor, workspace_id, "Alpha summary")
    _doc(actor, workspace_id, "Unrelated")
    return {
        "projects": projects,
        "alpha": alpha,
        "deep": deep,
        "shallow": shallow,
    }


class TestHits:
    def test_folders_and_documents_with_kind(self, actor, workspace_id, tree):
        hits = actor.get(f"{API}/search?workspace_id={workspace_id}&q=alpha").json()
        by_name = {hit["name"]: hit for hit in hits}
        assert by_name["Alpha reports"]["kind"] == "folder"
        assert by_name["Alpha reports"]["type"] is None
        assert by_name["Alpha notes"]["kind"] == "document"
        assert by_name["Alpha notes"]["type"] == "md"
        assert "Unrelated" not in by_name

    def test_search_is_case_insensitive_substring(self, actor, workspace_id, tree):
        hits = actor.get(f"{API}/search?workspace_id={workspace_id}&q=NOTE").json()
        assert [hit["name"] for hit in hits] == ["Alpha notes"]

    def test_breadcrumbs_are_root_first_ancestor_chains(self, actor, workspace_id, tree):
        hits = actor.get(f"{API}/search?workspace_id={workspace_id}&q=alpha").json()
        by_name = {hit["name"]: hit for hit in hits}

        # A nested document: the chain of the folder it lives in.
        assert [node["name"] for node in by_name["Alpha notes"]["breadcrumb"]] == [
            "Projects",
            "Alpha reports",
        ]
        assert by_name["Alpha notes"]["breadcrumb"][0]["id"] == tree["projects"]["id"]
        # A folder: the chain of its PARENTS, itself excluded.
        assert [node["name"] for node in by_name["Alpha reports"]["breadcrumb"]] == [
            "Projects"
        ]
        # A root-level document has no ancestors at all.
        assert by_name["Alpha summary"]["breadcrumb"] == []
        assert by_name["Alpha summary"]["parent_id"] is None

    def test_hits_carry_is_starred(self, actor, workspace_id, tree):
        actor.post(f"{API}/documents/{tree['deep']['id']}/star")
        hits = actor.get(f"{API}/search?workspace_id={workspace_id}&q=alpha").json()
        by_name = {hit["name"]: hit for hit in hits}
        assert by_name["Alpha notes"]["is_starred"] is True
        assert by_name["Alpha summary"]["is_starred"] is False

    def test_trashed_rows_never_appear(self, actor, workspace_id, tree):
        actor.delete(f"{API}/documents/{tree['deep']['id']}")
        actor.delete(f"{API}/folders/{tree['alpha']['id']}")
        hits = actor.get(f"{API}/search?workspace_id={workspace_id}&q=alpha").json()
        assert [hit["name"] for hit in hits] == ["Alpha summary"]

    def test_limit_caps_the_result(self, actor, workspace_id, tree):
        hits = actor.get(
            f"{API}/search?workspace_id={workspace_id}&q=alpha&limit=1"
        ).json()
        assert len(hits) == 1


class TestScopeAndValidation:
    def test_workspace_a_never_sees_workspace_b(self, actor, user, workspace_id,
                                                grant_capabilities):
        other_ws = uuid.uuid4()
        grant_capabilities(other_ws, user.pk)
        _doc(actor, workspace_id, "Shared name here")
        _folder(actor, other_ws, "Shared name there")
        _doc(actor, other_ws, "Shared name elsewhere")

        hits = actor.get(f"{API}/search?workspace_id={workspace_id}&q=shared").json()
        assert [hit["name"] for hit in hits] == ["Shared name here"]

    def test_missing_q_is_a_400(self, actor, workspace_id):
        assert (
            actor.get(f"{API}/search?workspace_id={workspace_id}").status_code == 400
        )

    def test_blank_q_is_a_400(self, actor, workspace_id):
        assert (
            actor.get(f"{API}/search?workspace_id={workspace_id}&q=").status_code == 400
        )
        assert (
            actor.get(f"{API}/search?workspace_id={workspace_id}&q=%20").status_code
            == 400
        )

    def test_missing_workspace_is_a_400(self, actor):
        assert actor.get(f"{API}/search?q=alpha").status_code == 400

    def test_denied_without_view(self, actor, user, workspace_id, grant_capabilities):
        grant_capabilities(workspace_id, user.pk, "docs.nothing")
        assert (
            actor.get(f"{API}/search?workspace_id={workspace_id}&q=a").status_code
            == 403
        )


class TestQueryCost:
    def test_breadcrumbs_do_not_scale_with_hit_count(self, actor, workspace_id,
                                                     django_assert_num_queries):
        """One folder index for the whole workspace, not one walk per hit.

        The assertion is on a CONSTANT: ten hits nested three deep must cost
        the same number of queries as one, or the endpoint is quadratic in
        tree depth the first time a real workspace gets deep.
        """
        root = _folder(actor, workspace_id, "Root")
        mid = _folder(actor, workspace_id, "Mid", root["id"])
        leaf = _folder(actor, workspace_id, "Leaf", mid["id"])
        for i in range(10):
            _doc(actor, workspace_id, f"Target {i}", leaf["id"])

        with django_assert_num_queries(3):
            hits = services.search(workspace_id, "Target")
        assert len(hits) == 10
        assert all(len(hit[2]) == 3 for hit in hits)


class TestListingFilter:
    def test_q_still_filters_the_documents_listing(self, actor, workspace_id, tree):
        rows = actor.get(
            f"{API}/documents?workspace_id={workspace_id}&q=summary"
        ).json()
        assert [row["title"] for row in rows] == ["Alpha summary"]
