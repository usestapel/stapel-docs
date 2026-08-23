"""Subject-scoped erasure (stapel-gdpr 0.5.0): one subscriber that erases
what docs owns about a subject, receipts what it removed, and answers the
liveness probe from the same module.

VALIDATE_SCHEMAS is on (conftest), so every receipt these tests capture was
validated against ``schemas/emits/gdpr.section.erased.json`` on the way out.
"""
import uuid

import pytest
from django.test import override_settings
from stapel_core.comm import emit, subscribe_action

from stapel_docs.models import Document, DocumentUpdate, Folder, Revision, UploadSession
from stapel_docs.storage import get_storage

pytestmark = pytest.mark.django_db

API = "/docs/api/v1"

_RECEIPTS: list[dict] = []
_ALIVE: list[dict] = []


def _collect_receipt(event):
    _RECEIPTS.append(event.payload)


def _collect_alive(event):
    _ALIVE.append(event.payload)


@pytest.fixture(autouse=True)
def bus():
    """Listen to what this owner answers. ``subscribe`` dedups an identical
    handler, so re-registering per test never stacks subscriptions."""
    subscribe_action("gdpr.section.erased", _collect_receipt)
    subscribe_action("gdpr.owner.alive", _collect_alive)
    _RECEIPTS.clear()
    _ALIVE.clear()
    yield
    _RECEIPTS.clear()
    _ALIVE.clear()


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
    payload = {"workspace_id": str(workspace_id), "type": "md", "title": "Doc"}
    payload.update(overrides)
    resp = actor.post(f"{API}/documents", payload, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def _put(actor, doc_id, body, seq):
    resp = actor.put(
        f"{API}/documents/{doc_id}/content",
        data=body,
        content_type="text/markdown",
        HTTP_IF_MATCH=f'"{seq}"',
    )
    assert resp.status_code == 200, resp.content
    return resp


def _keys_of(document_id):
    keys = set(
        Revision.objects.filter(document_id=document_id).values_list(
            "storage_key", flat=True
        )
    )
    snapshot_key = Document.objects.get(pk=document_id).snapshot_key
    if snapshot_key:
        keys.add(snapshot_key)
    return {k for k in keys if k}


def _request_erasure(subject_type, subject_key, **extra):
    payload = {
        "request_id": 7,
        "correlation_id": str(uuid.uuid4()),
        "subject_type": subject_type,
        "subject_key": str(subject_key),
    }
    payload.update(extra)
    emit("gdpr.erasure.requested", payload)
    return payload


class TestDocumentSubject:
    def test_destroys_rows_journal_and_objects(
        self, actor, workspace_id, django_capture_on_commit_callbacks
    ):
        with override_settings(STAPEL_DOCS={"AUTO_REVISION_INTERVAL_SECONDS": 0}):
            doc = _create_doc(actor, workspace_id, title="Erase me")
            _put(actor, doc["id"], b"v1", 0)
            _put(actor, doc["id"], b"v2", 1)

        keys = _keys_of(doc["id"])
        storage = get_storage()
        assert keys and any(storage.head_object(k)[0] for k in keys)

        # Objects die after the commit (purge discipline), so the deletion is
        # observed on commit — same as trash purge.
        with django_capture_on_commit_callbacks(execute=True):
            request = _request_erasure("document", doc["id"])

        assert not Document.objects.filter(pk=doc["id"]).exists()
        assert not Revision.objects.filter(document_id=doc["id"]).exists()
        assert not DocumentUpdate.objects.filter(document_id=doc["id"]).exists()
        for key in keys:
            assert not storage.head_object(key)[0], key

        (receipt,) = _RECEIPTS
        assert receipt["owner"] == "docs"
        assert receipt["correlation_id"] == request["correlation_id"]
        assert receipt["subject_type"] == "document"
        assert receipt["subject_key"] == str(doc["id"])
        assert receipt["receipt_id"] == f"docs:{request['correlation_id']}"
        assert receipt["counts"]["documents"] == 1
        assert receipt["counts"]["revisions"] == len(keys)
        assert receipt["counts"]["storage_objects"] == len(keys)

    def test_a_trashed_document_is_erased_too(self, actor, workspace_id):
        """An erasure is not a trash operation: it does not wait out the
        retention window, and a trashed row is not 'already handled'."""
        doc = _create_doc(actor, workspace_id)
        assert actor.delete(f"{API}/documents/{doc['id']}").status_code == 204
        assert Document.objects.get(pk=doc["id"]).deleted_at is not None

        _request_erasure("document", doc["id"])

        assert not Document.objects.filter(pk=doc["id"]).exists()
        assert _RECEIPTS[0]["counts"]["documents"] == 1

    def test_other_documents_survive(self, actor, workspace_id):
        doomed = _create_doc(actor, workspace_id, title="Doomed")
        neighbour = _create_doc(actor, workspace_id, title="Neighbour")

        _request_erasure("document", doomed["id"])

        assert not Document.objects.filter(pk=doomed["id"]).exists()
        assert Document.objects.filter(pk=neighbour["id"]).exists()

    def test_a_contradictory_workspace_id_refuses_instead_of_receipting(
        self, actor, workspace_id
    ):
        """The pair in the request has to agree with the row: erasing anyway
        obeys a pair nobody vouched for, and receipting zeros would certify
        an erasure that never happened. It refuses, and the part stays open."""
        from stapel_core.comm.exceptions import ActionDeliveryError

        doc = _create_doc(actor, workspace_id)

        with pytest.raises(ActionDeliveryError):
            _request_erasure("document", doc["id"], workspace_id=str(uuid.uuid4()))

        assert Document.objects.filter(pk=doc["id"]).exists()
        assert _RECEIPTS == []

    def test_redelivery_receipts_zeros(self, actor, workspace_id):
        doc = _create_doc(actor, workspace_id)

        _request_erasure("document", doc["id"])
        _request_erasure("document", doc["id"])

        first, second = _RECEIPTS
        assert first["counts"]["documents"] == 1
        assert second["counts"]["documents"] == 0


class TestWorkspaceSubject:
    def test_destroys_the_whole_corpus(
        self, actor, user, workspace_id, django_capture_on_commit_callbacks
    ):
        folder = actor.post(
            f"{API}/folders",
            {"workspace_id": str(workspace_id), "name": "f"},
            format="json",
        ).json()
        first = _create_doc(actor, workspace_id, title="One", folder_id=folder["id"])
        _put(actor, first["id"], b"body", 0)
        second = _create_doc(actor, workspace_id, title="Two")
        assert actor.delete(f"{API}/documents/{second['id']}").status_code == 204

        staging_key = f"docs/{workspace_id}/staging-blob"
        get_storage().put_bytes(staging_key, b"unfinalized", content_type="text/plain")
        UploadSession.objects.create(
            workspace_id=workspace_id, title="up", key=staging_key, created_by=user
        )

        keys = _keys_of(first["id"]) | {staging_key}

        with django_capture_on_commit_callbacks(execute=True):
            _request_erasure("workspace", workspace_id)

        assert not Document.objects.filter(workspace_id=workspace_id).exists()
        assert not Folder.objects.filter(workspace_id=workspace_id).exists()
        assert not UploadSession.objects.filter(workspace_id=workspace_id).exists()
        storage = get_storage()
        for key in keys:
            assert not storage.head_object(key)[0], key

        (receipt,) = _RECEIPTS
        assert receipt["subject_type"] == "workspace"
        counts = receipt["counts"]
        assert counts["documents"] == 2  # the live one and the trashed one
        assert counts["folders"] == 1
        assert counts["upload_sessions"] == 1
        assert counts["storage_objects"] == len(keys)

    def test_a_neighbouring_workspace_is_untouched(self, actor, api_client, user,
                                                   grant_capabilities, workspace_id):
        other_ws = uuid.uuid4()
        grant_capabilities(other_ws, user.pk)
        mine = _create_doc(actor, workspace_id)
        theirs = _create_doc(actor, other_ws)

        _request_erasure("workspace", workspace_id)

        assert not Document.objects.filter(pk=mine["id"]).exists()
        assert Document.objects.filter(pk=theirs["id"]).exists()

    def test_redelivery_receipts_zeros(self, actor, workspace_id):
        _create_doc(actor, workspace_id)

        _request_erasure("workspace", workspace_id)
        _request_erasure("workspace", workspace_id)

        first, second = _RECEIPTS
        assert first["counts"]["documents"] == 1
        assert second["counts"] == {
            "documents": 0,
            "revisions": 0,
            "updates": 0,
            "folders": 0,
            "upload_sessions": 0,
            "storage_objects": 0,
        }


class TestAccountSubject:
    """Documents are co-produced workspace content: an account erasure
    anonymizes authorship and keeps the corpus (storage-verdict §3)."""

    def test_anonymizes_and_receipts_counts(self, actor, user, workspace_id):
        doc = _create_doc(actor, workspace_id)
        _put(actor, doc["id"], b"body", 0)
        folder = Folder.objects.create(
            workspace_id=workspace_id, name="owned", created_by=user
        )

        _request_erasure("account", user.pk)

        assert Document.objects.get(pk=doc["id"]).owner_id is None
        folder.refresh_from_db()
        assert folder.created_by_id is None
        assert Document.objects.filter(pk=doc["id"]).exists()

        (receipt,) = _RECEIPTS
        assert receipt["subject_type"] == "account"
        assert receipt["counts"]["documents_anonymized"] == 1
        assert receipt["counts"]["folders_anonymized"] == 1
        assert receipt["counts"]["revisions_anonymized"] == 1

    def test_redelivery_receipts_zeros(self, actor, user, workspace_id):
        _create_doc(actor, workspace_id)

        _request_erasure("account", user.pk)
        _request_erasure("account", user.pk)

        first, second = _RECEIPTS
        assert first["counts"]["documents_anonymized"] == 1
        assert second["counts"]["documents_anonymized"] == 0

    def test_user_deleted_still_erases_and_stays_silent(self, actor, user, workspace_id):
        """The deprecated event keeps working for one minor and routes
        through the same erase; the receipt belongs to the erasure request
        that fires alongside it, so this path emits none."""
        doc = _create_doc(actor, workspace_id)

        emit("user.deleted", {"user_id": str(user.pk)})

        assert Document.objects.get(pk=doc["id"]).owner_id is None
        assert _RECEIPTS == []


class TestForeignAndMalformedRequests:
    def test_subject_type_this_owner_does_not_claim_is_ignored(
        self, actor, workspace_id
    ):
        doc = _create_doc(actor, workspace_id)

        _request_erasure("recording", doc["id"])

        assert Document.objects.filter(pk=doc["id"]).exists()
        assert _RECEIPTS == []

    def test_request_without_a_subject_is_not_receipted(self):
        emit("gdpr.erasure.requested", {"correlation_id": str(uuid.uuid4())})

        assert _RECEIPTS == []

    def test_erase_refuses_an_unknown_subject_type(self):
        from stapel_docs.erasure import erase

        with pytest.raises(ValueError):
            erase("meeting", uuid.uuid4())


class TestOwnerProbe:
    def test_answers_alive_with_the_subjects_it_claims(self):
        correlation_id = str(uuid.uuid4())

        emit("gdpr.owner.probe", {"correlation_id": correlation_id})

        (answer,) = _ALIVE
        assert answer == {
            "owner": "docs",
            "subject_types": ["account", "workspace", "document"],
            "correlation_id": correlation_id,
        }

    def test_the_probe_is_answered_from_the_erasure_subscriber(self):
        """Co-location is the contract, not a detail: gdpr's W006 reads
        these answers as evidence that the erasure path is consumed, so an
        answer from a module that does not also erase would make the check
        lie."""
        from stapel_core.comm.registry import action_registry

        erasers = action_registry.handlers("gdpr.erasure.requested")
        answerers = action_registry.handlers("gdpr.owner.probe")
        assert {h.__module__ for h in erasers} == {"stapel_docs.actions"}
        assert {h.__module__ for h in answerers} == {"stapel_docs.actions"}

    def test_claimed_subjects_match_the_erase_dispatch(self):
        from stapel_docs.erasure import SUBJECT_TYPES, erase

        for subject_type in SUBJECT_TYPES:
            # every claimed subject has an implementation (no ValueError)
            erase(subject_type, uuid.uuid4())
