"""The sharing axis — grant sources over the immutable workspace baseline.

This is the module's security surface, so the tests are written the way an
attacker reads the feature rather than the way the happy path demos it:
every gate is probed from the side that must be refused, and the ones that
can fail OPEN (an outage, a dead sponsor, a disabled mode, an anonymous
writer) get a test each stating what the refusal must look like.

The axis (tasks/sharing-axis-design.md) in five sentences, each of which
has tests below: the baseline is immutable; sources only ever grant;
``manage`` is never granted; an anonymous presenter never writes; and an
outage is answered 503, never 403.
"""
import uuid
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_docs import services
from stapel_docs.authz import (
    ALLOW,
    DENY,
    UNAVAILABLE,
    Principal,
    authorize,
    granted_level,
)
from stapel_docs.models import DocumentAccess, DocumentLink

pytestmark = pytest.mark.django_db

API = "/docs/api/v1"

WHITELIST_ON = {"SHARING": {"MODES": ["whitelist"]}}
LINK_ON = {"SHARING": {"MODES": ["link"]}}
BOTH_ON = {"SHARING": {"MODES": ["whitelist", "link"]}}


# ── resolvers registered by the RESOLVERS seam ───────────────────────
#
# Host code, deliberately: docs never ships a resolver (axis §11.3), so the
# ones under test are written here exactly as a product would write them.

_RESOLVER_CALLS = []


def allow_resolver(ref, user_id):
    _RESOLVER_CALLS.append((ref, str(user_id)))
    return True


def deny_resolver(ref, user_id):
    _RESOLVER_CALLS.append((ref, str(user_id)))
    return False


def exploding_resolver(ref, user_id):
    _RESOLVER_CALLS.append((ref, str(user_id)))
    raise RuntimeError("the chat service fell over")


# Addressed through ``__name__`` rather than a written-out dotted path: the
# test module is importable under two names (``tests.test_sharing`` from the
# runner, ``stapel_docs.tests.test_sharing`` from the package), and two module
# objects means the resolver records its calls into a list this file cannot
# see. The same trap awaits any host that registers a resolver from a module
# its runner imports twice.
_ALLOW = f"{__name__}.allow_resolver"
_DENY = f"{__name__}.deny_resolver"
_BOOM = f"{__name__}.exploding_resolver"


def _with_resolvers(modes, resolvers):
    return {"SHARING": {"MODES": modes, "RESOLVERS": resolvers}}


@pytest.fixture(autouse=True)
def _axis(request):
    """Apply the class's ``AXIS`` overlay, if it declares one.

    ``override_settings`` refuses to decorate a plain class (it wants a
    ``SimpleTestCase``), so a class states the axis it is written under as
    an attribute — which reads at the top of the class exactly where the
    decorator would have been, and leaves every method free to narrow it.
    """
    config = getattr(request.cls, "AXIS", None)
    if config is None:
        yield
        return
    with override_settings(STAPEL_DOCS=config):
        yield


@pytest.fixture(autouse=True)
def _reset_resolver_calls():
    _RESOLVER_CALLS.clear()
    yield
    _RESOLVER_CALLS.clear()


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


@pytest.fixture
def workspace_id():
    return uuid.uuid4()


def _user(name):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username=f"{name}-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def owner(db):
    """A workspace admin: holds every docs capability, sharing included."""
    return _user("owner")


@pytest.fixture
def guest(db):
    """A real account with NO membership of the workspace — the relational
    guest the axis exists for ("member of org A, guest of org B's doc")."""
    return _user("guest")


@pytest.fixture
def visitor():
    """A SECOND HTTP client, never authenticated as the owner.

    ``actor`` authenticates the shared ``api_client``, so a test that
    switched that one client to the guest would silently un-authenticate
    the admin half of the same test — which is how a share-sheet assertion
    can pass for the wrong reason.
    """
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def actor(api_client, owner, grant_capabilities, workspace_id):
    api_client.force_authenticate(user=owner)
    grant_capabilities(workspace_id, owner.pk)
    return api_client


