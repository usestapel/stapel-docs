"""``docs.usage`` — the metering surface billing composes with (§3.4).

VALIDATE_SCHEMAS is on (conftest), so every call here also enforces
``schemas/functions/docs.usage.json``.

Two things are pinned beyond the arithmetic. First, invariant I2: the
reported ``bytes_total`` is the SAME sum the 507 quota refuses against — a
meter and a refusal that disagree let an operator watch a workspace fill up
while the invoice says it is empty. Second, authority: the surface is
read-only but the data is a workspace's, so it takes the same caller gate as
the write seam. "It only reads" is how an enumerable per-workspace corpus
size ends up available to every participant on the bus.
"""
import uuid

import pytest
from django.test import override_settings
from stapel_core.comm import call
from stapel_core.comm.exceptions import FunctionCallError, SchemaValidationError

from stapel_docs import services
from stapel_docs.models import Document, Folder

pytestmark = pytest.mark.django_db

API = "/docs/api/v1"


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}", email="u@example.com", password="x"
    )


@pytest.fixture
def workspace_id():
    return uuid.uuid4()


@pytest.fixture
def actor(api_client, user, grant_capabilities, workspace_id):
    api_client.force_authenticate(user=user)
    grant_capabilities(workspace_id, user.pk)
    return api_client


def _usage(user, workspace_id):
    return call(
        "docs.usage",
        {"workspace_id": str(workspace_id), "actor_id": str(user.pk)},
    )


class TestSchema:
    def test_workspace_id_is_required(self):
        with pytest.raises(SchemaValidationError):
            call("docs.usage", {})

    def test_unknown_fields_are_refused(self, user, workspace_id,
                                        grant_capabilities):
        grant_capabilities(workspace_id, user.pk)
        with pytest.raises(SchemaValidationError):
            call(
                "docs.usage",
                {"workspace_id": str(workspace_id), "tenant": "nope"},
            )


class TestAggregates:
    def test_empty_workspace_is_all_zeros(self, user, workspace_id,
                                          grant_capabilities):
        grant_capabilities(workspace_id, user.pk)
        assert _usage(user, workspace_id) == {
            "bytes_live": 0,
            "bytes_trash": 0,
            "bytes_total": 0,
            "documents": 0,
            "folders": 0,
            "by_type": {},
        }

    def test_counts_and_bytes_match_the_fixtures(self, actor, user, workspace_id):
        actor.post(
            f"{API}/folders",
            {"workspace_id": str(workspace_id), "name": "Meetings"},
            format="json",
        )
        for title, body in (("A", "# aa"), ("B", "# bbbb")):
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
            assert resp.status_code == 201

        usage = _usage(user, workspace_id)
        assert usage["documents"] == 2
        assert usage["folders"] == 1
        assert usage["by_type"]["md"]["documents"] == 2
        # Heads plus the auto revisions minted with them: the same rows the
        # quota sums, not a second (smaller, friendlier) number.
        assert usage["bytes_live"] == services.workspace_usage_bytes(workspace_id)
        assert usage["bytes_total"] == usage["bytes_live"]
        assert usage["bytes_trash"] == 0

    def test_by_type_sums_to_bytes_live(self, actor, user, workspace_id):
        for kind, title, body in (("md", "A", "# a"), ("txt", "B", "bbbb")):
            actor.post(
                f"{API}/documents",
                {
                    "workspace_id": str(workspace_id),
                    "type": kind,
                    "title": title,
                    "body": body,
                },
                format="json",
            )
        usage = _usage(user, workspace_id)
        assert set(usage["by_type"]) == {"md", "txt"}
        assert (
            sum(entry["bytes"] for entry in usage["by_type"].values())
            == usage["bytes_live"]
        )

    def test_trash_is_charged_not_discounted(self, actor, user, workspace_id):
        resp = actor.post(
            f"{API}/documents",
            {
                "workspace_id": str(workspace_id),
                "type": "md",
                "title": "Doomed",
                "body": "# bytes",
            },
            format="json",
        )
        doc = resp.json()
        before = _usage(user, workspace_id)
        actor.delete(f"{API}/documents/{doc['id']}")

        after = _usage(user, workspace_id)
        assert after["documents"] == 0
        assert after["by_type"] == {}
        assert after["bytes_live"] == 0
        assert after["bytes_trash"] == before["bytes_live"]
        # I2: total is invariant across trashing, and equals the quota sum.
        assert after["bytes_total"] == before["bytes_total"]
        assert after["bytes_total"] == services.workspace_usage_bytes(workspace_id)

    def test_purge_removes_the_bytes(self, actor, user, workspace_id):
        resp = actor.post(
            f"{API}/documents",
            {
                "workspace_id": str(workspace_id),
                "type": "md",
                "title": "Doomed",
                "body": "# bytes",
            },
            format="json",
        )
        actor.delete(f"{API}/documents/{resp.json()['id']}")
        actor.post(
            f"{API}/trash/empty", {"workspace_id": str(workspace_id)}, format="json"
        )
        assert _usage(user, workspace_id)["bytes_total"] == 0

    def test_other_workspaces_are_invisible(self, user, workspace_id,
                                            grant_capabilities):
        grant_capabilities(workspace_id, user.pk)
        other = uuid.uuid4()
        Folder.objects.create(workspace_id=other, name="Theirs")
        Document.objects.create(
            workspace_id=other, type="md", title="Theirs", size_bytes=999
        )
        usage = _usage(user, workspace_id)
        assert usage == {
            "bytes_live": 0,
            "bytes_trash": 0,
            "bytes_total": 0,
            "documents": 0,
            "folders": 0,
            "by_type": {},
        }


class TestCallerAuthority:
    """Same gate as docs.create_document, one capability lower (view)."""

    def test_a_nameless_caller_is_refused(self, workspace_id):
        with pytest.raises(FunctionCallError, match="CallerNotAuthorized"):
            call("docs.usage", {"workspace_id": str(workspace_id)})

    def test_an_actor_without_view_is_refused(self, user, workspace_id,
                                              grant_capabilities):
        grant_capabilities(workspace_id, user.pk, "docs.nothing")
        with pytest.raises(FunctionCallError, match="CallerNotAuthorized"):
            _usage(user, workspace_id)

    def test_view_alone_is_enough(self, user, workspace_id, grant_capabilities):
        grant_capabilities(workspace_id, user.pk, "docs.view")
        assert _usage(user, workspace_id)["documents"] == 0

    def test_a_trusted_service_may_meter_without_an_actor(self, workspace_id):
        with override_settings(
            STAPEL_DOCS={"INTERNAL_TRUSTED_SERVICES": ["billing"]}
        ):
            usage = call(
                "docs.usage",
                {"workspace_id": str(workspace_id), "caller_service": "billing"},
            )
        assert usage["bytes_total"] == 0

    def test_an_untrusted_service_is_refused(self, workspace_id):
        with override_settings(
            STAPEL_DOCS={"INTERNAL_TRUSTED_SERVICES": ["billing"]}
        ):
            with pytest.raises(FunctionCallError, match="CallerNotAuthorized"):
                call(
                    "docs.usage",
                    {"workspace_id": str(workspace_id), "caller_service": "stranger"},
                )
