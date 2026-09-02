"""Uploads: presigned upload sessions for type=file — pending documents
hidden from listings, finalize promotes the blob to seq 1."""
import uuid
from datetime import timedelta

import pytest
from django.test import override_settings

from stapel_docs.models import Document, Revision, UploadSession
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


def _open_upload(actor, workspace_id, **overrides):
    payload = {
        "workspace_id": str(workspace_id),
        "title": "meeting.mp4",
        "mime_type": "video/mp4",
    }
    payload.update(overrides)
    resp = actor.post(f"{API}/uploads", payload, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def test_open_upload_creates_hidden_pending_document(actor, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    assert set(ticket) == {"upload_id", "document_id", "key", "put_url", "expires_at"}
    assert ticket["key"].endswith(f"upload-{ticket['upload_id']}")
    # The ticket advertises its own deadline (UPLOAD_SESSION_TTL_SECONDS).
    assert ticket["expires_at"]

    row = Document.objects.get(pk=ticket["document_id"])
    assert row.type == "file"
    assert row.metadata == {"upload_pending": True}
    assert row.head_seq == 0

    # Pending documents are EXCLUDED from listings.
    listing = actor.get(f"{API}/documents", {"workspace_id": str(workspace_id)})
    assert listing.json() == []
    # But the envelope endpoint sees them (the client polls its own upload).
    assert actor.get(f"{API}/documents/{ticket['document_id']}").status_code == 200


def test_finalize_without_object_is_upload_state_400(actor, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_upload_state"


def test_finalize_promotes_blob_to_head(actor, user, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    # The client-side PUT is simulated straight into the seam — what the
    # S3 profile's presigned PUT (or the module-intake PUT, tested in its
    # own section below) ends up doing.
    get_storage().put_bytes(ticket["key"], b"raw video bytes", content_type="video/mp4")

    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 200, resp.content
    envelope = resp.json()
    assert envelope["head_seq"] == 1
    assert envelope["snapshot_seq"] == 1
    assert envelope["size_bytes"] == len(b"raw video bytes")
    assert envelope["mime_type"] == "video/mp4"
    assert envelope["metadata"] == {}
    assert envelope["editor_hint"] == ""  # file: download-only
    assert envelope["collab"] == "snapshot"

    revision = Revision.objects.get(document_id=ticket["document_id"])
    assert revision.seq == 1
    assert revision.kind == "auto"
    assert revision.storage_key == ticket["key"]

    session = UploadSession.objects.get(pk=ticket["upload_id"])
    assert session.state == "finalized"

    listing = actor.get(f"{API}/documents", {"workspace_id": str(workspace_id)})
    assert [d["id"] for d in listing.json()] == [ticket["document_id"]]

    # Byte-preserved original (verdict §9.4): download-and-compare.
    content = actor.get(f"{API}/documents/{ticket['document_id']}/content")
    assert content.content == b"raw video bytes"
    assert content["Content-Type"].startswith("video/mp4")


def test_finalize_twice_is_upload_state_400(actor, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    get_storage().put_bytes(ticket["key"], b"x")
    assert actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize").status_code == 200
    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_upload_state"


def test_upload_requires_edit(api_client, user, grant_capabilities, workspace_id):
    api_client.force_authenticate(user=user)
    grant_capabilities(workspace_id, user.pk, "docs.view")
    resp = api_client.post(
        f"{API}/uploads",
        {"workspace_id": str(workspace_id), "title": "f.bin"},
        format="json",
    )
    assert resp.status_code == 403


def test_unknown_upload_is_404(actor):
    resp = actor.post(f"{API}/uploads/{uuid.uuid4()}/finalize")
    assert resp.status_code == 404
    assert resp.json()["localizable_error"] == "error.404.docs_upload_not_found"


# ── Upload invariants (audit DOCS-01) ────────────────────────────────
#
# A ticket is a capability, not a slot: it declares the object, it expires,
# it belongs to the user who opened it, and it is spendable exactly once.


def test_declared_size_must_match_the_stored_object(actor, workspace_id):
    ticket = _open_upload(actor, workspace_id, size_bytes=100)
    get_storage().put_bytes(ticket["key"], b"only nine")

    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_upload_mismatch"
    # Nothing was promoted: the pending document keeps head_seq 0.
    assert Document.objects.get(pk=ticket["document_id"]).head_seq == 0


def test_declared_checksum_binds_the_bytes(actor, workspace_id):
    import hashlib

    blob = b"raw video bytes"
    digest = hashlib.sha256(blob).hexdigest()
    ticket = _open_upload(actor, workspace_id, checksum=digest)
    get_storage().put_bytes(ticket["key"], b"different bytes!")

    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_upload_mismatch"

    # The declared object finalizes.
    get_storage().put_bytes(ticket["key"], blob)
    assert actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize").status_code == 200


def test_expired_session_cannot_be_finalized(actor, workspace_id):
    from django.utils import timezone

    ticket = _open_upload(actor, workspace_id)
    session = UploadSession.objects.get(pk=ticket["upload_id"])
    session.expires_at = timezone.now() - timedelta(seconds=1)
    session.save(update_fields=["expires_at"])
    get_storage().put_bytes(ticket["key"], b"late")

    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_upload_expired"


def test_ticket_ttl_comes_from_settings(actor, workspace_id):
    with override_settings(STAPEL_DOCS={"UPLOAD_SESSION_TTL_SECONDS": 60}):
        ticket = _open_upload(actor, workspace_id)
    session = UploadSession.objects.get(pk=ticket["upload_id"])
    assert session.expires_at is not None
    assert (session.expires_at - session.created_at).total_seconds() <= 61


def test_another_member_cannot_spend_someone_elses_ticket(
    api_client, actor, workspace_id, grant_capabilities
):
    """A leaked upload_id must not let a second member plant a document."""
    from django.contrib.auth import get_user_model

    ticket = _open_upload(actor, workspace_id)
    get_storage().put_bytes(ticket["key"], b"blob")

    other = get_user_model().objects.create(username=f"o-{uuid.uuid4().hex[:8]}")
    grant_capabilities(workspace_id, other.pk, "docs.view", "docs.edit")
    api_client.force_authenticate(user=other)
    resp = api_client.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 403
    assert resp.json()["localizable_error"] == "error.403.docs_upload_owner"

    # A workspace manager may (recovery path for an absent uploader).
    from django.core.cache import cache

    grant_capabilities(workspace_id, other.pk, "docs.view", "docs.edit", "docs.manage")
    cache.clear()  # capability verdicts are cached 30 s; re-ask with the new grant
    assert api_client.post(f"{API}/uploads/{ticket['upload_id']}/finalize").status_code == 200


def test_an_ownerless_ticket_is_not_spendable_by_default(
    api_client, actor, user, workspace_id, grant_capabilities
):
    """GDPR anonymize nulls created_by. A ticket nobody owns satisfies
    nobody's owner binding — it needs the same `manage` escalation as
    somebody else's ticket, instead of becoming every editor's to spend."""
    from django.core.cache import cache

    ticket = _open_upload(actor, workspace_id)
    get_storage().put_bytes(ticket["key"], b"blob")
    UploadSession.objects.filter(pk=ticket["upload_id"]).update(created_by=None)

    # Not even the user who opened it: the binding is gone, not satisfied,
    # so an editor without `manage` no longer gets through on it.
    grant_capabilities(workspace_id, user.pk, "docs.view", "docs.edit")
    cache.clear()
    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 403
    assert resp.json()["localizable_error"] == "error.403.docs_upload_owner"

    from django.contrib.auth import get_user_model

    other = get_user_model().objects.create(username=f"o-{uuid.uuid4().hex[:8]}")
    grant_capabilities(workspace_id, other.pk, "docs.view", "docs.edit")
    api_client.force_authenticate(user=other)
    assert api_client.post(
        f"{API}/uploads/{ticket['upload_id']}/finalize"
    ).status_code == 403

    # A workspace manager still has the recovery path.
    grant_capabilities(workspace_id, other.pk, "docs.view", "docs.edit", "docs.manage")
    cache.clear()  # capability verdicts are cached 30 s; re-ask with the new grant
    assert api_client.post(
        f"{API}/uploads/{ticket['upload_id']}/finalize"
    ).status_code == 200


def test_finalize_consumes_the_session_atomically(actor, workspace_id):
    """The state transition is a conditional UPDATE, so a caller holding a
    stale `pending` row cannot promote the blob a second time."""
    ticket = _open_upload(actor, workspace_id)
    get_storage().put_bytes(ticket["key"], b"blob")
    stale = UploadSession.objects.get(pk=ticket["upload_id"])  # captured pending

    assert actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize").status_code == 200

    from stapel_docs.services import DocsError, finalize_upload

    with pytest.raises(DocsError) as exc:
        finalize_upload(stale)
    assert exc.value.error_key == "error.400.docs_upload_state"
    assert Revision.objects.filter(document_id=ticket["document_id"]).count() == 1


def test_an_unmeasurable_object_fails_the_finalize(actor, workspace_id, monkeypatch):
    """A store that cannot report a size must not hand the decision back to
    the client's declaration: the ceiling and the quota are enforced on a
    measurement or not at all."""
    ticket = _open_upload(actor, workspace_id, size_bytes=0)
    get_storage().put_bytes(ticket["key"], b"x" * 4096)
    monkeypatch.setattr(
        type(get_storage()), "head_object", lambda self, key: (True, None)
    )

    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_upload_unmeasurable"
    # Nothing was promoted and nothing was charged.
    row = Document.objects.get(pk=ticket["document_id"])
    assert row.head_seq == 0
    assert row.size_bytes == 0
    assert UploadSession.objects.get(pk=ticket["upload_id"]).state == "pending"


def test_head_object_does_not_swallow_a_size_failure(tmp_path):
    """The seam's own contract: `(exists, None)` is not a sink for storage
    errors — the caller must see the failure."""
    from django.core.files.storage import FileSystemStorage

    from stapel_docs.storage import DjangoStorageBackend

    backend = DjangoStorageBackend()
    backend.put_bytes("probe.bin", b"measured")
    assert backend.head_object("probe.bin") == (True, len(b"measured"))

    def _boom(self, name):
        raise OSError("the object store lost the file's metadata")

    original = FileSystemStorage.size
    FileSystemStorage.size = _boom
    try:
        with pytest.raises(OSError):
            backend.head_object("probe.bin")
    finally:
        FileSystemStorage.size = original


def test_declared_size_over_the_ceiling_is_refused(actor, workspace_id):
    with override_settings(STAPEL_DOCS={"MAX_UPLOAD_BYTES": 1024}):
        resp = actor.post(
            f"{API}/uploads",
            {"workspace_id": str(workspace_id), "title": "big.bin", "size_bytes": 2048},
            format="json",
        )
    assert resp.status_code == 413
    assert resp.json()["localizable_error"] == "error.413.docs_upload_too_large"


def test_stored_object_over_the_ceiling_is_refused(actor, workspace_id):
    """The declaration is not trusted: the STORED object is measured."""
    ticket = _open_upload(actor, workspace_id)
    get_storage().put_bytes(ticket["key"], b"x" * 64)
    with override_settings(STAPEL_DOCS={"MAX_UPLOAD_BYTES": 8}):
        resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 413
    assert resp.json()["localizable_error"] == "error.413.docs_upload_too_large"


def test_mime_allowlist_is_enforced(actor, workspace_id):
    with override_settings(STAPEL_DOCS={"UPLOAD_ALLOWED_MIME_TYPES": ["image/*"]}):
        resp = actor.post(
            f"{API}/uploads",
            {
                "workspace_id": str(workspace_id),
                "title": "payload.exe",
                "mime_type": "application/x-msdownload",
            },
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_upload_mime"
        allowed = actor.post(
            f"{API}/uploads",
            {
                "workspace_id": str(workspace_id),
                "title": "photo.png",
                "mime_type": "image/png",
            },
            format="json",
        )
        assert allowed.status_code == 201


def test_the_shipped_allowlist_refuses_active_content(actor, workspace_id):
    """No override: the DEFAULT config must already refuse the types a host
    serving its media inline would execute."""
    for mime in (
        "text/html",
        "image/svg+xml",
        "application/javascript",
        "application/x-msdownload",
        "application/x-sh",
    ):
        resp = actor.post(
            f"{API}/uploads",
            {"workspace_id": str(workspace_id), "title": "payload", "mime_type": mime},
            format="json",
        )
        assert resp.status_code == 400, f"{mime} was accepted by the default config"
        assert resp.json()["localizable_error"] == "error.400.docs_upload_mime"


def test_an_undeclared_content_type_is_not_a_blank_cheque(actor, workspace_id):
    """Omitting mime_type must not be the way around the allowlist."""
    resp = actor.post(
        f"{API}/uploads",
        {"workspace_id": str(workspace_id), "title": "mystery.bin"},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_upload_mime"


def test_the_shipped_allowlist_accepts_documents(actor, workspace_id):
    for mime in ("application/pdf", "text/plain", "image/png", "video/mp4"):
        resp = actor.post(
            f"{API}/uploads",
            {"workspace_id": str(workspace_id), "title": "doc", "mime_type": mime},
            format="json",
        )
        assert resp.status_code == 201, f"{mime} was refused by the default config"


def test_accepting_anything_is_an_explicit_setting(actor, workspace_id):
    """"No restriction" is spelled out, never inferred from an empty list."""
    with override_settings(STAPEL_DOCS={"UPLOAD_ALLOWED_MIME_TYPES": ["*/*"]}):
        resp = actor.post(
            f"{API}/uploads",
            {
                "workspace_id": str(workspace_id),
                "title": "payload.exe",
                "mime_type": "application/x-msdownload",
            },
            format="json",
        )
        assert resp.status_code == 201


def test_an_empty_allowlist_allows_nothing(actor, workspace_id):
    with override_settings(STAPEL_DOCS={"UPLOAD_ALLOWED_MIME_TYPES": []}):
        resp = actor.post(
            f"{API}/uploads",
            {
                "workspace_id": str(workspace_id),
                "title": "photo.png",
                "mime_type": "image/png",
            },
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_upload_mime"


def test_open_sessions_per_workspace_are_capped(actor, workspace_id):
    with override_settings(STAPEL_DOCS={"MAX_PENDING_UPLOADS_PER_WORKSPACE": 2}):
        _open_upload(actor, workspace_id)
        _open_upload(actor, workspace_id)
        resp = actor.post(
            f"{API}/uploads",
            {
                "workspace_id": str(workspace_id),
                "title": "third.pdf",
                "mime_type": "application/pdf",
            },
            format="json",
        )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_too_many_uploads"


def test_upload_respects_the_workspace_quota(actor, workspace_id):
    with override_settings(STAPEL_DOCS={"WORKSPACE_QUOTA_BYTES": 100}):
        resp = actor.post(
            f"{API}/uploads",
            {
                "workspace_id": str(workspace_id),
                "title": "big.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 200,
            },
            format="json",
        )
    assert resp.status_code == 507
    assert resp.json()["localizable_error"] == "error.507.docs_workspace_quota"


# ── Module intake PUT (0.7.1) — the browser path without S3 ──────────
#
# DjangoStorageBackend cannot mint a URL a client can PUT to:
# ``storage.url`` is a GET-only served path, so 0.7.0 tickets sent the
# browser's bytes at a wall (403, nothing stored, finalize unreachable).
# A backend that declares ``accepts_direct_put = False`` now gets the
# module's OWN signed intake URL minted instead —
# ``PUT /uploads/<id>/content?signature=…`` — where the signature is the
# whole credential, exactly like an S3 presigned URL: no auth header, no
# cookie, bounded by the same session TTL.


def _anonymous():
    from rest_framework.test import APIClient

    return APIClient()


def test_django_backend_put_url_is_the_signed_module_intake(actor, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    assert ticket["put_url"].startswith(
        f"{API}/uploads/{ticket['upload_id']}/content?signature="
    )


def test_browser_upload_loop_works_end_to_end_without_s3(actor, workspace_id):
    """The live drive-tab repro, fixed: open → PUT the bytes to the minted
    URL with NO credentials attached → finalize → content GET serves them."""
    ticket = _open_upload(actor, workspace_id)
    blob = b"raw video bytes"

    resp = _anonymous().put(ticket["put_url"], data=blob, content_type="video/mp4")
    assert resp.status_code == 204, resp.content

    finalize = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert finalize.status_code == 200, finalize.content
    content = actor.get(f"{API}/documents/{ticket['document_id']}/content")
    assert content.content == blob
    assert content["Content-Type"].startswith("video/mp4")


def test_intake_put_is_immune_to_csrf_enforcement(actor, workspace_id):
    """DRF enforces CSRF only inside ``SessionAuthentication.enforce_csrf``.
    The intake view authenticates nobody — the signature is the whole
    credential — so a cookie-mode browser (the live repro host) can never
    be refused for a missing CSRF token. Asserted structurally (no
    authenticator ever runs enforce_csrf) and behaviourally (a
    CSRF-enforcing client gets through)."""
    from rest_framework.test import APIClient

    from stapel_docs.views import UploadContentPutView

    assert UploadContentPutView.authentication_classes == []

    ticket = _open_upload(actor, workspace_id)
    strict = APIClient(enforce_csrf_checks=True)
    resp = strict.put(ticket["put_url"], data=b"bytes", content_type="video/mp4")
    assert resp.status_code == 204, resp.content


def test_tampered_intake_signature_is_403(actor, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    resp = _anonymous().put(
        ticket["put_url"] + "tampered", data=b"x", content_type="video/mp4"
    )
    assert resp.status_code == 403
    # And no signature at all is the same refusal.
    bare = ticket["put_url"].split("?")[0]
    assert _anonymous().put(
        bare, data=b"x", content_type="video/mp4"
    ).status_code == 403


def test_intake_signature_binds_its_own_ticket(actor, workspace_id):
    """A signature minted for ticket A opens nothing else."""
    a = _open_upload(actor, workspace_id)
    b = _open_upload(actor, workspace_id, title="other.mp4")
    stolen = a["put_url"].split("signature=", 1)[1]
    resp = _anonymous().put(
        f"{API}/uploads/{b['upload_id']}/content?signature={stolen}",
        data=b"x",
        content_type="video/mp4",
    )
    assert resp.status_code == 403


def test_signed_put_to_an_unknown_session_is_404(actor):
    from urllib.parse import quote

    from stapel_docs.services import upload_intake_signer

    ghost = uuid.uuid4()
    signature = quote(upload_intake_signer().sign(str(ghost)))
    resp = _anonymous().put(
        f"{API}/uploads/{ghost}/content?signature={signature}",
        data=b"x",
        content_type="video/mp4",
    )
    assert resp.status_code == 404
    assert resp.json()["localizable_error"] == "error.404.docs_upload_not_found"


def test_intake_put_to_an_expired_session_is_400(actor, workspace_id):
    from django.utils import timezone

    ticket = _open_upload(actor, workspace_id)
    UploadSession.objects.filter(pk=ticket["upload_id"]).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    resp = _anonymous().put(ticket["put_url"], data=b"late", content_type="video/mp4")
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_upload_expired"


def test_intake_put_to_a_finalized_session_is_400(actor, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    assert _anonymous().put(
        ticket["put_url"], data=b"blob", content_type="video/mp4"
    ).status_code == 204
    assert actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize").status_code == 200
    # The ticket is spent: a replayed PUT must not overwrite the promoted blob.
    resp = _anonymous().put(
        ticket["put_url"], data=b"overwrite", content_type="video/mp4"
    )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_upload_state"


def test_intake_put_over_the_ceiling_is_413(actor, workspace_id):
    ticket = _open_upload(actor, workspace_id)
    with override_settings(STAPEL_DOCS={"MAX_UPLOAD_BYTES": 8}):
        resp = _anonymous().put(
            ticket["put_url"], data=b"x" * 64, content_type="video/mp4"
        )
    assert resp.status_code == 413
    assert resp.json()["localizable_error"] == "error.413.docs_upload_too_large"


def test_direct_put_backend_still_gets_the_storage_minted_url(
    actor, workspace_id, monkeypatch
):
    """The S3 path is untouched: a backend that accepts a direct PUT is
    ASKED for the presigned URL and its answer is the ticket's put_url."""
    from stapel_docs import storage as storage_module

    backend = storage_module.get_storage()
    seen = {}

    def _fake_presign(self, key, *, expires_seconds=900, content_type=None):
        seen["key"] = key
        seen["content_type"] = content_type
        return "https://minio.example/bucket/presigned-put"

    monkeypatch.setattr(type(backend), "accepts_direct_put", True)
    monkeypatch.setattr(type(backend), "presigned_put_url", _fake_presign)

    ticket = _open_upload(actor, workspace_id)
    assert ticket["put_url"] == "https://minio.example/bucket/presigned-put"
    assert seen["key"] == ticket["key"]
    assert seen["content_type"] == "video/mp4"
