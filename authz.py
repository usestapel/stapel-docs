"""The single authorization choke point (sharing-axis-design §7).

Every access decision about a document routes through :func:`authorize` —
HTTP views, presigned-URL issuance, the future realtime stream's
``authorize(scope, stream_key)``. There is no second read path.

The rule is two steps and a deny:

1. **The workspace baseline** — active membership plus capability
   ``docs.<action>`` via ``workspaces.check_capability`` (fail-closed,
   deny-by-default). Always on, not configurable, and no share source can
   subtract from it.
2. **The enabled grant sources** — ``whitelist`` (explicit rows on a user,
   or on an external container resolved by a host resolver) and ``link``
   (a bearer token, the ``WorkspaceInvitation`` canon). Each is an
   INDEPENDENT SUFFICIENT reason: the composition is a union with the
   maximum granted level, so the algebra is additive and monotonic —
   enabling a mode can never revoke, disabling one can never open, and two
   modes cannot disagree because no source can say "no".
3. Deny.

Three invariants this module refuses to bend:

* ``manage`` is **never** grantable by any share source (axis §2.2) —
  deletion, moving and grant administration stay mandatory capabilities,
  so a shared-in principal can never widen the circle of access;
* an **anonymous** principal never writes, even holding an edit-level link
  (axis §6) — ``DocumentUpdate.author_id`` needs a real subject, and
  authorless vandalism is not a feature behind a flag;
* an outage is **not a verdict** — ``unavailable`` propagates to a 503 and
  is never quietly turned into deny or allow.

The :class:`Principal` form is fixed (axis §11.1): ``user_id=None`` means
no session at all; ``is_anonymous`` marks an anonymous ACCOUNT of the auth
axis (which does have a user_id); ``link_token`` carries a presented
bearer token.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)

ALLOW = "allow"
DENY = "deny"
UNAVAILABLE = "unavailable"

#: Grantable levels. ``manage`` is deliberately NOT grantable by any share
#: source ever — mandate-only (anti-escalation invariant, sharing-axis §2.2).
LEVEL_VIEW = "view"
LEVEL_EDIT = "edit"

#: Total order over grantable levels — ``view < edit``, and nothing above.
LEVEL_ORDER = {LEVEL_VIEW: 1, LEVEL_EDIT: 2}

#: Grant sources the axis will ever accept (sharing-axis §2.1) and the ones
#: this version implements. ``checks.py`` re-exports both: an unknown mode
#: is E010, a known-but-unimplemented one is E011, and neither ever
#: "enables everything" by accident.
MODE_WHITELIST = "whitelist"
MODE_LINK = "link"
KNOWN_SHARING_MODES = (MODE_WHITELIST, MODE_LINK)
IMPLEMENTED_SHARING_MODES = (MODE_WHITELIST, MODE_LINK)

#: How long a ref-subject resolver's point-query answer is cached. Same
#: order as the membership cache in ``stapel_core.django.workspaces`` (30 s)
#: for the same reason: revoking membership in the foreign container must
#: reach the document in seconds — not never (a copied membership) and not
#: at N RPS (no cache at all).
REF_RESOLVER_CACHE_SECONDS = 30

#: Actions and the workspace capability answering each. Declared from day
#: 1 (including the share capabilities) so host role overlays never have
#: to migrate when sharing modes are enabled later.
ACTION_CAPABILITIES = {
    "view": "docs.view",
    "edit": "docs.edit",
    "manage": "docs.manage",
}
#: The level a share grant must carry to satisfy an action. ``manage`` is
#: absent on purpose — there is no level that buys it.
ACTION_LEVELS = {"view": LEVEL_VIEW, "edit": LEVEL_EDIT}

CAP_SHARE_WHITELIST = "docs.share.whitelist"
CAP_SHARE_LINK = "docs.share.link"
CAPABILITIES = (
    "docs.view",
    "docs.edit",
    "docs.manage",
    CAP_SHARE_WHITELIST,
    CAP_SHARE_LINK,
)


@dataclass(frozen=True)
class Principal:
    """Who is asking. Built by the view layer, consumed only here."""

    user_id: Optional[UUID]
    is_anonymous: bool = False
    link_token: Optional[str] = None

    @classmethod
    def from_request(cls, request, *, link_token: str | None = None) -> "Principal":
        user = getattr(request, "user", None)
        user_id = getattr(user, "pk", None) if getattr(user, "is_authenticated", False) else None
        is_anon = bool(getattr(user, "is_anonymous_account", False))
        return cls(user_id=user_id, is_anonymous=is_anon, link_token=link_token)

    @property
    def is_anonymous_bearer(self) -> bool:
        """True for BOTH anonymities the axis distinguishes (§11.1): no
        session at all, and an anonymous account of the auth axis. They are
        different subjects but the same trust level, so one predicate gates
        them — there is no second anonymity axis in docs."""
        return self.user_id is None or self.is_anonymous


def sharing_settings() -> dict:
    """The effective ``SHARING`` dict, defaults merged in (never partial)."""
    from .conf import DEFAULT_SHARING, docs_settings

    configured = docs_settings.SHARING or {}
    merged = {**DEFAULT_SHARING, **configured}
    merged["LINK"] = {**DEFAULT_SHARING["LINK"], **(configured.get("LINK") or {})}
    return merged


def effective_modes(workspace_id=None) -> tuple:
    """Grant sources actually in force for *workspace_id*.

    Deployment ``MODES`` filtered to what this version implements, and
    filtered again by any per-workspace NARROWING (axis §4). The narrowing
    is an intersection and only ever an intersection: a workspace may forbid
    links for itself, never enable what the deployment left off.

    **Delta from the axis, stated rather than faked:** the axis names
    ``Workspace.settings["docs"]["sharing"]["modes"]`` as the narrowing
    source, and stapel-workspaces exposes no reader for it — its comm
    surface is exactly ``check_membership`` / ``check_capability`` /
    ``check_mandate``, none of which carry workspace settings. Inventing a
    second path to another module's rows (an HTTP call, a direct model
    import) is the seam violation the L2 canon exists to prevent, and
    guessing "no narrowing" from data we never read would be worse than
    admitting it. So this resolves to the deployment list until workspaces
    ships a settings surface; the intersection lives in this one function,
    which is the whole change when it does.
    """
    modes = sharing_settings().get("MODES") or ()
    return tuple(
        mode
        for mode in modes
        if mode in KNOWN_SHARING_MODES and mode in IMPLEMENTED_SHARING_MODES
    )


def mode_enabled(mode: str, workspace_id=None) -> bool:
    """Is *mode* in force? The kill-switch predicate the share sheet reads
    to mark existing rows "suspended by configuration" rather than hide
    them (axis §3): an admin who cannot see an inert grant believes it was
    revoked."""
    return mode in effective_modes(workspace_id)


def link_settings() -> dict:
    """The ``SHARING["LINK"]`` sub-dict with its closed defaults merged in."""
    return sharing_settings()["LINK"]


def authorize(*, workspace_id, principal: Principal, action: str, document=None) -> str:
    """Decide *action* for *principal* on *workspace_id* (optionally a
    specific document). Returns ``allow`` | ``deny`` | ``unavailable``.

    ``unavailable`` means the workspaces service rendered no verdict —
    callers must answer 503, never 403 ("a routing 404 is not a verdict",
    stapel-core workspaces client canon).

    Grant sources are consulted only when a *document* is named: a grant is
    a row about one object, so workspace-wide listings stay baseline-only
    by construction rather than by remembering to check.
    """
    if action not in ACTION_CAPABILITIES:
        raise ValueError(f"unknown docs action: {action!r}")

    # 1. Workspace baseline — always on, not configurable.
    if principal.user_id is not None:
        from stapel_core.django.workspaces import (
            WorkspaceLookupUnavailable,
            require_capability,
        )

        try:
            membership = require_capability(
                workspace_id, principal.user_id, ACTION_CAPABILITIES[action]
            )
        except WorkspaceLookupUnavailable:
            return UNAVAILABLE
        if membership is not None:
            return ALLOW

    # 2. Additional grant sources (whitelist / link), union with max level.
    #    `manage` has no entry in ACTION_LEVELS and therefore no reachable
    #    grant: no source consulted, nothing to escalate through.
    needed = ACTION_LEVELS.get(action)
    #    ...and an anonymous presenter never writes, whatever level the link
    #    it carries was stamped with (axis §6).
    writes = needed is not None and needed != LEVEL_VIEW
    if needed is not None and not (writes and not may_write(principal)):
        level, outage = granted_level(
            workspace_id=workspace_id, principal=principal, document=document
        )
        if outage:
            # A source could not be evaluated. Denying here would turn a
            # workspaces outage into "you were never shared this document".
            return UNAVAILABLE
        if level is not None and LEVEL_ORDER[level] >= LEVEL_ORDER[needed]:
            return ALLOW

    # 3. Deny by default.
    return DENY


def granted_level(*, workspace_id, principal: Principal, document=None):
    """The MAXIMUM level the enabled grant sources give *principal* on
    *document*, as ``(level | None, outage: bool)``.

    Public because the presentation layer needs the same number the rule
    used (what a link bearer may do with the document it just opened), and
    a second implementation of "what is this principal allowed" is exactly
    how a share mode ships half-enforced.

    Union semantics: every source is evaluated, the best answer wins. No
    source can lower another's — there are no deny rows in this algebra, by
    construction (axis §3), which is why enabling a mode is always safe to
    reason about locally.
    """
    if document is None:
        return None, False
    modes = effective_modes(workspace_id)
    if not modes:
        return None, False

    levels = []
    outage = False
    if MODE_WHITELIST in modes:
        levels.extend(_whitelist_levels(document, principal))
    if MODE_LINK in modes and principal.link_token:
        link_level, link_outage = _link_level(document, principal)
        outage = outage or link_outage
        if link_level is not None:
            levels.append(link_level)
    if not levels:
        return None, outage
    return max(levels, key=lambda lvl: LEVEL_ORDER[lvl]), outage


def check_share_capability(*, workspace_id, principal: Principal, capability: str) -> str:
    """``allow`` | ``deny`` | ``unavailable`` for a share-administration
    capability (``docs.share.whitelist`` / ``docs.share.link``).

    Deliberately NOT part of :func:`authorize`'s action vocabulary: minting
    a grant is not a level on the document, it is a mandate in the
    workspace, and conflating the two is how "shared with me" turns into
    "may share with others" (axis §2.2). Anonymous principals and sessions
    without a user hold no mandate anywhere, so they deny without a call.
    """
    if principal.user_id is None or principal.is_anonymous:
        return DENY
    from stapel_core.django.workspaces import (
        WorkspaceLookupUnavailable,
        require_capability,
    )

    try:
        membership = require_capability(workspace_id, principal.user_id, capability)
    except WorkspaceLookupUnavailable:
        return UNAVAILABLE
    return ALLOW if membership is not None else DENY


# ── grant sources ────────────────────────────────────────────────────


def _whitelist_levels(document, principal: Principal) -> list:
    """Levels the whitelist rows of *document* give *principal*.

    Anonymous principals are refused outright (axis §2.1): a whitelist
    names people, and neither "no session" nor an anonymous account is a
    person this list could have meant.
    """
    if principal.user_id is None or principal.is_anonymous:
        return []

    from .models import DocumentAccess

    levels = []
    rows = DocumentAccess.objects.filter(document=document)
    for row in rows:
        if row.subject_kind == DocumentAccess.SUBJECT_USER:
            if str(row.user_id) == str(principal.user_id):
                levels.append(row.level)
        elif resolve_ref(row.ref, principal.user_id):
            levels.append(row.level)
    return levels


def ref_kind(ref: str) -> str:
    """The registry key of a subject reference: everything before the LAST
    colon ("chat:conversation:<id>" -> "chat:conversation"). One rule for
    every depth of namespacing, so a host's key is whatever it registered."""
    return ref.rsplit(":", 1)[0] if ":" in ref else ""