@pytest.fixture
def document(actor, workspace_id):
    resp = actor.post(
        f"{API}/documents",
        {"workspace_id": str(workspace_id), "type": "md", "title": "Notes", "body": "# hi"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    from stapel_docs.models import Document

    return Document.objects.get(pk=resp.json()["id"])


def _principal(user=None, *, token=None, anonymous=False):
    return Principal(
        user_id=user.pk if user is not None else None,
        is_anonymous=anonymous,
        link_token=token,
    )


def _grant(document, user, level="view", granted_by=None):
    return DocumentAccess.objects.create(
        document=document,
        workspace_id=document.workspace_id,
        subject_kind=DocumentAccess.SUBJECT_USER,
        user_id=user.pk,
        level=level,
        granted_by=granted_by,
    )


def _link(document, creator, *, level="view", **overrides):
    from secrets import token_urlsafe

    fields = dict(
        document=document,
        workspace_id=document.workspace_id,
        token=token_urlsafe(32),
        level=level,
        created_by=creator,
        expires_at=timezone.now() + timedelta(days=30),
    )
    fields.update(overrides)
    return DocumentLink.objects.create(**fields)


# ─────────────────────────────────────────────────────────────────────
# The baseline is immutable
# ─────────────────────────────────────────────────────────────────────


class TestBaselineImmutability:
    def test_a_member_works_with_the_axis_fully_closed(self, owner, document):
        """The shipped default (MODES=[]) is not a degraded mode: the whole
        workspace surface works, because sharing was only ever additive."""
        assert authorize(
            workspace_id=document.workspace_id,
            principal=_principal(owner),
            action="view",
            document=document,
        ) == ALLOW

    def test_a_closed_axis_ignores_rows_that_exist(self, guest, owner, document):
        _grant(document, guest, "edit")
        assert authorize(
            workspace_id=document.workspace_id,
            principal=_principal(guest),
            action="view",
            document=document,
        ) == DENY

    @override_settings(STAPEL_DOCS=BOTH_ON)
    def test_no_grant_ever_reaches_manage(self, guest, owner, document):
        """The anti-escalation invariant (§2.2), from both sources at once:
        a guest may read and even write the body and still cannot delete,
        move or re-share the object."""
        _grant(document, guest, "edit")
        link = _link(document, owner, level="edit")
        for principal in (
            _principal(guest),
            _principal(guest, token=link.token),
        ):
            assert authorize(
                workspace_id=document.workspace_id,
                principal=principal,
                action="manage",
                document=document,
            ) == DENY

    @override_settings(STAPEL_DOCS=WHITELIST_ON)
    def test_a_grant_never_widens_a_workspace_listing(self, visitor, guest, document):
        """A grant is about ONE document: it must not turn into a key to the
        workspace's tree, which is what an axis read without a document in
        hand would do."""
        _grant(document, guest, "edit")
        visitor.force_authenticate(user=guest)
        resp = visitor.get(
            f"{API}/documents", {"workspace_id": str(document.workspace_id)}
        )
        assert resp.status_code == 403

    @override_settings(STAPEL_DOCS=WHITELIST_ON)
    def test_max_level_wins_between_sources(self, owner, guest, document, grant_capabilities):
        """A workspace VIEWER with an edit grant edits that one document —
        the deliberate Google-Docs semantics of §3 (max of the levels each
        independent source allows), not an escalation bug."""
        grant_capabilities(document.workspace_id, guest.pk, "docs.view")
        assert authorize(
            workspace_id=document.workspace_id,
            principal=_principal(guest),
            action="edit",
            document=document,
        ) == DENY
        _grant(document, guest, "edit")
        assert authorize(
            workspace_id=document.workspace_id,
            principal=_principal(guest),
            action="edit",
            document=document,
        ) == ALLOW


# ─────────────────────────────────────────────────────────────────────
# Kill-switch: rows go inert, and SAY so
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitch:
    def test_disabling_a_mode_denies_but_keeps_the_rows(self, guest, document):
        with override_settings(STAPEL_DOCS=WHITELIST_ON):
            _grant(document, guest, "view")
            assert authorize(
                workspace_id=document.workspace_id,
                principal=_principal(guest),
                action="view",
                document=document,
            ) == ALLOW
        assert authorize(
            workspace_id=document.workspace_id,
            principal=_principal(guest),
            action="view",
            document=document,
        ) == DENY
        assert DocumentAccess.objects.filter(document=document).count() == 1

    def test_the_share_sheet_shows_inert_rows_as_suspended(self, actor, guest, document):
        """Never hidden (§3): an admin who cannot see an inert grant reads
        it as revoked and is surprised when re-enabling the mode restores
        access nobody remembers giving."""
        _grant(document, guest, "view")
        _link(document, None)
        rows = actor.get(f"{API}/documents/{document.id}/access").json()
        links = actor.get(f"{API}/documents/{document.id}/links").json()
        assert [r["suspended"] for r in rows] == [True]
        assert [r["suspended"] for r in links] == [True]

    @override_settings(STAPEL_DOCS=BOTH_ON)
    def test_an_enabled_mode_reports_no_suspension(self, actor, guest, document, owner):
        _grant(document, guest, "view")
        _link(document, owner)
        rows = actor.get(f"{API}/documents/{document.id}/access").json()
        links = actor.get(f"{API}/documents/{document.id}/links").json()
        assert [r["suspended"] for r in rows] == [False]
        assert [r["suspended"] for r in links] == [False]

    def test_minting_into_a_disabled_mode_is_refused(self, actor, guest, document):
        """A row nothing will ever read is worse than a refusal: the admin
        sees a grant in the sheet and the guest sees a 403."""
        resp = actor.post(
            f"{API}/documents/{document.id}/access",
            {"subject_kind": "user", "user_id": str(guest.pk), "level": "view"},
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_share_mode_disabled"

    def test_revoking_works_while_the_mode_is_off(self, actor, guest, document):
        """Taking access away must never be the thing nobody is allowed to
        do — least of all right after an operator switched the mode off."""
        row = _grant(document, guest, "view")
        resp = actor.delete(f"{API}/documents/{document.id}/access/{row.id}")
        assert resp.status_code == 204
        assert not DocumentAccess.objects.filter(pk=row.pk).exists()


# ─────────────────────────────────────────────────────────────────────
# Whitelist, subject = user
# ─────────────────────────────────────────────────────────────────────


class TestWhitelistUserSubject:
    AXIS = WHITELIST_ON

    def test_view_grant_reads_but_does_not_write(self, guest, document):
        _grant(document, guest, "view")
        principal = _principal(guest)
        assert authorize(
            workspace_id=document.workspace_id, principal=principal,
            action="view", document=document,
        ) == ALLOW
        assert authorize(
            workspace_id=document.workspace_id, principal=principal,
            action="edit", document=document,
        ) == DENY

    def test_edit_grant_writes(self, guest, document):
        _grant(document, guest, "edit")
        assert authorize(
            workspace_id=document.workspace_id, principal=_principal(guest),
            action="edit", document=document,
        ) == ALLOW

    def test_a_grant_on_one_document_is_not_a_grant_on_another(
        self, actor, guest, document, workspace_id
    ):
        other = actor.post(
            f"{API}/documents",
            {"workspace_id": str(workspace_id), "type": "md", "title": "Other"},
            format="json",
        ).json()
        from stapel_docs.models import Document

        _grant(document, guest, "edit")
        assert authorize(
            workspace_id=workspace_id, principal=_principal(guest),
            action="view", document=Document.objects.get(pk=other["id"]),
        ) == DENY

    def test_an_anonymous_account_is_never_whitelisted(self, guest, document):
        """A whitelist names people; an anonymous account is not one of them
        even when a row happens to carry its id (§2.1)."""
        _grant(document, guest, "edit")
        assert authorize(
            workspace_id=document.workspace_id,
            principal=_principal(guest, anonymous=True),
            action="view",
            document=document,
        ) == DENY

    def test_http_grant_and_revoke_round_trip(self, actor, guest, document):
        created = actor.post(
            f"{API}/documents/{document.id}/access",
            {"subject_kind": "user", "user_id": str(guest.pk), "level": "view"},
            format="json",
        )
        assert created.status_code == 201, created.content
        body = created.json()
        assert body["subject"] == str(guest.pk)
        assert body["level"] == "view"

        raised = actor.post(
            f"{API}/documents/{document.id}/access",
            {"subject_kind": "user", "user_id": str(guest.pk), "level": "edit"},
            format="json",
        )
        assert raised.status_code == 201
        assert DocumentAccess.objects.filter(document=document).count() == 1
        assert raised.json()["level"] == "edit"

        gone = actor.delete(f"{API}/documents/{document.id}/access/{body['id']}")
        assert gone.status_code == 204

    def test_minting_needs_the_share_capability(
        self, visitor, guest, document, grant_capabilities
    ):
        """A member who may EDIT the document still may not widen access to
        it: the mandate to share is separate from every level (§4)."""
        grant_capabilities(document.workspace_id, guest.pk, "docs.view", "docs.edit")
        visitor.force_authenticate(user=guest)
        resp = visitor.post(
            f"{API}/documents/{document.id}/access",
            {"subject_kind": "user", "user_id": str(guest.pk), "level": "view"},
            format="json",
        )
        assert resp.status_code == 403

    def test_cannot_grant_a_level_you_do_not_hold(
        self, visitor, owner, guest, document, grant_capabilities
    ):
        """A view-only sharer cannot mint editors (§2.1: the row's level is
        capped by the granter's own at the moment of issue)."""
        sharer = _user("sharer")
        grant_capabilities(
            document.workspace_id, sharer.pk, "docs.view", "docs.share.whitelist"
        )
        visitor.force_authenticate(user=sharer)
        assert visitor.post(
            f"{API}/documents/{document.id}/access",
            {"subject_kind": "user", "user_id": str(guest.pk), "level": "edit"},
            format="json",
        ).status_code == 403
        assert visitor.post(
            f"{API}/documents/{document.id}/access",
            {"subject_kind": "user", "user_id": str(guest.pk), "level": "view"},
            format="json",
        ).status_code == 201

    def test_a_half_named_subject_is_refused(self, actor, document):
        for payload in (
            {"subject_kind": "user"},
            {"subject_kind": "user", "ref": "chat:conversation:1"},
            {"subject_kind": "ref"},
        ):
            resp = actor.post(
                f"{API}/documents/{document.id}/access", payload, format="json"
            )
            assert resp.status_code == 400, payload


# ─────────────────────────────────────────────────────────────────────
# Whitelist, subject = ref (the chat case, through the resolver seam)
# ─────────────────────────────────────────────────────────────────────


class TestWhitelistRefSubject:
    def _ref_row(self, document, ref="chat:conversation:42"):
        return DocumentAccess.objects.create(
            document=document,
            workspace_id=document.workspace_id,
            subject_kind=DocumentAccess.SUBJECT_REF,
            ref=ref,
            level="view",
        )

    def test_a_resolver_that_says_yes_admits(self, guest, document):
        self._ref_row(document)
        with override_settings(
            STAPEL_DOCS=_with_resolvers(["whitelist"], {"chat:conversation": _ALLOW})
        ):
            assert authorize(
                workspace_id=document.workspace_id, principal=_principal(guest),
                action="view", document=document,
            ) == ALLOW
        assert _RESOLVER_CALLS == [("chat:conversation:42", str(guest.pk))]

    def test_a_resolver_that_says_no_denies(self, guest, document):
        self._ref_row(document)
        with override_settings(
            STAPEL_DOCS=_with_resolvers(["whitelist"], {"chat:conversation": _DENY})
        ):
            assert authorize(
                workspace_id=document.workspace_id, principal=_principal(guest),
                action="view", document=document,
            ) == DENY

    def test_a_raising_resolver_denies_rather_than_opens(self, guest, document):
        """Fail-closed on the read boundary: an outage in somebody else's
        service must not be a key to this document."""
        self._ref_row(document)
        with override_settings(
            STAPEL_DOCS=_with_resolvers(["whitelist"], {"chat:conversation": _BOOM})
        ):
            assert authorize(
                workspace_id=document.workspace_id, principal=_principal(guest),
                action="view", document=document,
            ) == DENY

    def test_an_unregistered_kind_denies(self, guest, document):
        self._ref_row(document)
        with override_settings(STAPEL_DOCS=_with_resolvers(["whitelist"], {})):
            assert authorize(
                workspace_id=document.workspace_id, principal=_principal(guest),
                action="view", document=document,
            ) == DENY

    def test_an_unimportable_resolver_denies(self, guest, document):
        self._ref_row(document)
        with override_settings(
            STAPEL_DOCS=_with_resolvers(
                ["whitelist"], {"chat:conversation": "nope.missing.resolver"}
            )
        ):
            assert authorize(
                workspace_id=document.workspace_id, principal=_principal(guest),
                action="view", document=document,
            ) == DENY

    def test_the_answer_is_cached_briefly(self, guest, document):
        """One point-query per ~30 s per (ref, user): without the cache the
        resolver is called at request rate; with a COPY of the membership it
        would never be called again. The cache is the middle the axis picked."""
        self._ref_row(document)
        with override_settings(
            STAPEL_DOCS=_with_resolvers(["whitelist"], {"chat:conversation": _ALLOW})
        ):
            for _ in range(3):
                assert authorize(
                    workspace_id=document.workspace_id, principal=_principal(guest),
                    action="view", document=document,
                ) == ALLOW
        assert len(_RESOLVER_CALLS) == 1

    def test_a_refusal_is_cached_too_but_an_exception_is_not(self, guest, document):
        self._ref_row(document)
        with override_settings(
            STAPEL_DOCS=_with_resolvers(["whitelist"], {"chat:conversation": _DENY})
        ):
            for _ in range(3):
                authorize(
                    workspace_id=document.workspace_id, principal=_principal(guest),
                    action="view", document=document,
                )
        assert len(_RESOLVER_CALLS) == 1
        _RESOLVER_CALLS.clear()
        # The cache is keyed by (ref, user), not by which resolver answered —
        # so the stored refusal above would satisfy the next three calls too.
        # Clearing it is what puts the exploding resolver on the hot path.
        from django.core.cache import cache

        cache.clear()
        with override_settings(
            STAPEL_DOCS=_with_resolvers(["whitelist"], {"chat:conversation": _BOOM})
        ):
            for _ in range(3):
                authorize(
                    workspace_id=document.workspace_id, principal=_principal(guest),
                    action="view", document=document,
                )
        assert len(_RESOLVER_CALLS) == 3

    def test_minting_a_ref_with_no_resolver_is_refused(self, actor, document):
        """Fail-closed on the WRITE boundary too (§11.3): a row that could
        only ever deny never gets stored."""
        with override_settings(STAPEL_DOCS=_with_resolvers(["whitelist"], {})):
            resp = actor.post(
                f"{API}/documents/{document.id}/access",
                {"subject_kind": "ref", "ref": "chat:conversation:42"},
                format="json",
            )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_share_ref_kind"

    def test_minting_a_ref_with_a_resolver_works(self, actor, document):
        with override_settings(
            STAPEL_DOCS=_with_resolvers(["whitelist"], {"chat:conversation": _ALLOW})
        ):
            resp = actor.post(
                f"{API}/documents/{document.id}/access",
                {"subject_kind": "ref", "ref": "chat:conversation:42"},
                format="json",
            )
        assert resp.status_code == 201, resp.content
        assert resp.json()["subject"] == "chat:conversation:42"


# ─────────────────────────────────────────────────────────────────────
# Link mode — where things leak, so one test per way
# ─────────────────────────────────────────────────────────────────────


class TestLinkRedemption:
    AXIS = LINK_ON

    def test_an_authenticated_non_member_reads_by_link(
        self, visitor, owner, guest, document
    ):
        link = _link(document, owner)
        visitor.force_authenticate(user=guest)
        resp = visitor.get(f"{API}/shared/{link.token}")
        assert resp.status_code == 200, resp.content
        assert resp.json()["title"] == "Notes"

    def test_the_bearer_envelope_is_stripped(self, visitor, owner, guest, document):
        """A link grants a document, not a seat (§6): nothing around it —
        no workspace, no folder, no owner, no star state, no history."""
        link = _link(document, owner)
        visitor.force_authenticate(user=guest)
        body = visitor.get(f"{API}/shared/{link.token}").json()
        assert set(body) == {
            "id", "type", "title", "head_seq", "size_bytes", "mime_type",
            "editor_hint", "collab", "diffable", "level", "updated_at",
        }
        assert body["level"] == "view"

    def test_the_bearer_reads_content(self, visitor, owner, guest, document):
        link = _link(document, owner)
        visitor.force_authenticate(user=guest)
        resp = visitor.get(f"{API}/shared/{link.token}/content")
        assert resp.status_code == 200
        assert resp.content == b"# hi"

    def test_the_bearer_path_leaves_no_recents(self, visitor, owner, guest, document):
        from stapel_docs.models import RecentEntry

        link = _link(document, owner)
        visitor.force_authenticate(user=guest)
        visitor.get(f"{API}/shared/{link.token}/content")
        assert not RecentEntry.objects.filter(user_id=guest.pk).exists()

    def test_an_expired_link_is_dead(self, visitor, owner, guest, document):
        link = _link(document, owner, expires_at=timezone.now() - timedelta(seconds=1))
        assert link.status == "expired"
        visitor.force_authenticate(user=guest)
        assert visitor.get(f"{API}/shared/{link.token}").status_code == 404

    def test_a_revoked_link_is_dead_and_revoked_beats_the_ttl(
        self, visitor, owner, guest, document
    ):
        link = _link(
            document,
            owner,
            revoked_at=timezone.now(),
            expires_at=timezone.now() - timedelta(days=1),
        )
        assert link.status == "revoked"
        visitor.force_authenticate(user=guest)
        assert visitor.get(f"{API}/shared/{link.token}").status_code == 404

    def test_a_dead_token_answers_like_an_unknown_one(
        self, visitor, owner, guest, document
    ):
        """No oracle: "that token was real once" is information a guesser
        can grind, so both refusals are the same 404."""
        link = _link(document, owner, revoked_at=timezone.now())
        visitor.force_authenticate(user=guest)
        dead = visitor.get(f"{API}/shared/{link.token}")
        unknown = visitor.get(f"{API}/shared/{'z' * 43}")
        assert dead.status_code == unknown.status_code == 404
        assert dead.json() == unknown.json()

    def test_a_token_cannot_reach_another_workspaces_document(
        self, actor, visitor, owner, guest, document, grant_capabilities
    ):
        """The token addresses ONE document; the URL carries nothing else,
        so there is no id to substitute — and the link row for one document
        never resolves to another."""
        other_ws = uuid.uuid4()
        grant_capabilities(other_ws, owner.pk)
        other = actor.post(
            f"{API}/documents",
            {"workspace_id": str(other_ws), "type": "md", "title": "Secret"},
            format="json",
        ).json()
        link = _link(document, owner)
        visitor.force_authenticate(user=guest)
        assert visitor.get(f"{API}/shared/{link.token}").json()["id"] == str(document.id)
        assert visitor.get(f"{API}/documents/{other['id']}").status_code == 403

    def test_the_creator_losing_the_capability_kills_the_link(
        self, visitor, owner, guest, document, grant_capabilities
    ):
        """The asymmetry with whitelist (§6): a whitelist row is enumerable
        and an admin can strike it, but a bearer token in unknown hands whose
        sponsor has left is the leak itself, so it dies on its own."""
        link = _link(document, owner)
        visitor.force_authenticate(user=guest)
        assert visitor.get(f"{API}/shared/{link.token}").status_code == 200
        grant_capabilities(document.workspace_id, owner.pk, "docs.view")
        from django.core.cache import cache

        cache.clear()
        assert visitor.get(f"{API}/shared/{link.token}").status_code == 404

    def test_a_link_whose_creator_is_gone_is_dead(
        self, visitor, owner, guest, document
    ):
        link = _link(document, None)
        visitor.force_authenticate(user=guest)
        assert visitor.get(f"{API}/shared/{link.token}").status_code == 404

    def test_a_workspaces_outage_during_the_creator_check_is_503(
        self, visitor, owner, guest, document, monkeypatch
    ):
        """An outage is not a verdict. 403 here would tell the holder of a
        good link that it was withdrawn, and the operator watching would have
        nothing to tell the two apart."""
        from stapel_core.django import workspaces as ws_client

        real = ws_client.require_capability

        def flaky(workspace_id, user_id, capability, **kwargs):
            if str(user_id) == str(owner.pk) and capability == "docs.share.link":
                raise ws_client.WorkspaceLookupUnavailable("peer down")
            return real(workspace_id, user_id, capability, **kwargs)

        monkeypatch.setattr(ws_client, "require_capability", flaky)
        link = _link(document, owner)
        visitor.force_authenticate(user=guest)
        resp = visitor.get(f"{API}/shared/{link.token}")
        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == "error.503.docs_workspaces_unavailable"

    def test_first_redeemed_at_is_stamped_exactly_once(
        self, visitor, owner, guest, document
    ):
        link = _link(document, owner)
        visitor.force_authenticate(user=guest)
        visitor.get(f"{API}/shared/{link.token}")
        link.refresh_from_db()
        first = link.first_redeemed_at
        assert first is not None
        visitor.get(f"{API}/shared/{link.token}")
        link.refresh_from_db()
        assert link.first_redeemed_at == first

    def test_a_refused_presentation_stamps_nothing(
        self, visitor, owner, guest, document
    ):
        """The stamp is evidence somebody got IN — a refused presentation
        that wrote it would forge the audit trail."""
        link = _link(document, None)
        visitor.force_authenticate(user=guest)
        visitor.get(f"{API}/shared/{link.token}")
        link.refresh_from_db()
        assert link.first_redeemed_at is None


class TestLinkAnonymity:
    @override_settings(STAPEL_DOCS=LINK_ON)
    def test_an_unauthenticated_bearer_is_401_by_default(
        self, visitor, owner, document
    ):
        """401, not 403: "sign in" and "this is not yours" are different
        facts, and only the first tells the holder of a good link what to do."""
        link = _link(document, owner)
        resp = visitor.get(f"{API}/shared/{link.token}")
        assert resp.status_code == 401
        assert resp.json()["localizable_error"] == "error.401.docs_share_auth_required"

    @override_settings(
        STAPEL_DOCS={"SHARING": {"MODES": ["link"], "LINK": {"ANONYMOUS": True}}}
    )
    def test_anonymous_redemption_reads_when_the_host_opens_it(
        self, visitor, owner, document
    ):
        link = _link(document, owner)
        resp = visitor.get(f"{API}/shared/{link.token}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Notes"

    @override_settings(
        STAPEL_DOCS={
            "SHARING": {
                "MODES": ["link"],
                "LINK": {"ANONYMOUS": True, "MAX_LEVEL": "edit"},
            }
        }
    )
    def test_an_anonymous_bearer_never_writes_even_at_edit_level(
        self, owner, guest, document
    ):
        """The one combination the axis forbids FOREVER (§6/§11.2): the
        journal and the revision history are attributed by design, so an
        edit-level link plus an anonymous holder is authorless vandalism,
        not a feature behind a flag. Both anonymities are refused — no
        session at all, and an anonymous ACCOUNT of the auth axis."""
        link = _link(document, owner, level="edit")
        for principal in (
            _principal(None, token=link.token),
            _principal(guest, token=link.token, anonymous=True),
        ):
            assert authorize(
                workspace_id=document.workspace_id, principal=principal,
                action="edit", document=document,
            ) == DENY
        assert authorize(
            workspace_id=document.workspace_id,
            principal=_principal(guest, token=link.token),
            action="edit",
            document=document,
        ) == ALLOW


class TestLinkMinting:
    @override_settings(STAPEL_DOCS=LINK_ON)
    def test_mint_list_and_revoke(self, actor, document):
        created = actor.post(
            f"{API}/documents/{document.id}/links", {}, format="json"
        )
        assert created.status_code == 201, created.content
        body = created.json()
        assert body["level"] == "view"
        assert body["status"] == "active"
        assert body["token"]
        assert body["expires_at"]

        listed = actor.get(f"{API}/documents/{document.id}/links").json()
        assert [row["id"] for row in listed] == [body["id"]]

        gone = actor.delete(f"{API}/documents/{document.id}/links/{body['id']}")
        assert gone.status_code == 204
        link = DocumentLink.objects.get(pk=body["id"])
        assert link.status == "revoked"

    @override_settings(STAPEL_DOCS=LINK_ON)
    def test_a_level_above_the_ceiling_is_refused_not_clamped(self, actor, document):
        """Refused loudly: a client that asked for edit and silently got view
        shows the wrong thing to the person it hands the link to."""
        resp = actor.post(
            f"{API}/documents/{document.id}/links", {"level": "edit"}, format="json"
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_share_level"

    @override_settings(
        STAPEL_DOCS={"SHARING": {"MODES": ["link"], "LINK": {"MAX_LEVEL": "edit"}}}
    )
    def test_a_raised_ceiling_admits_the_level(self, actor, document):
        resp = actor.post(
            f"{API}/documents/{document.id}/links", {"level": "edit"}, format="json"
        )
        assert resp.status_code == 201
        assert resp.json()["level"] == "edit"

    @override_settings(STAPEL_DOCS=LINK_ON)
    def test_minting_needs_the_link_capability(
        self, visitor, guest, document, grant_capabilities
    ):
        grant_capabilities(document.workspace_id, guest.pk, "docs.view", "docs.edit")
        visitor.force_authenticate(user=guest)
        assert visitor.post(
            f"{API}/documents/{document.id}/links", {}, format="json"
        ).status_code == 403
        assert visitor.get(
            f"{API}/documents/{document.id}/links"
        ).status_code == 403

    @override_settings(STAPEL_DOCS=LINK_ON)
    def test_the_ttl_is_finite_by_default(self, actor, document):
        body = actor.post(f"{API}/documents/{document.id}/links", {}, format="json").json()
        link = DocumentLink.objects.get(pk=body["id"])
        assert timezone.now() < link.expires_at < timezone.now() + timedelta(days=31)

    @override_settings(
        STAPEL_DOCS={"SHARING": {"MODES": ["link"], "LINK": {"TTL_DAYS": None}}}
    )
    def test_a_perpetual_ttl_is_a_date_not_a_null(self, actor, document):
        """The column is NOT NULL by the invitation canon; "perpetual" is
        said as a deadline every reader can render."""
        body = actor.post(f"{API}/documents/{document.id}/links", {}, format="json").json()
        link = DocumentLink.objects.get(pk=body["id"])
        assert link.expires_at > timezone.now() + timedelta(days=365 * 50)

    @override_settings(STAPEL_DOCS=LINK_ON)
    def test_revoking_is_idempotent(self, actor, owner, document):
        link = _link(document, owner)
        first = actor.delete(f"{API}/documents/{document.id}/links/{link.id}")
        link.refresh_from_db()
        stamped = link.revoked_at
        second = actor.delete(f"{API}/documents/{document.id}/links/{link.id}")
        link.refresh_from_db()
        assert first.status_code == second.status_code == 204
        assert link.revoked_at == stamped

    @override_settings(STAPEL_DOCS=LINK_ON)
    def test_a_link_id_from_another_document_is_404(self, actor, owner, document, workspace_id):
        other = actor.post(
            f"{API}/documents",
            {"workspace_id": str(workspace_id), "type": "md", "title": "Other"},
            format="json",
        ).json()
        link = _link(document, owner)
        resp = actor.delete(f"{API}/documents/{other['id']}/links/{link.id}")
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# Emits (validated against schemas/emits/ — VALIDATE_SCHEMAS is on)
# ─────────────────────────────────────────────────────────────────────


class TestShareEmits:
    AXIS = BOTH_ON

    def _capture(self):
        from stapel_core.comm import subscribe_action

        seen = []
        for name in (
            "document.share.granted",
            "document.share.revoked",
            "document.share.link_created",
            "document.share.link_revoked",
            "document.share.link_redeemed",
        ):
            subscribe_action(name, lambda event: seen.append((event.event_type, event.payload)))
        return seen

    def test_the_whole_family_fires_and_validates(
        self, visitor, actor, owner, guest, document
    ):
        seen = self._capture()
        granted = actor.post(
            f"{API}/documents/{document.id}/access",
            {"subject_kind": "user", "user_id": str(guest.pk)},
            format="json",
        ).json()
        minted = actor.post(
            f"{API}/documents/{document.id}/links", {}, format="json"
        ).json()
        visitor.force_authenticate(user=guest)
        visitor.get(f"{API}/shared/{minted['token']}")
        actor.delete(f"{API}/documents/{document.id}/links/{minted['id']}")
        actor.delete(f"{API}/documents/{document.id}/access/{granted['id']}")

        assert [name for name, _ in seen] == [
            "document.share.granted",
            "document.share.link_created",
            "document.share.link_redeemed",
            "document.share.link_revoked",
            "document.share.revoked",
        ]

    def test_no_emitted_payload_ever_carries_a_token(self, actor, document):
        """An event is copied into an outbox, a broker and somebody's
        dashboard: a bearer secret that travels that far has been leaked by
        its own audit trail."""
        seen = self._capture()
        minted = actor.post(
            f"{API}/documents/{document.id}/links", {}, format="json"
        ).json()
        payloads = [payload for _, payload in seen]
        assert payloads
        for payload in payloads:
            assert "token" not in payload
            assert minted["token"] not in str(payload)


# ─────────────────────────────────────────────────────────────────────
# Erasure and merge
# ─────────────────────────────────────────────────────────────────────


class TestErasure:
    AXIS = BOTH_ON

    def test_account_erasure_deletes_grants_revokes_links_anonymizes_provenance(
        self, owner, guest, document
    ):
        from stapel_docs.erasure import erase_account

        _grant(document, guest, "view", granted_by=owner)
        link = _link(document, owner)

        counts = erase_account(guest.pk)
        assert counts["access_deleted"] == 1
        assert not DocumentAccess.objects.filter(user_id=guest.pk).exists()

        counts = erase_account(owner.pk)
        assert counts["links_revoked"] == 1
        assert counts["access_anonymized"] == 0  # the row it granted is gone
        link.refresh_from_db()
        assert link.status == "revoked"
        assert link.created_by_id is None

    def test_erasure_is_idempotent(self, owner, guest, document):
        from stapel_docs.erasure import erase_account

        _grant(document, guest, "view", granted_by=owner)
        _link(document, owner)
        erase_account(owner.pk)
        second = erase_account(owner.pk)
        assert second["links_revoked"] == 0
        assert second["links_anonymized"] == 0

    def test_a_surviving_grant_keeps_an_anonymized_granter(self, owner, guest, document):
        from stapel_docs.erasure import erase_account

        row = _grant(document, guest, "view", granted_by=owner)
        counts = erase_account(owner.pk)
        assert counts["access_anonymized"] == 1
        row.refresh_from_db()
        assert row.granted_by_id is None
        assert row.level == "view"

    def test_document_purge_cascades_both_tables_and_counts_them(
        self, owner, guest, document
    ):
        from stapel_docs.erasure import erase_document

        _grant(document, guest, "view")
        _link(document, owner)
        counts = erase_document(document.id)
        assert counts["access_grants"] == 1
        assert counts["links"] == 1
        assert not DocumentAccess.objects.exists()
        assert not DocumentLink.objects.exists()

    def test_workspace_erasure_takes_the_sharing_rows_with_it(
        self, owner, guest, document
    ):
        from stapel_docs.erasure import erase_workspace

        _grant(document, guest, "view")
        _link(document, owner)
        erase_workspace(document.workspace_id)
        assert not DocumentAccess.objects.exists()
        assert not DocumentLink.objects.exists()


class TestMerge:
    AXIS = BOTH_ON

    def _merge(self, from_user, into_user):
        from types import SimpleNamespace

        from stapel_docs.actions import handle_user_merged

        handle_user_merged(
            SimpleNamespace(
                event_id="e1",
                payload={
                    "from_user_id": str(from_user.pk),
                    "into_user_id": str(into_user.pk),
                },
            )
        )

    def test_a_merged_guest_keeps_their_access(self, owner, guest, document):
        survivor = _user("survivor")
        _grant(document, guest, "view")
        self._merge(guest, survivor)
        assert DocumentAccess.objects.filter(
            document=document, user_id=survivor.pk, level="view"
        ).exists()
        assert not DocumentAccess.objects.filter(user_id=guest.pk).exists()

    def test_a_collision_keeps_the_higher_level(self, owner, guest, document):
        """Folding down would revoke access as a side effect of a merge —
        a silent loss on the one table where losing access is hardest to
        diagnose."""
        survivor = _user("survivor")
        _grant(document, guest, "edit")
        _grant(document, survivor, "view")
        self._merge(guest, survivor)
        rows = DocumentAccess.objects.filter(document=document)
        assert rows.count() == 1
        assert rows.first().level == "edit"

    def test_provenance_and_links_are_reparented(self, owner, guest, document):
        survivor = _user("survivor")
        row = _grant(document, guest, "view", granted_by=owner)
        link = _link(document, owner)
        self._merge(owner, survivor)
        row.refresh_from_db()
        link.refresh_from_db()
        assert row.granted_by_id == survivor.pk
        assert link.created_by_id == survivor.pk


# ─────────────────────────────────────────────────────────────────────
# Model invariants stated to the database
# ─────────────────────────────────────────────────────────────────────


class TestModelInvariants:
    def test_a_row_with_two_subjects_is_refused_by_the_database(self, guest, document):
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError):
            DocumentAccess.objects.create(
                document=document,
                workspace_id=document.workspace_id,
                subject_kind=DocumentAccess.SUBJECT_USER,
                user_id=guest.pk,
                ref="chat:conversation:1",
                level="view",
            )

    def test_a_row_with_no_subject_is_refused_by_the_database(self, document):
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError):
            DocumentAccess.objects.create(
                document=document,
                workspace_id=document.workspace_id,
                subject_kind=DocumentAccess.SUBJECT_REF,
                ref="",
                level="view",
            )

    def test_one_grant_per_subject_per_document(self, guest, document):
        from django.db.utils import IntegrityError

        _grant(document, guest, "view")
        with pytest.raises(IntegrityError):
            _grant(document, guest, "edit")

    def test_two_user_rows_on_one_document_do_not_collide_on_ref(
        self, guest, owner, document
    ):
        """The ref unique is partial: "" does equal "" in SQL, so an
        unconditional (document, ref) unique would make a second user grant
        impossible."""
        _grant(document, guest, "view")
        _grant(document, owner, "view")
        assert DocumentAccess.objects.filter(document=document).count() == 2

    def test_link_status_precedence(self, owner, document):
        now = timezone.now()
        assert _link(document, owner).status == "active"
        assert _link(
            document, owner, expires_at=now - timedelta(seconds=1)
        ).status == "expired"
        assert _link(
            document, owner, revoked_at=now, expires_at=now - timedelta(days=1)
        ).status == "revoked"

    def test_tokens_are_unguessable_and_unique(self, actor, document):
        with override_settings(STAPEL_DOCS=LINK_ON):
            tokens = {
                actor.post(
                    f"{API}/documents/{document.id}/links", {}, format="json"
                ).json()["token"]
                for _ in range(5)
            }
        assert len(tokens) == 5
        assert all(len(token) >= 40 for token in tokens)


class TestGrantedLevel:
    @override_settings(STAPEL_DOCS=BOTH_ON)
    def test_union_takes_the_maximum(self, owner, guest, document):
        _grant(document, guest, "view")
        link = _link(document, owner, level="view")
        level, outage = granted_level(
            workspace_id=document.workspace_id,
            principal=_principal(guest, token=link.token),
            document=document,
        )
        assert (level, outage) == ("view", False)
        DocumentAccess.objects.filter(document=document).update(level="edit")
        level, _ = granted_level(
            workspace_id=document.workspace_id,
            principal=_principal(guest, token=link.token),
            document=document,
        )
        assert level == "edit"

    def test_no_document_means_no_grants(self, guest, workspace_id):
        assert granted_level(
            workspace_id=workspace_id, principal=_principal(guest), document=None
        ) == (None, False)

    @override_settings(STAPEL_DOCS=LINK_ON)
    def test_an_outage_is_reported_not_swallowed(
        self, owner, guest, document, monkeypatch
    ):
        """Only the SPONSOR check is down, so the baseline still renders a
        real verdict for the bearer — and the source that could not be
        evaluated still turns the whole answer into ``unavailable`` rather
        than letting a partial evaluation read as a refusal."""
        from stapel_core.django import workspaces as ws_client

        real = ws_client.require_capability

        def flaky(workspace_id, user_id, capability, **kwargs):
            if str(user_id) == str(owner.pk) and capability == "docs.share.link":
                raise ws_client.WorkspaceLookupUnavailable("peer down")
            return real(workspace_id, user_id, capability, **kwargs)

        link = _link(document, owner)
        monkeypatch.setattr(ws_client, "require_capability", flaky)
        assert granted_level(
            workspace_id=document.workspace_id,
            principal=_principal(guest, token=link.token),
            document=document,
        ) == (None, True)
        assert authorize(
            workspace_id=document.workspace_id,
            principal=_principal(guest, token=link.token),
            action="view",
            document=document,
        ) == UNAVAILABLE


class TestSharingSeamHelpers:
    def test_ref_kind_reads_everything_before_the_last_colon(self):
        from stapel_docs.authz import ref_kind

        assert ref_kind("chat:conversation:42") == "chat:conversation"
        assert ref_kind("team:7") == "team"
        assert ref_kind("nocolon") == ""

    def test_link_expiry_honours_the_configured_ttl(self):
        with override_settings(STAPEL_DOCS={"SHARING": {"LINK": {"TTL_DAYS": 1}}}):
            assert services.link_expiry() < timezone.now() + timedelta(days=2)

    def test_effective_modes_drops_what_this_version_cannot_enforce(self):
        from stapel_docs import authz

        with override_settings(STAPEL_DOCS={"SHARING": {"MODES": ["whitelist", "bogus"]}}):
            assert authz.effective_modes() == ("whitelist",)

    def test_an_unknown_action_is_a_programming_error_not_a_deny(self, guest, workspace_id):
        with pytest.raises(ValueError):
            authorize(
                workspace_id=workspace_id,
                principal=_principal(guest),
                action="obliterate",
            )
