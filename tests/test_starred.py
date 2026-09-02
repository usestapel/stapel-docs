"""Starred — per-user bookmarks on documents and folders (drive-spec §3.1).

Pinned here: the exactly-one-target constraint is SQL and not a convention,
star/unstar are idempotent in both directions, the listing is workspace
scoped and live-only (a trashed item leaves the listing and keeps its star),
``is_starred`` rides every folder/document envelope, and the whole surface
goes through ``authorize(docs.view)`` — a star is a bookmark, not an edit,
and not a grant either.
"""
import uuid

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from stapel_docs.models import Document, Folder, Star

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
def other_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username=f"o-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def workspace_id():
    return uuid.uuid4()


@pytest.fixture
def actor(api_client, user, grant_capabilities, workspace_id):
    api_client.force_authenticate(user=user)
    grant_capabilities(workspace_id, user.pk)
    return api_client


def _doc(actor, workspace_id, title="Notes"):
    resp = actor.post(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "type": "md", "title": title},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    return resp.json()


def _folder(actor, workspace_id, name="Meetings"):
    resp = actor.post(
        f"{API}/folders",
        {"workspace_id": str(workspace_id), "name": name},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    return resp.json()


class TestModelConstraints:
    """The target rule lives in SQL: a row with both targets (or neither)
    has no meaning any reader could render, so no code path may create one."""

    def test_exactly_one_target_is_enforced(self, user, workspace_id):
        folder = Folder.objects.create(workspace_id=workspace_id, name="F")
        document = Document.objects.create(
            workspace_id=workspace_id, type="md", title="D"
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Star.objects.create(user=user, workspace_id=workspace_id)
        with pytest.raises(IntegrityError), transaction.atomic():
            Star.objects.create(
                user=user,
                workspace_id=workspace_id,
                folder=folder,
                document=document,
            )

    def test_one_star_per_user_and_target(self, user, workspace_id):
        document = Document.objects.create(
            workspace_id=workspace_id, type="md", title="D"
        )
        Star.objects.create(user=user, workspace_id=workspace_id, document=document)
        with pytest.raises(IntegrityError), transaction.atomic():
            Star.objects.create(user=user, workspace_id=workspace_id, document=document)

    def test_a_null_target_does_not_collide_with_another(self, user, workspace_id):
        """Two folder stars must not trip the (user, document) unique — SQL
        NULL never equals NULL, and this test is what keeps that true if the
        constraints are ever rewritten as partial indexes."""
        a = Folder.objects.create(workspace_id=workspace_id, name="A")
        b = Folder.objects.create(workspace_id=workspace_id, name="B")
        Star.objects.create(user=user, workspace_id=workspace_id, folder=a)
        Star.objects.create(user=user, workspace_id=workspace_id, folder=b)
        assert Star.objects.filter(user=user).count() == 2

    def test_purging_the_target_takes_the_star(self, user, workspace_id):
        document = Document.objects.create(
            workspace_id=workspace_id, type="md", title="D"
        )
        Star.objects.create(user=user, workspace_id=workspace_id, document=document)
        document.delete()
        assert Star.objects.count() == 0


class TestEndpoints:
    def test_star_and_unstar_are_idempotent(self, actor, workspace_id):
        doc = _doc(actor, workspace_id)
        url = f"{API}/documents/{doc['id']}/star"

        assert actor.post(url).status_code == 204
        assert actor.post(url).status_code == 204
        assert Star.objects.filter(document_id=doc["id"]).count() == 1

        assert actor.delete(url).status_code == 204
        # Unstarring something that is not starred is a success: the caller
        # asked for a state, and the state holds.
        assert actor.delete(url).status_code == 204
        assert Star.objects.filter(document_id=doc["id"]).count() == 0

    def test_folder_star_round_trip(self, actor, workspace_id):
        folder = _folder(actor, workspace_id)
        url = f"{API}/folders/{folder['id']}/star"
        assert actor.post(url).status_code == 204
        assert Star.objects.filter(folder_id=folder["id"]).count() == 1
        assert actor.delete(url).status_code == 204
        assert Star.objects.filter(folder_id=folder["id"]).count() == 0

    def test_listing_carries_folders_and_documents(self, actor, workspace_id):
        doc = _doc(actor, workspace_id, "Starred doc")
        _doc(actor, workspace_id, "Plain doc")
        folder = _folder(actor, workspace_id, "Starred folder")
        _folder(actor, workspace_id, "Plain folder")
        actor.post(f"{API}/documents/{doc['id']}/star")
        actor.post(f"{API}/folders/{folder['id']}/star")

        body = actor.get(f"{API}/starred?workspace_id={workspace_id}").json()
        assert [d["title"] for d in body["documents"]] == ["Starred doc"]
        assert [f["name"] for f in body["folders"]] == ["Starred folder"]
        assert body["documents"][0]["is_starred"] is True

    def test_trashed_rows_leave_the_listing_and_keep_the_star(
        self, actor, workspace_id
    ):
        doc = _doc(actor, workspace_id)
        actor.post(f"{API}/documents/{doc['id']}/star")
        actor.delete(f"{API}/documents/{doc['id']}")

        body = actor.get(f"{API}/starred?workspace_id={workspace_id}").json()
        assert body["documents"] == []
        assert Star.objects.filter(document_id=doc["id"]).count() == 1

        actor.post(f"{API}/documents/{doc['id']}/restore")
        body = actor.get(f"{API}/starred?workspace_id={workspace_id}").json()
        assert [d["id"] for d in body["documents"]] == [doc["id"]]

    def test_stars_are_per_user(self, actor, api_client, workspace_id, other_user,
                                grant_capabilities):
        doc = _doc(actor, workspace_id)
        actor.post(f"{API}/documents/{doc['id']}/star")

        api_client.force_authenticate(user=other_user)
        grant_capabilities(workspace_id, other_user.pk)
        body = api_client.get(f"{API}/starred?workspace_id={workspace_id}").json()
        assert body["documents"] == []
        detail = api_client.get(f"{API}/documents/{doc['id']}").json()
        assert detail["is_starred"] is False

    def test_workspace_isolation(self, actor, workspace_id, grant_capabilities, user):
        other_ws = uuid.uuid4()
        grant_capabilities(other_ws, user.pk)
        mine = _doc(actor, workspace_id, "Mine")
        theirs = _doc(actor, other_ws, "Theirs")
        actor.post(f"{API}/documents/{mine['id']}/star")
        actor.post(f"{API}/documents/{theirs['id']}/star")

        body = actor.get(f"{API}/starred?workspace_id={workspace_id}").json()
        assert [d["title"] for d in body["documents"]] == ["Mine"]


class TestIsStarredEnvelope:
    def test_document_and_folder_envelopes_carry_the_flag(self, actor, workspace_id):
        doc = _doc(actor, workspace_id)
        folder = _folder(actor, workspace_id)
        assert actor.get(f"{API}/documents/{doc['id']}").json()["is_starred"] is False
        assert actor.get(f"{API}/folders/{folder['id']}").json()["is_starred"] is False

        actor.post(f"{API}/documents/{doc['id']}/star")
        actor.post(f"{API}/folders/{folder['id']}/star")
        assert actor.get(f"{API}/documents/{doc['id']}").json()["is_starred"] is True
        assert actor.get(f"{API}/folders/{folder['id']}").json()["is_starred"] is True

        listed = actor.get(f"{API}/documents?workspace_id={workspace_id}").json()
        assert listed[0]["is_starred"] is True
        listed = actor.get(f"{API}/folders?workspace_id={workspace_id}").json()
        assert listed[0]["is_starred"] is True

    def test_no_user_annotates_null_not_false(self, workspace_id):
        """listings canon: "not applicable" is a third answer. A principal
        with no user id never starred anything AND never could have, and
        answering False tells it something untrue about itself."""
        from stapel_docs import services

        document = Document.objects.create(
            workspace_id=workspace_id, type="md", title="D"
        )
        rows = services.list_documents(workspace_id, user=None)
        assert rows[0].is_starred is None
        assert services.attach_star(document, None, target="document").is_starred is None

        presented = services.list_documents(workspace_id, user=None)
        from stapel_docs.presenters import get_document_presenter

        dto = get_document_presenter().present(presented[0])
        assert dto.is_starred is None


class TestAuthorization:
    """Fail-closed: no grant, no star — and an unavailable workspaces
    service is a 503, never a 403 (an outage is not a verdict)."""

    def test_star_denied_without_view(self, api_client, user, workspace_id,
                                      grant_capabilities):
        grant_capabilities(workspace_id, user.pk)
        api_client.force_authenticate(user=user)
        doc = _doc(api_client, workspace_id)

        grant_capabilities(workspace_id, user.pk, "docs.nothing")
        assert api_client.post(f"{API}/documents/{doc['id']}/star").status_code == 403
        assert api_client.delete(f"{API}/documents/{doc['id']}/star").status_code == 403
        assert (
            api_client.get(f"{API}/starred?workspace_id={workspace_id}").status_code
            == 403
        )

    def test_view_is_enough_to_star(self, api_client, user, workspace_id,
                                    grant_capabilities):
        """A star is a bookmark, not an edit: requiring docs.edit would make
        "keep this handy" an act of authorship."""
        grant_capabilities(workspace_id, user.pk)
        api_client.force_authenticate(user=user)
        doc = _doc(api_client, workspace_id)

        grant_capabilities(workspace_id, user.pk, "docs.view")
        assert api_client.post(f"{API}/documents/{doc['id']}/star").status_code == 204

    def test_unknown_target_is_404(self, actor):
        assert actor.post(f"{API}/documents/{uuid.uuid4()}/star").status_code == 404
        assert actor.post(f"{API}/folders/{uuid.uuid4()}/star").status_code == 404
