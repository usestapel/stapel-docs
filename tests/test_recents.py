"""Recents — per-user last-reached documents (drive-spec §3.2).

The upsert lives in the SERVICE layer, on the three paths that actually hand
a document to a person: content read, download-URL issuance and an accepted
save. Pinned here: those three write, a rejected save does not, the row is
upserted rather than appended, the per-user cap trims oldest-first on write,
the listing is workspace-scoped and live-only, and no event is emitted (a
bus message per document open would be the noisiest topic in the fleet).
"""
import datetime
import uuid

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_docs import services
from stapel_docs.models import Document, RecentEntry

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


def _doc(actor, workspace_id, title="Notes", body="# hi"):
    resp = actor.post(
        f"{API}/documents",
        {
            "workspace_id": str(workspace_id),
            "type": "md",
            "title": title,
            "body": body,
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content
    return resp.json()


class TestUpsertHooks:
    def test_content_read_records_a_recent(self, actor, user, workspace_id):
        doc = _doc(actor, workspace_id)
        assert RecentEntry.objects.count() == 0
        assert actor.get(f"{API}/documents/{doc['id']}/content").status_code == 200
        row = RecentEntry.objects.get()
        assert str(row.document_id) == doc["id"]
        assert row.user_id == user.pk
        assert str(row.workspace_id) == str(workspace_id)

    def test_repeated_reads_upsert_rather_than_append(self, actor, workspace_id):
        doc = _doc(actor, workspace_id)
        actor.get(f"{API}/documents/{doc['id']}/content")
        first = RecentEntry.objects.get().accessed_at
        actor.get(f"{API}/documents/{doc['id']}/content")
        assert RecentEntry.objects.count() == 1
        assert RecentEntry.objects.get().accessed_at >= first

    def test_accepted_save_records_a_recent(self, actor, workspace_id):
        doc = _doc(actor, workspace_id)
        RecentEntry.objects.all().delete()
        resp = actor.put(
            f"{API}/documents/{doc['id']}/content",
            b"# newer",
            content_type="text/markdown",
            HTTP_IF_MATCH=f'"{doc["head_seq"]}"',
        )
        assert resp.status_code == 200, resp.content
        assert RecentEntry.objects.filter(document_id=doc["id"]).exists()

    def test_a_rejected_save_records_nothing(self, actor, workspace_id):
        """A conflict never happened, so it never made the document recent."""
        doc = _doc(actor, workspace_id)
        RecentEntry.objects.all().delete()
        resp = actor.put(
            f"{API}/documents/{doc['id']}/content",
            b"# loser",
            content_type="text/markdown",
            HTTP_IF_MATCH='"999"',
        )
        assert resp.status_code == 409
        assert RecentEntry.objects.count() == 0

    def test_download_url_issuance_records_a_recent(self, actor, workspace_id):
        """The signed link leaves and this service never sees the read, so
        issuance IS the moment the user got the document."""
        doc = _doc(actor, workspace_id)
        RecentEntry.objects.all().delete()
        with override_settings(STAPEL_DOCS={"ALLOW_UNEXPIRING_DOWNLOAD_URLS": True}):
            resp = actor.get(f"{API}/documents/{doc['id']}/download")
        assert resp.status_code == 200, resp.content
        assert RecentEntry.objects.filter(document_id=doc["id"]).exists()

    def test_a_refused_download_url_records_nothing(self, actor, workspace_id):
        doc = _doc(actor, workspace_id)
        RecentEntry.objects.all().delete()
        # Default config: DjangoStorageBackend cannot sign, so no URL is
        # minted — and nothing was handed over to remember.
        assert actor.get(f"{API}/documents/{doc['id']}/download").status_code == 503
        assert RecentEntry.objects.count() == 0

    def test_machine_reads_leave_no_trace(self, actor, workspace_id):
        """Export renders bytes for a machine, not for a person."""
        doc = _doc(actor, workspace_id)
        RecentEntry.objects.all().delete()
        actor.get(f"{API}/documents/{doc['id']}/export?format=nope")
        assert RecentEntry.objects.count() == 0

    def test_no_user_is_a_no_op(self, workspace_id):
        document = Document.objects.create(
            workspace_id=workspace_id, type="md", title="D"
        )
        services.touch_recent(document, None)
        assert RecentEntry.objects.count() == 0


class TestTrim:
    def test_cap_trims_oldest_first_on_write(self, user, workspace_id):
        docs = [
            Document.objects.create(workspace_id=workspace_id, type="md", title=f"D{i}")
            for i in range(5)
        ]
        # Four rows already reached, oldest first; the fifth arrives now.
        past = timezone.now() - datetime.timedelta(hours=1)
        for offset, document in enumerate(docs[:4]):
            RecentEntry.objects.create(
                user=user,
                document=document,
                workspace_id=workspace_id,
                accessed_at=past + datetime.timedelta(minutes=offset),
            )

        with override_settings(STAPEL_DOCS={"RECENTS_MAX_PER_USER": 3}):
            services.touch_recent(docs[4], user)

        kept = set(
            RecentEntry.objects.filter(user=user).values_list("document_id", flat=True)
        )
        # The newest three survive; the two oldest are gone, not the newest.
        assert kept == {docs[4].id, docs[3].id, docs[2].id}

    def test_cap_zero_disables_the_trim(self, user, workspace_id):
        with override_settings(STAPEL_DOCS={"RECENTS_MAX_PER_USER": 0}):
            for i in range(4):
                services.touch_recent(
                    Document.objects.create(
                        workspace_id=workspace_id, type="md", title=f"D{i}"
                    ),
                    user,
                )
        assert RecentEntry.objects.filter(user=user).count() == 4


class TestListing:
    def test_newest_first_live_only_and_scoped(self, actor, user, workspace_id,
                                               grant_capabilities):
        other_ws = uuid.uuid4()
        grant_capabilities(other_ws, user.pk)
        first = _doc(actor, workspace_id, "First")
        second = _doc(actor, workspace_id, "Second")
        elsewhere = _doc(actor, other_ws, "Elsewhere")
        for doc in (first, second, elsewhere):
            actor.get(f"{API}/documents/{doc['id']}/content")

        body = actor.get(f"{API}/recents?workspace_id={workspace_id}").json()
        assert [d["title"] for d in body] == ["Second", "First"]

        # Trashing takes it out of the listing; the row survives for restore.
        actor.delete(f"{API}/documents/{second['id']}")
        body = actor.get(f"{API}/recents?workspace_id={workspace_id}").json()
        assert [d["title"] for d in body] == ["First"]
        assert RecentEntry.objects.filter(document_id=second["id"]).exists()

    def test_recents_are_per_user(self, actor, api_client, workspace_id,
                                  grant_capabilities):
        from django.contrib.auth import get_user_model

        doc = _doc(actor, workspace_id)
        actor.get(f"{API}/documents/{doc['id']}/content")

        other = get_user_model().objects.create(username=f"o-{uuid.uuid4().hex[:8]}")
        api_client.force_authenticate(user=other)
        grant_capabilities(workspace_id, other.pk)
        assert api_client.get(f"{API}/recents?workspace_id={workspace_id}").json() == []

    def test_denied_without_view(self, actor, user, workspace_id, grant_capabilities):
        grant_capabilities(workspace_id, user.pk, "docs.nothing")
        assert (
            actor.get(f"{API}/recents?workspace_id={workspace_id}").status_code == 403
        )

    def test_workspace_id_is_mandatory(self, actor):
        assert actor.get(f"{API}/recents").status_code == 400
