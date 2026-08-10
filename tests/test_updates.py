"""Update journal (crdt discipline) via a runtime-registered fake type:
append, dedup, replay-from-seq, resync after compaction."""
import base64
import uuid

import pytest
from django.test import override_settings

from stapel_docs.doc_types import (
    COLLAB_CRDT,
    DocTypeSpec,
    register_doc_type,
    unregister_doc_type,
)
from stapel_docs.models import DocumentUpdate

pytestmark = pytest.mark.django_db

API = "/docs/api/v1"


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
def crdt_type():
    """No builtin v1 type is crdt — the journal substrate is exercised with
    a runtime-registered fake, exactly as a host would register one."""
    spec = DocTypeSpec(
        slug="fakecrdt", label="Fake CRDT", collab=COLLAB_CRDT, editor_hint="fake"
    )
    register_doc_type(spec)
    yield spec
    unregister_doc_type("fakecrdt")


@pytest.fixture
def doc(actor, workspace_id, crdt_type):
    resp = actor.post(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "type": "fakecrdt", "title": "Live"},
        format="json",
    )
    assert resp.status_code == 201
    return resp.json()


def test_append_refused_for_snapshot_types(actor, workspace_id):
    md = actor.post(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "type": "md", "title": "Doc"},
        format="json",
    ).json()
    resp = actor.post(
        f"{API}/documents/{md['id']}/updates",
        {"updates": [_b64(b"u1")]},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_updates_not_crdt"


def test_append_and_replay(actor, user, doc):
    resp = actor.post(
        f"{API}/documents/{doc['id']}/updates",
        {"updates": [_b64(b"u1"), _b64(b"u2")]},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json() == {"head_seq": 2}

    rows = DocumentUpdate.objects.filter(document_id=doc["id"]).order_by("seq")
    assert [r.seq for r in rows] == [1, 2]
    assert all(str(r.author_id) == str(user.pk) for r in rows)  # I4

    feed = actor.get(f"{API}/documents/{doc['id']}/updates", {"since": "0"})
    assert feed.status_code == 200
    body = feed.json()
    assert body["head_seq"] == 2
    assert [u["seq"] for u in body["updates"]] == [1, 2]
    assert base64.b64decode(body["updates"][0]["payload"]) == b"u1"
    assert body["updates"][0]["author_id"] == str(user.pk)

    partial = actor.get(f"{API}/documents/{doc['id']}/updates", {"since": "1"}).json()
    assert [u["seq"] for u in partial["updates"]] == [2]


def test_duplicate_client_batch_is_ignored(actor, doc):
    payload = {"updates": [_b64(b"u1")], "client_id": "c-1", "client_seq": 7}
    first = actor.post(f"{API}/documents/{doc['id']}/updates", payload, format="json")
    assert first.json() == {"head_seq": 1}
    retry = actor.post(f"{API}/documents/{doc['id']}/updates", payload, format="json")
    assert retry.status_code == 200
    assert retry.json() == {"head_seq": 1}  # silently ignored, no second row
    assert DocumentUpdate.objects.filter(document_id=doc["id"]).count() == 1


def test_invalid_since_is_400(actor, doc):
    for bad in ("x", "-1", "1.5"):
        resp = actor.get(f"{API}/documents/{doc['id']}/updates", {"since": bad})
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_bad_since"


def test_snapshot_types_always_answer_empty_feed(actor, workspace_id):
    md = actor.post(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "type": "md", "title": "Doc", "body": "x"},
        format="json",
    ).json()
    feed = actor.get(f"{API}/documents/{md['id']}/updates").json()
    assert feed == {"head_seq": 1, "updates": []}


def test_invalid_base64_is_a_validation_error(actor, doc):
    resp = actor.post(
        f"{API}/documents/{doc['id']}/updates",
        {"updates": ["not base64!!"]},
        format="json",
    )
    assert resp.status_code == 400


def test_resync_after_compaction(actor, doc):
    with override_settings(STAPEL_DOCS={"REPLAY_WINDOW": 2}):
        actor.post(
            f"{API}/documents/{doc['id']}/updates",
            {"updates": [_b64(b"u1"), _b64(b"u2"), _b64(b"u3")]},
            format="json",
        )
        # A snapshot save at S=4 compacts journal rows with seq <= S-2.
        saved = actor.put(
            f"{API}/documents/{doc['id']}/content",
            data=b"merged state",
            content_type="application/octet-stream",
            HTTP_IF_MATCH='"3"',
        )
        assert saved.status_code == 200
        assert saved.json()["head_seq"] == 4

        remaining = DocumentUpdate.objects.filter(document_id=doc["id"])
        assert [r.seq for r in remaining] == [3]

        # since=0 fell out of the window -> chat-style resync frame.
        resync = actor.get(f"{API}/documents/{doc['id']}/updates", {"since": "0"})
        assert resync.status_code == 200
        assert resync.json() == {"resync": True, "head_seq": 4, "snapshot_seq": 4}

        # since=2 still touches the retained tail -> normal replay.
        feed = actor.get(f"{API}/documents/{doc['id']}/updates", {"since": "2"}).json()
        assert [u["seq"] for u in feed["updates"]] == [3]
        assert feed["head_seq"] == 4
