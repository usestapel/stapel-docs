"""comm surface — docs.create_document via call() + emit schema validation.

VALIDATE_SCHEMAS is on (conftest), so these tests enforce the committed
contracts in ``schemas/``: events.py payloads against ``schemas/emits/``,
the Function payload against ``schemas/functions/docs.create_document.json``.
"""
import uuid
from types import SimpleNamespace

import pytest
from django.test import override_settings
from stapel_core.comm import call
from stapel_core.comm.exceptions import FunctionCallError, SchemaValidationError

from stapel_docs import events


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username="alice", email="alice@example.com", password="x"
    )


def _fake_document(**overrides):
    """Duck-typed stand-in matching the attrs events.py reads — lets the
    emit payloads be exercised against their schemas without services."""
    base = dict(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        type="md",
        title="Standup notes",
        folder_id=None,
        owner_id=None,
        head_seq=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestEmitSchemas:
    """events.py payloads must match schemas/emits/ exactly."""

    def test_document_created_nullable_and_filled(self):
        events.emit_document_created(_fake_document())
        events.emit_document_created(
            _fake_document(folder_id=uuid.uuid4(), owner_id=uuid.uuid4())
        )

    def test_document_updated(self):
        events.emit_document_updated(_fake_document())

    def test_document_deleted(self):
        events.emit_document_deleted(_fake_document())

    def test_storage_changed_signed_delta(self):
        events.emit_storage_changed(uuid.uuid4(), 1024)
        events.emit_storage_changed(uuid.uuid4(), -1024)

    def test_bad_emit_payload_rejected(self):
        from stapel_core.comm import emit

        with pytest.raises(SchemaValidationError):
            emit("document.updated", {"document_id": "x"})  # missing fields


@pytest.mark.django_db
class TestCreateDocumentFunction:
    """The comm surface has no session, so every call carries its authority
    (audit DOCS-02): `actor_id` is authorized through the same choke point
    as an HTTP caller, or the calling service is one the host trusts."""

    def test_schema_rejects_bad_payload(self):
        with pytest.raises(SchemaValidationError):
            call("docs.create_document", {"workspace_id": str(uuid.uuid4())})

    def test_unknown_type_is_loud(self, user, grant_capabilities):
        ws = uuid.uuid4()
        grant_capabilities(ws, user.pk)
        with pytest.raises(FunctionCallError, match="DocTypeNotRegistered"):
            call(
                "docs.create_document",
                {
                    "workspace_id": str(ws),
                    "type": "nope",
                    "title": "t",
                    "actor_id": str(user.pk),
                },
            )

    def test_end_to_end_create(self, tmp_path, user, grant_capabilities):
        pytest.importorskip("stapel_docs.services")
        from stapel_docs.models import Document, Folder
        from stapel_docs.storage import get_storage

        ws = uuid.uuid4()
        grant_capabilities(ws, user.pk)
        body = "# notes\nhello"
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            res = call(
                "docs.create_document",
                {
                    "workspace_id": str(ws),
                    "type": "md",
                    "title": "Standup notes",
                    "folder_path": "/Meetings/2026-08",
                    "body": body,
                    "metadata": {"source": "ironmemo"},
                    "actor_id": str(user.pk),
                    "owner_id": str(user.pk),
                },
            )
            doc = Document.objects.get(id=res["document_id"])
            assert doc.workspace_id == ws
            assert doc.type == "md"
            assert doc.title == "Standup notes"
            assert doc.owner_id == user.pk
            assert doc.metadata == {"source": "ironmemo"}
            # folder_path created the chain and attached the document
            parent = Folder.objects.get(workspace_id=ws, name="Meetings")
            child = Folder.objects.get(workspace_id=ws, name="2026-08", parent=parent)
            assert doc.folder_id == child.id
            # body round-trips through the storage seam
            assert doc.snapshot_key
            assert get_storage().get_bytes(doc.snapshot_key) == body.encode("utf-8")

    def test_folder_path_idempotent(self, tmp_path, user, grant_capabilities):
        pytest.importorskip("stapel_docs.services")
        from stapel_docs.models import Folder

        ws = uuid.uuid4()
        grant_capabilities(ws, user.pk)
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            for title in ("a", "b"):
                call(
                    "docs.create_document",
                    {
                        "workspace_id": str(ws),
                        "type": "txt",
                        "title": title,
                        "folder_path": "/Meetings/2026-08",
                        "actor_id": str(user.pk),
                    },
                )
            assert Folder.objects.filter(workspace_id=ws).count() == 2

    def test_vanished_owner_degrades_to_none(self, tmp_path, user, grant_capabilities):
        pytest.importorskip("stapel_docs.services")
        from stapel_docs.models import Document

        ws = uuid.uuid4()
        grant_capabilities(ws, user.pk)
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            res = call(
                "docs.create_document",
                {
                    "workspace_id": str(ws),
                    "type": "txt",
                    "title": "orphan",
                    "actor_id": str(user.pk),
                    "owner_id": str(uuid.uuid4()),  # no such user (erased)
                },
            )
        assert Document.objects.get(id=res["document_id"]).owner_id is None


@pytest.mark.django_db
class TestCreateDocumentAuthority:
    """Audit DOCS-02: the internal create path is scoped to a caller.

    Without this the seam accepted any workspace and any owner from anyone
    who could reach the bus — a service compromise anywhere on the bus was
    a write into every tenant's document tree.
    """

    def _payload(self, ws, **overrides):
        payload = {"workspace_id": str(ws), "type": "txt", "title": "t"}
        payload.update(overrides)
        return payload

    def test_call_without_a_caller_is_refused(self, tmp_path, db):
        from stapel_docs.models import Document

        ws = uuid.uuid4()
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            with pytest.raises(FunctionCallError, match="CallerNotAuthorized"):
                call("docs.create_document", self._payload(ws))
        assert not Document.objects.filter(workspace_id=ws).exists()

    def test_actor_without_edit_in_the_workspace_is_refused(
        self, tmp_path, user, grant_capabilities
    ):
        ws = uuid.uuid4()
        grant_capabilities(ws, user.pk, "docs.view")  # read-only member
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            with pytest.raises(FunctionCallError, match="CallerNotAuthorized"):
                call("docs.create_document", self._payload(ws, actor_id=str(user.pk)))

    def test_actor_from_another_workspace_cannot_write_here(
        self, tmp_path, user, grant_capabilities
    ):
        home, target = uuid.uuid4(), uuid.uuid4()
        grant_capabilities(home, user.pk)  # full rights, wrong workspace
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            with pytest.raises(FunctionCallError, match="CallerNotAuthorized"):
                call("docs.create_document", self._payload(target, actor_id=str(user.pk)))

    def test_trusted_service_may_act_without_a_user(self, tmp_path, db):
        from stapel_docs.models import Document

        ws = uuid.uuid4()
        with override_settings(
            MEDIA_ROOT=str(tmp_path),
            STAPEL_DOCS={"INTERNAL_TRUSTED_SERVICES": ["ironmemo"]},
        ):
            res = call(
                "docs.create_document", self._payload(ws, caller_service="ironmemo")
            )
        assert Document.objects.get(id=res["document_id"]).workspace_id == ws

    def test_untrusted_service_name_is_not_a_caller(self, tmp_path, db):
        ws = uuid.uuid4()
        with override_settings(
            MEDIA_ROOT=str(tmp_path),
            STAPEL_DOCS={"INTERNAL_TRUSTED_SERVICES": ["ironmemo"]},
        ):
            with pytest.raises(FunctionCallError, match="CallerNotAuthorized"):
                call(
                    "docs.create_document", self._payload(ws, caller_service="whoever")
                )

    def test_owner_must_be_a_member_too(self, tmp_path, user, grant_capabilities):
        """Attribution to another person is still a claim about that person."""
        from django.contrib.auth import get_user_model

        ws = uuid.uuid4()
        grant_capabilities(ws, user.pk)
        outsider = get_user_model().objects.create(username="outsider")
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            with pytest.raises(FunctionCallError, match="CallerNotAuthorized"):
                call(
                    "docs.create_document",
                    self._payload(ws, actor_id=str(user.pk), owner_id=str(outsider.pk)),
                )

    def test_actor_owns_the_document_by_default(
        self, tmp_path, user, grant_capabilities
    ):
        from stapel_docs.models import Document

        ws = uuid.uuid4()
        grant_capabilities(ws, user.pk)
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            res = call("docs.create_document", self._payload(ws, actor_id=str(user.pk)))
        assert Document.objects.get(id=res["document_id"]).owner_id == user.pk

    def test_host_may_open_the_seam_deliberately(self, tmp_path, db):
        """REQUIRE_CALLER off is a documented single-tenant choice — the
        point is that it is a choice, not the shipped default."""
        from stapel_docs.models import Document

        ws = uuid.uuid4()
        with override_settings(
            MEDIA_ROOT=str(tmp_path), STAPEL_DOCS={"INTERNAL_REQUIRE_CALLER": False}
        ):
            res = call("docs.create_document", self._payload(ws))
        assert Document.objects.filter(id=res["document_id"]).exists()

    def test_workspaces_outage_is_not_an_allow(self, tmp_path, user, monkeypatch):
        """Fail-closed: no verdict means no write, never a default yes."""
        from stapel_docs import authz

        monkeypatch.setattr(authz, "authorize", lambda **kw: authz.UNAVAILABLE)
        ws = uuid.uuid4()
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            with pytest.raises(FunctionCallError, match="workspaces_unavailable"):
                call("docs.create_document", self._payload(ws, actor_id=str(user.pk)))
