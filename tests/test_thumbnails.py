"""Image thumbnails — an authorized docs endpoint, never a second read path
(drive-spec §3.6).

The invariant this file exists to hold is I2, storage closure: a thumbnail
is derived from a document's bytes, so it lives under that document's own
prefix and it dies when the document is purged. ``purge_document`` deletes
ENUMERATED keys rather than a prefix, so an unregistered derived object
would quietly outlive its subject — the purge test below is what keeps the
registration honest.

Also pinned: the tier ladder is closed, a non-image and a bodyless document
are 400 (not 500), a missing Pillow is 503 the way a missing exporter
dependency is 503, saved content re-renders instead of serving the previous
picture, and every byte goes out through authorize() + the storage seam.
"""
import io
import uuid

import pytest
from django.test import override_settings

from stapel_docs.models import Thumbnail
from stapel_docs.storage import get_storage

pytestmark = pytest.mark.django_db

API = "/docs/api/v1"

Image = pytest.importorskip("PIL.Image", reason="thumbnails need the [thumbnails] extra")


def _png(width=800, height=400, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _png_with_alpha(width=200, height=100) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (width, height), (10, 200, 10, 0)).save(buf, format="PNG")
    return buf.getvalue()


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


def _upload(actor, workspace_id, payload: bytes, mime="image/png", title="shot.png"):
    """A finalized type=file document carrying *payload* as its blob."""
    resp = actor.post(
        f"{API}/uploads",
        {"workspace_id": str(workspace_id), "title": title, "mime_type": mime},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    ticket = resp.json()
    # DjangoStorageBackend's presigned PUT degrades to a served URL, so the
    # client-side PUT is simulated straight into the seam (test_uploads.py).
    get_storage().put_bytes(ticket["key"], payload, content_type=mime)
    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 200, resp.content
    return resp.json()


class TestHappyPath:
    def test_renders_and_serves_a_real_image(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _png(800, 400))
        resp = actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160")

        assert resp.status_code == 200, resp.content
        assert resp["Content-Type"] == "image/jpeg"
        with Image.open(io.BytesIO(resp.content)) as rendered:
            assert rendered.format == "JPEG"
            # Aspect preserved, longest edge capped at the tier.
            assert max(rendered.size) == 160
            assert rendered.size == (160, 80)

    def test_both_tiers_are_cached_separately(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _png(800, 400))
        for tier in (160, 480):
            assert (
                actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier={tier}").status_code
                == 200
            )
        rows = Thumbnail.objects.filter(document_id=doc["id"])
        assert sorted(rows.values_list("tier", flat=True)) == [160, 480]
        assert len(set(rows.values_list("storage_key", flat=True))) == 2

    def test_second_request_serves_the_cache(self, actor, workspace_id, monkeypatch):
        doc = _upload(actor, workspace_id, _png())
        assert (
            actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160").status_code == 200
        )

        from stapel_docs import thumbnails

        def _explode(*args, **kwargs):  # pragma: no cover — must not be called
            raise AssertionError("a cached thumbnail must not re-render")

        monkeypatch.setattr(thumbnails, "render", _explode)
        resp = actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160")
        assert resp.status_code == 200
        assert Thumbnail.objects.filter(document_id=doc["id"]).count() == 1

    def test_alpha_is_composited_not_lost(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _png_with_alpha())
        resp = actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160")
        assert resp.status_code == 200
        with Image.open(io.BytesIO(resp.content)) as rendered:
            assert rendered.mode == "RGB"

    def test_never_upscales(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _png(64, 64))
        resp = actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=480")
        with Image.open(io.BytesIO(resp.content)) as rendered:
            assert rendered.size == (64, 64)

    def test_bytes_come_out_of_the_storage_seam(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _png())
        actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160")
        row = Thumbnail.objects.get(document_id=doc["id"])
        exists, size = get_storage().head_object(row.storage_key)
        assert exists and size == row.size_bytes
        # I2: under the document's own prefix, nowhere else.
        assert row.storage_key.endswith(".jpg")
        assert f"/{doc['id']}/" in row.storage_key


class TestStaleness:
    def test_saved_content_re_renders(
        self, actor, workspace_id, django_capture_on_commit_callbacks
    ):
        doc = _upload(actor, workspace_id, _png(800, 400, (255, 0, 0)))
        first = actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160").content
        old_key = Thumbnail.objects.get(document_id=doc["id"]).storage_key

        # A new revision of the blob: the key carries head_seq, so the stale
        # image is not merely unpreferred — it is unaddressable.
        from stapel_docs import services

        services.save_content(
            doc["id"], _png(800, 400, (0, 0, 255)), require_mutable_type=False
        )
        with django_capture_on_commit_callbacks(execute=True):
            second = actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160")

        assert second.status_code == 200
        assert second.content != first
        row = Thumbnail.objects.get(document_id=doc["id"])
        assert row.storage_key != old_key
        # The superseded object is swept, not left in the bucket.
        assert get_storage().head_object(old_key)[0] is False


class TestPurgeClosure:
    def test_purging_a_document_removes_its_thumbnails(
        self, actor, workspace_id, django_capture_on_commit_callbacks
    ):
        """I2 has no exception for pictures OF the content."""
        doc = _upload(actor, workspace_id, _png())
        for tier in (160, 480):
            actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier={tier}")
        keys = list(
            Thumbnail.objects.filter(document_id=doc["id"]).values_list(
                "storage_key", flat=True
            )
        )
        assert len(keys) == 2
        assert all(get_storage().head_object(key)[0] for key in keys)

        actor.delete(f"{API}/documents/{doc['id']}")
        with django_capture_on_commit_callbacks(execute=True):
            resp = actor.post(
                f"{API}/trash/empty", {"workspace_id": str(workspace_id)}, format="json"
            )
        assert resp.status_code == 200, resp.content

        assert Thumbnail.objects.count() == 0
        for key in keys:
            assert get_storage().head_object(key)[0] is False, key


class TestRefusals:
    def test_unknown_tier_is_400(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _png())
        resp = actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=99")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_thumbnail_tier"

    def test_missing_or_unparsable_tier_is_400(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _png())
        for query in ("", "?tier=", "?tier=big"):
            resp = actor.get(f"{API}/documents/{doc['id']}/thumbnail{query}")
            assert resp.status_code == 400, query
            assert resp.json()["localizable_error"] == "error.400.docs_thumbnail_tier"

    def test_non_image_file_is_400(self, actor, workspace_id):
        doc = _upload(
            actor, workspace_id, b"%PDF-1.4", mime="application/pdf", title="a.pdf"
        )
        resp = actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_thumbnail_unsupported"

    def test_non_file_document_is_400(self, actor, workspace_id):
        resp = actor.post(
            f"{API}/documents",
            {
                "workspace_id": str(workspace_id),
                "type": "md",
                "title": "Notes",
                "body": "# hi",
            },
            format="json",
        )
        doc = resp.json()
        resp = actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_thumbnail_unsupported"

    def test_pending_upload_has_no_bytes_to_render(self, actor, workspace_id):
        resp = actor.post(
            f"{API}/uploads",
            {
                "workspace_id": str(workspace_id),
                "title": "shot.png",
                "mime_type": "image/png",
            },
            format="json",
        )
        ticket = resp.json()
        resp = actor.get(f"{API}/documents/{ticket['document_id']}/thumbnail?tier=160")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_thumbnail_unsupported"

    def test_corrupt_image_is_the_callers_400_not_a_500(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, b"not an image at all")
        resp = actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_thumbnail_unsupported"

    def test_denied_without_view(self, actor, user, workspace_id, grant_capabilities):
        doc = _upload(actor, workspace_id, _png())
        grant_capabilities(workspace_id, user.pk, "docs.nothing")
        assert (
            actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160").status_code
            == 403
        )

    def test_unknown_document_is_404(self, actor):
        assert (
            actor.get(f"{API}/documents/{uuid.uuid4()}/thumbnail?tier=160").status_code
            == 404
        )


class TestMissingPillow:
    """The 503 convention exporters already use: a frontend falls back to a
    type icon on a 503, but cannot tell a silent empty answer from a broken
    deploy (test_export.py simulates the same way — by making the optional
    import fail)."""

    def test_absent_renderer_is_503(self, actor, workspace_id, monkeypatch):
        doc = _upload(actor, workspace_id, _png())

        import builtins

        real_import = builtins.__import__

        def _no_pillow(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("No module named 'PIL'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_pillow)
        resp = actor.get(f"{API}/documents/{doc['id']}/thumbnail?tier=160")

        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == "error.503.docs_thumbnails_unavailable"
        # Nothing half-written: a 503 leaves no row claiming a cached image.
        assert Thumbnail.objects.count() == 0

    def test_available_reports_the_installation(self, monkeypatch):
        from stapel_docs import thumbnails

        assert thumbnails.available() is True

        import builtins

        real_import = builtins.__import__

        def _no_pillow(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("No module named 'PIL'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_pillow)
        assert thumbnails.available() is False