def get_ref_resolver(kind: str):
    """The registered resolver for *kind*, or None.

    ``SHARING["RESOLVERS"]`` is a merge registry of dotted paths
    (axis §2.3). An unregistered kind and an unimportable path are the same
    answer here — nothing to ask — and the caller turns that into deny.
    A broken CONFIGURATION is separately loud at deploy time (E014): a
    misconfiguration must not degrade into "open".
    """
    from django.utils.module_loading import import_string

    dotted = (sharing_settings().get("RESOLVERS") or {}).get(kind)
    if not dotted:
        return None
    try:
        return import_string(dotted)
    except Exception:
        logger.warning("docs sharing: resolver %r (%s) cannot be imported", kind, dotted)
        return None


def resolve_ref(ref: str, user_id) -> bool:
    """Point-query the host resolver: is *user_id* a member of *ref*?

    Fail-closed on every unhappy path — unknown kind, missing resolver,
    raising resolver, non-boolean answer — because a configuration error
    that degrades into "allowed" is the env-address failure class this
    fleet already paid for. Answers are cached briefly
    (:data:`REF_RESOLVER_CACHE_SECONDS`); a REFUSAL is cached too, so a
    resolver cannot be turned into a per-request amplifier, but an
    EXCEPTION is not — a failure is not an answer worth remembering.
    """
    kind = ref_kind(ref)
    if not kind:
        return False
    resolver = get_ref_resolver(kind)
    if resolver is None:
        return False

    from django.core.cache import cache

    cache_key = f"docs:sharing:ref:{ref}:{user_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return bool(cached)
    try:
        allowed = bool(resolver(ref, user_id))
    except Exception:
        logger.warning("docs sharing: resolver for %r raised — denying", kind, exc_info=True)
        return False
    cache.set(cache_key, allowed, REF_RESOLVER_CACHE_SECONDS)
    return allowed


