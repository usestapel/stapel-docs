"""HTTP Range on the authorized content streams (viewing-wave, 0.8.0).

Media viewers seek. On object-store profiles the presigned GET honours
Range at the store; on the DjangoStorage dev profile the ONLY byte path
is the authorized ``/content`` stream, so that stream must answer 206 to
a single byte range — otherwise video seek silently degrades to
re-downloading from zero. The honest limitation stays: the dev backend
materializes the body in worker memory either way (documented, dev-grade).

Pinned here: single-range 206 with Content-Range, open/suffix ranges,
416 past the end, malformed/multi ranges degrade to the full 200, and
the revision + bearer streams speak the same protocol.
"""
import uuid

import pytest
from django.test import override_settings

from stapel_docs.storage import get_storage

pytestmark = pytest.mark.django_db

API = "/docs/api/v1"

BODY = b"0123456789abcdefghij"  # 20 bytes


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
def file_doc(actor, workspace_id):
    resp = actor.post(
        f"{API}/uploads",
        {"workspace_id": str(workspace_id), "title": "clip.mp4", "mime_type": "video/mp4"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    ticket = resp.json()
    get_storage().put_bytes(ticket["key"], BODY, content_type="video/mp4")
    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 200, resp.content
    return resp.json()


class TestContentRange:
    def test_full_reads_advertise_ranges(self, actor, file_doc):
        resp = actor.get(f"{API}/documents/{file_doc['id']}/content")
        assert resp.status_code == 200
        assert resp["Accept-Ranges"] == "bytes"
        assert resp.content == BODY

    def test_a_closed_range_is_206(self, actor, file_doc):
        resp = actor.get(
            f"{API}/documents/{file_doc['id']}/content", HTTP_RANGE="bytes=2-5"
        )
        assert resp.status_code == 206, resp.content
        assert resp.content == BODY[2:6]
        assert resp["Content-Range"] == f"bytes 2-5/{len(BODY)}"
        assert resp["Content-Length"] == "4"

    def test_an_open_range_runs_to_the_end(self, actor, file_doc):
        resp = actor.get(
            f"{API}/documents/{file_doc['id']}/content", HTTP_RANGE="bytes=15-"
        )
        assert resp.status_code == 206
        assert resp.content == BODY[15:]
        assert resp["Content-Range"] == f"bytes 15-19/{len(BODY)}"

    def test_a_suffix_range_serves_the_tail(self, actor, file_doc):
        resp = actor.get(
            f"{API}/documents/{file_doc['id']}/content", HTTP_RANGE="bytes=-4"
        )
        assert resp.status_code == 206
        assert resp.content == BODY[-4:]
        assert resp["Content-Range"] == f"bytes 16-19/{len(BODY)}"

    def test_past_the_end_is_416(self, actor, file_doc):
        resp = actor.get(
            f"{API}/documents/{file_doc['id']}/content", HTTP_RANGE="bytes=99-"
        )
        assert resp.status_code == 416
        assert resp["Content-Range"] == f"bytes */{len(BODY)}"

    def test_an_end_past_the_size_is_clamped(self, actor, file_doc):
        resp = actor.get(
            f"{API}/documents/{file_doc['id']}/content", HTTP_RANGE="bytes=10-999"
        )
        assert resp.status_code == 206
        assert resp.content == BODY[10:]

    def test_malformed_ranges_degrade_to_200(self, actor, file_doc):
        for bad in ("bytes=5-2", "bytes=a-b", "chunks=1-2", "bytes=1-2,4-6"):
            resp = actor.get(
                f"{API}/documents/{file_doc['id']}/content", HTTP_RANGE=bad
            )
            assert resp.status_code == 200, bad
            assert resp.content == BODY


class TestRevisionRange:
    def test_revision_content_speaks_range(self, actor, file_doc):
        resp = actor.get(f"{API}/documents/{file_doc['id']}/revisions")
        assert resp.status_code == 200, resp.content
        revisions = resp.json()
        assert revisions, "the finalized upload should have minted a revision"
        rev_id = revisions[0]["id"]
        resp = actor.get(
            f"{API}/documents/{file_doc['id']}/revisions/{rev_id}/content",
            HTTP_RANGE="bytes=0-3",
        )
        assert resp.status_code == 206
        assert resp.content == BODY[0:4]


class TestBearerRange:
    @pytest.fixture(autouse=True)
    def _link_mode(self):
        with override_settings(STAPEL_DOCS={"SHARING": {"MODES": ["link"]}}):
            yield

    def test_the_shared_stream_speaks_range(self, actor, user, file_doc, api_client, db):
        from datetime import timedelta
        from secrets import token_urlsafe

        from django.contrib.auth import get_user_model
        from django.utils import timezone

        from stapel_docs.models import Document, DocumentLink

        document = Document.objects.get(pk=file_doc["id"])
        link = DocumentLink.objects.create(
            document=document,
            workspace_id=document.workspace_id,
            token=token_urlsafe(32),
            level="view",
            created_by=user,
            expires_at=timezone.now() + timedelta(days=30),
        )
        guest = get_user_model().objects.create(username=f"g-{uuid.uuid4().hex[:8]}")
        api_client.force_authenticate(user=guest)
        resp = api_client.get(
            f"{API}/shared/{link.token}/content", HTTP_RANGE="bytes=2-5"
        )
        assert resp.status_code == 206, resp.content
        assert resp.content == BODY[2:6]
