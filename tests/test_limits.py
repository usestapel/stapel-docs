"""Resource invariants (audit DOCS-01): every accepted byte is bounded.

Body, journal update, export input and workspace budget each have a hard
ceiling from the ``STAPEL_DOCS`` namespace, and body writes are refused for
types that own their own write path (``file`` bodies come from an upload
session, a vanished type is read-only).
"""
import uuid

import pytest
from django.test import override_settings

from stapel_docs.doc_types import DocTypeSpec, register_doc_type, unregister_doc_type

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
    payload = {"workspace_id": str(workspace_id), "type": "md", "title": "Notes"}
    payload.update(overrides)
    resp = actor.post(f"{API}/documents", payload, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def _put(actor, doc, body: bytes, seq: int):
    return actor.put(
        f"{API}/documents/{doc['id']}/content",
        data=body,
        content_type="text/markdown",
        HTTP_IF_MATCH=str(seq),
    )


# ── Body ceiling ─────────────────────────────────────────────────────


def test_oversized_body_is_refused(actor, workspace_id):
    doc = _create_doc(actor, workspace_id)
    with override_settings(STAPEL_DOCS={"MAX_BODY_BYTES": 8}):
        resp = _put(actor, doc, b"x" * 9, 0)
    assert resp.status_code == 413
    assert resp.json()["localizable_error"] == "error.413.docs_body_too_large"
    # The refused save left the head untouched.
    assert actor.get(f"{API}/documents/{doc['id']}").json()["head_seq"] == 0


def test_oversized_create_body_is_refused(actor, workspace_id):
    with override_settings(STAPEL_DOCS={"MAX_BODY_BYTES": 4}):
        resp = actor.post(
            f"{API}/documents",
            {
                "workspace_id": str(workspace_id),
                "type": "md",
                "title": "Notes",
                "body": "far too long",
            },
            format="json",
        )
    assert resp.status_code == 413
    assert resp.json()["localizable_error"] == "error.413.docs_body_too_large"


def test_body_within_the_ceiling_still_saves(actor, workspace_id):
    doc = _create_doc(actor, workspace_id)
    with override_settings(STAPEL_DOCS={"MAX_BODY_BYTES": 8}):
        assert _put(actor, doc, b"x" * 8, 0).status_code == 200


# ── Workspace quota ──────────────────────────────────────────────────


def test_workspace_quota_refuses_the_write_that_would_cross_it(actor, workspace_id):
    doc = _create_doc(actor, workspace_id)
    with override_settings(STAPEL_DOCS={"WORKSPACE_QUOTA_BYTES": 16}):
        assert _put(actor, doc, b"x" * 10, 0).status_code == 200
        resp = _put(actor, doc, b"y" * 40, 1)
    assert resp.status_code == 507
    assert resp.json()["localizable_error"] == "error.507.docs_workspace_quota"


def test_the_workspace_quota_ships_switched_on(monkeypatch, workspace_id):
    """No override_settings: the SHIPPED configuration must refuse a write
    into a workspace that has already stored more than the default budget.
    A quota that ships at 0 is a limit only the invoice enforces."""
    from stapel_docs import services

    monkeypatch.setattr(
        services, "workspace_usage_bytes", lambda ws: 64 * 1024 * 1024 * 1024
    )
    with pytest.raises(services.DocsError) as exc:
        services.assert_quota(workspace_id, 1)
    assert exc.value.error_key == "error.507.docs_workspace_quota"


def test_quota_zero_means_unlimited(actor, workspace_id):
    doc = _create_doc(actor, workspace_id)
    with override_settings(STAPEL_DOCS={"WORKSPACE_QUOTA_BYTES": 0}):
        assert _put(actor, doc, b"z" * 4096, 0).status_code == 200


# ── Update journal ceilings ──────────────────────────────────────────


def _crdt_doc(actor, workspace_id):
    register_doc_type(
        DocTypeSpec(slug="crdt-test", label="CRDT", collab="crdt", editor_hint="text")
    )
    try:
        return _create_doc(actor, workspace_id, type="crdt-test")
    finally:
        pass


def test_oversized_update_payload_is_refused(actor, workspace_id):
    import base64

    doc = _crdt_doc(actor, workspace_id)
    try:
        payload = base64.b64encode(b"u" * 64).decode()
        with override_settings(STAPEL_DOCS={"MAX_UPDATE_BYTES": 16}):
            resp = actor.post(
                f"{API}/documents/{doc['id']}/updates",
                {"updates": [payload]},
                format="json",
            )
        assert resp.status_code == 413
        assert resp.json()["localizable_error"] == "error.413.docs_update_too_large"
    finally:
        unregister_doc_type("crdt-test")


def test_oversized_update_batch_is_refused(actor, workspace_id):
    import base64

    doc = _crdt_doc(actor, workspace_id)
    try:
        payload = base64.b64encode(b"u").decode()
        with override_settings(STAPEL_DOCS={"MAX_UPDATES_PER_REQUEST": 2}):
            resp = actor.post(
                f"{API}/documents/{doc['id']}/updates",
                {"updates": [payload] * 3},
                format="json",
            )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_too_many_updates"
    finally:
        unregister_doc_type("crdt-test")


# ── Type-specific mutation ───────────────────────────────────────────


def test_content_put_cannot_overwrite_a_file_document(actor, workspace_id):
    """The upload flow owns ``file`` bodies — a content PUT must not be a
    second door around its size/MIME/quota policy."""
    ticket = actor.post(
        f"{API}/uploads",
        {"workspace_id": str(workspace_id), "title": "photo.png", "mime_type": "image/png"},
        format="json",
    ).json()
    resp = actor.put(
        f"{API}/documents/{ticket['document_id']}/content",
        data=b"\x89PNG",
        content_type="image/png",
        HTTP_IF_MATCH="0",
    )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_type_not_editable"


def test_creating_a_file_document_with_a_body_is_refused(actor, workspace_id):
    resp = actor.post(
        f"{API}/documents",
        {
            "workspace_id": str(workspace_id),
            "type": "file",
            "title": "photo.png",
            "body": "not through this door",
        },
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_type_not_editable"


def test_vanished_type_is_read_only(actor, workspace_id):
    """Verdict §7.3 promises read-only for a type whose spec vanished; the
    body write path has to honour it, not just the presentation layer."""
    register_doc_type(DocTypeSpec(slug="exotic", label="Exotic", editor_hint="exotic"))
    try:
        doc = _create_doc(actor, workspace_id, type="exotic", body="payload")
    finally:
        unregister_doc_type("exotic")

    resp = _put(actor, doc, b"overwritten", 1)
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_type_not_editable"
    # Still readable — read-only, never unreadable.
    assert actor.get(f"{API}/documents/{doc['id']}/content").content == b"payload"


# ── Export input ceiling ─────────────────────────────────────────────


def test_export_refuses_an_oversized_body(actor, workspace_id):
    doc = _create_doc(actor, workspace_id, body="x" * 64)
    with override_settings(STAPEL_DOCS={"MAX_EXPORT_BYTES": 16}):
        resp = actor.get(f"{API}/documents/{doc['id']}/export", {"format": "pdf"})
    assert resp.status_code == 413
    assert resp.json()["localizable_error"] == "error.413.docs_export_too_large"