def _link_level(document, principal: Principal):
    """The level the presented bearer token gives, as ``(level, outage)``.

    Four gates, all of them live on every presentation (a link is checked,
    never "accepted" — axis §6):

    1. the token names a LIVE link of THIS document (revoked beats expired
       beats active; a token from another workspace's document simply is
       not a row here);
    2. the presenter is authenticated, or ``LINK["ANONYMOUS"]`` is on;
    3. the link's creator STILL holds ``docs.share.link`` in the workspace —
       a bearer secret in unknown hands whose sponsor has left is the leak
       this asymmetry with whitelist exists to close. No verdict from
       workspaces is an outage, never a silent deny and never a silent
       allow;
    4. ...and, at the level the link carries.
    """
    from .models import DocumentLink

    link = DocumentLink.objects.filter(
        document=document, token=principal.link_token
    ).first()
    if link is None or not link.is_live:
        return None, False
    if principal.is_anonymous_bearer and not link_settings().get("ANONYMOUS"):
        return None, False
    if link.created_by_id is None:
        # The creator's account is gone (erased or merged away). Nobody
        # sponsors this token any more — same verdict as losing the
        # capability, reached without asking workspaces about a null.
        return None, False

    from stapel_core.django.workspaces import (
        WorkspaceLookupUnavailable,
        require_capability,
    )

    try:
        membership = require_capability(
            link.workspace_id, link.created_by_id, CAP_SHARE_LINK
        )
    except WorkspaceLookupUnavailable:
        return None, True
    if membership is None:
        return None, False
    return link.level, False


def may_write(principal: Principal) -> bool:
    """Whether *principal* may ever author a write, whatever their level.

    An anonymous presenter never writes (axis §6): the update journal and
    the revision history are attributed by design, and an authorless edit
    is vandalism with no subject to name. This is the one place the rule
    lives, so no endpoint can grant it by forgetting.
    """
    return not principal.is_anonymous_bearer


__all__ = [
    "ALLOW",
    "DENY",
    "UNAVAILABLE",
    "LEVEL_VIEW",
    "LEVEL_EDIT",
    "LEVEL_ORDER",
    "MODE_WHITELIST",
    "MODE_LINK",
    "KNOWN_SHARING_MODES",
    "IMPLEMENTED_SHARING_MODES",
    "REF_RESOLVER_CACHE_SECONDS",
    "ACTION_CAPABILITIES",
    "ACTION_LEVELS",
    "CAP_SHARE_WHITELIST",
    "CAP_SHARE_LINK",
    "CAPABILITIES",
    "Principal",
    "authorize",
    "granted_level",
    "check_share_capability",
    "sharing_settings",
    "effective_modes",
    "mode_enabled",
    "link_settings",
    "ref_kind",
    "get_ref_resolver",
    "resolve_ref",
    "may_write",
]
