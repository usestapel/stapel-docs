"""The builtin yjs-codec document types (`ymd` / `ytxt`, 0.7.0).

Registered CONDITIONALLY: a deployment without the [crdt] extra sees no
crdt builtins and nothing else changes. The snapshot body of these types IS
the binary Y state (item identity must survive for convergence), so the
content endpoints serve octet-stream, the content PUT validates the body as
a Y update, and human-readable export is the exporter's job.
"""
import base64
import uuid

import pytest
from django.test import override_settings

pycrdt = pytest.importorskip("pycrdt")

from stapel_docs import crdt  # noqa: E402
from stapel_docs.doc_types import (  # noqa: E402
    COLLAB_CRDT,
    CODEC_YJS,
    get_doc_types,
)

pytestmark = pytest.mark.django_db

API = "/docs/api/v1"


def _state_of(text: str) -> bytes:
    doc = pycrdt.Doc()
    doc["content"] = pycrdt.Text()
    doc.get("content", type=pycrdt.Text).insert(0, text)
    return doc.get_update()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


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
def ymd_doc(actor, workspace_id):
    resp = actor.post(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "type": "ymd", "title": "Live notes"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    return resp.json()


# ── conditional registration ─────────────────────────────────────────


def test_builtin_crdt_types_registered_when_pycrdt_present():
    registry = get_doc_types()
    for slug in ("ymd", "ytxt"):
        spec = registry[slug]
        assert spec.collab == COLLAB_CRDT
        assert spec.codec == CODEC_YJS
        assert spec.diffable is False
        assert spec.empty_body == crdt.EMPTY_STATE
        assert spec.text_extractor is crdt.extract_text
    assert registry["ymd"].editor_hint == "markdown.crdt"
    assert registry["ymd"].mime_type == "text/markdown"
    assert registry["ymd"].extension == ".md"
    assert registry["ytxt"].editor_hint == "text.crdt"
    assert registry["ytxt"].mime_type == "text/plain"
    assert registry["ytxt"].extension == ".txt"


def test_builtins_absent_without_pycrdt(monkeypatch):
    """A deployment without the [crdt] extra sees NO crdt builtins — the
    registry answer, not an import error, is the degradation."""
    monkeypatch.setattr(crdt, "available", lambda: False)
    registry = get_doc_types()
    assert "ymd" not in registry
    assert "ytxt" not in registry
    # ...and nothing else changed.
    assert {"txt", "md", "csv", "file"} <= set(registry)


def test_snapshot_builtins_unchanged():
    """Additive: the existing snapshot types keep their exact discipline."""
    registry = get_doc_types()
    assert registry["md"].collab == "snapshot"
    assert registry["md"].codec == ""
    assert registry["txt"].diffable is True


# ── the content surface of a yjs-codec type ──────────────────────────


def test_new_ymd_document_serves_the_empty_y_state(actor, ymd_doc):
    resp = actor.get(f"{API}/documents/{ymd_doc['id']}/content")
    assert resp.status_code == 200
    assert resp.content == crdt.EMPTY_STATE
    # The wire body is the binary Y state, not the type's logical text mime.
    assert resp["Content-Type"] == "application/octet-stream"


def test_document_envelope_carries_the_crdt_discipline(ymd_doc):
    assert ymd_doc["collab"] == "crdt"
    assert ymd_doc["editor_hint"] == "markdown.crdt"
    assert ymd_doc["diffable"] is False


def test_content_put_accepts_a_valid_y_state(actor, ymd_doc):
    state = _state_of("# saved from a client fold")
    resp = actor.put(
        f"{API}/documents/{ymd_doc['id']}/content",
        data=state,
        content_type="application/octet-stream",
        HTTP_IF_MATCH='"0"',
    )
    assert resp.status_code == 200, resp.content
    read = actor.get(f"{API}/documents/{ymd_doc['id']}/content")
    assert read.content == state


def test_content_put_refuses_a_body_that_is_not_a_y_update(actor, ymd_doc):
    """A text body stored as the "snapshot" of a crdt type would corrupt the
    discipline — clients holding older Y docs could never converge on it."""
    resp = actor.put(
        f"{API}/documents/{ymd_doc['id']}/content",
        data=b"# markdown, not a Y update",
        content_type="text/markdown",
        HTTP_IF_MATCH='"0"',
    )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_invalid_crdt_payload"


def test_create_with_body_validates_the_same_way(actor, workspace_id):
    resp = actor.post(
        f"{API}/documents",
        {
            "workspace_id": str(workspace_id),
            "type": "ymd",
            "title": "Broken",
            "body": "plain text is not a Y state",
        },
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_invalid_crdt_payload"


def test_snapshot_types_still_accept_arbitrary_text(actor, workspace_id):
    """The validation is codec-scoped: md/txt bodies are untouched."""
    doc = actor.post(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "type": "md", "title": "Plain"},
        format="json",
    ).json()
    resp = actor.put(
        f"{API}/documents/{doc['id']}/content",
        data=b"# any text",
        content_type="text/markdown",
        HTTP_IF_MATCH='"0"',
    )
    assert resp.status_code == 200


def test_append_updates_validates_yjs_payloads(actor, ymd_doc):
    good = actor.post(
        f"{API}/documents/{ymd_doc['id']}/updates",
        {"updates": [_b64(_state_of("hi"))]},
        format="json",
    )
    assert good.status_code == 200, good.content
    bad = actor.post(
        f"{API}/documents/{ymd_doc['id']}/updates",
        {"updates": [_b64(b"garbage bytes")]},
        format="json",
    )
    assert bad.status_code == 400
    assert bad.json()["localizable_error"] == "error.400.docs_invalid_crdt_payload"


def test_host_crdt_types_with_their_own_codec_are_not_validated(actor, workspace_id):
    """The journal stays opaque for a crdt type whose codec this library
    does not own — validation is a property of codec="yjs", not of the
    discipline."""
    from stapel_docs.doc_types import DocTypeSpec, register_doc_type, unregister_doc_type

    register_doc_type(
        DocTypeSpec(slug="hostcrdt", label="Host CRDT", collab=COLLAB_CRDT)
    )
    try:
        doc = actor.post(
            f"{API}/documents",
            {"workspace_id": str(workspace_id), "type": "hostcrdt", "title": "H"},
            format="json",
        ).json()
        resp = actor.post(
            f"{API}/documents/{doc['id']}/updates",
            {"updates": [_b64(b"opaque-host-bytes")]},
            format="json",
        )
        assert resp.status_code == 200
    finally:
        unregister_doc_type("hostcrdt")


# ── export: the human-readable form of a Y body ──────────────────────


def test_export_md_serves_extracted_markdown(actor, ymd_doc):
    state = _state_of("# Title\n\nbody")
    actor.put(
        f"{API}/documents/{ymd_doc['id']}/content",
        data=state,
        content_type="application/octet-stream",
        HTTP_IF_MATCH='"0"',
    )
    resp = actor.get(f"{API}/documents/{ymd_doc['id']}/export", {"format": "md"})
    assert resp.status_code == 200, resp.content
    assert resp["Content-Type"].startswith("text/markdown")
    assert resp.content.decode("utf-8") == "# Title\n\nbody"


def test_export_txt_serves_extracted_text(actor, workspace_id):
    doc = actor.post(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "type": "ytxt", "title": "Live txt"},
        format="json",
    ).json()
    state = _state_of("plain live text")
    actor.put(
        f"{API}/documents/{doc['id']}/content",
        data=state,
        content_type="application/octet-stream",
        HTTP_IF_MATCH='"0"',
    )
    resp = actor.get(f"{API}/documents/{doc['id']}/export", {"format": "txt"})
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/plain")
    assert resp.content.decode("utf-8") == "plain live text"


def test_export_pdf_renders_the_extracted_markdown(actor, ymd_doc):
    state = _state_of("# Heading\n\nparagraph")
    actor.put(
        f"{API}/documents/{ymd_doc['id']}/content",
        data=state,
        content_type="application/octet-stream",
        HTTP_IF_MATCH='"0"',
    )
    resp = actor.get(f"{API}/documents/{ymd_doc['id']}/export", {"format": "pdf"})
    assert resp.status_code == 200, resp.content
    assert resp.content.startswith(b"%PDF")


def test_export_md_of_an_opaque_file_is_400(actor, workspace_id):
    doc = actor.post(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "type": "file", "title": "Blob"},
        format="json",
    ).json()
    resp = actor.get(f"{API}/documents/{doc['id']}/export", {"format": "md"})
    assert resp.status_code == 400


def test_text_extractor_reads_the_y_state():
    """What feeds search/knowledge is the extracted text, never the binary."""
    registry = get_doc_types()
    state = crdt.fold(crdt.EMPTY_STATE, [_state_of("indexable words")])
    assert registry["ymd"].text_extractor(state) == "indexable words"
