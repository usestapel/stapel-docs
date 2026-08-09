"""The single authorization choke point (sharing-axis-design §7).

Every access decision about a document routes through :func:`authorize` —
HTTP views, presigned-URL issuance, the future realtime stream's
``authorize(scope, stream_key)``. There is no second read path.

v1 implements exactly the immutable workspace baseline: active membership
plus capability ``docs.<action>`` via ``workspaces.check_capability``
(fail-closed, deny-by-default). The sharing axis's additional grant
sources (whitelist / link) are phase 3: their config keys exist with
closed defaults, and opening them before the mechanism exists is a loud
system-check error (``checks.py``), never a silent no-op.

The :class:`Principal` form is fixed on day 1 (sharing-axis §11.1) so that
anonymous-link support later is an additive branch, not a core rewrite:
``user_id=None`` means no session at all; ``is_anonymous`` marks an
anonymous ACCOUNT of the auth axis (which does have a user_id);
``link_token`` carries a presented bearer token, unused in v1.
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

#: Actions and the workspace capability answering each. Declared from day
#: 1 (including the share capabilities) so host role overlays never have
#: to migrate when sharing modes are enabled later.
ACTION_CAPABILITIES = {
    "view": "docs.view",
    "edit": "docs.edit",
    "manage": "docs.manage",
}
CAPABILITIES = (
    "docs.view",
    "docs.edit",
    "docs.manage",
    "docs.share.whitelist",
    "docs.share.link",
)


@dataclass(frozen=True)
class Principal:
    """Who is asking. Built by the view layer, consumed only here."""

    user_id: Optional[UUID]
    is_anonymous: bool = False
    link_token: Optional[str] = None

    @classmethod
    def from_request(cls, request) -> "Principal":
        user = getattr(request, "user", None)
        user_id = getattr(user, "pk", None) if getattr(user, "is_authenticated", False) else None
        is_anon = bool(getattr(user, "is_anonymous_account", False))
        return cls(user_id=user_id, is_anonymous=is_anon, link_token=None)


def authorize(*, workspace_id, principal: Principal, action: str, document=None) -> str:
    """Decide *action* for *principal* on *workspace_id* (optionally a
    specific document). Returns ``allow`` | ``deny`` | ``unavailable``.

    ``unavailable`` means the workspaces service rendered no verdict —
    callers must answer 503, never 403 ("a routing 404 is not a verdict",
    stapel-core workspaces client canon).

    The algebra is additive and monotonic: sources only ever grant, so
    enabling a future mode can never revoke, and the baseline can never be
    configured away.
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

    # 2. Additional grant sources (whitelist / link) — phase 3. Effective
    #    MODES in v1 is always [] (system checks refuse anything else), so
    #    there is nothing to consult here yet. The branch point is kept so
    #    the future implementation lands inside this function, not around it.

    # 3. Deny by default.
    return DENY


__all__ = [
    "ALLOW",
    "DENY",
    "UNAVAILABLE",
    "LEVEL_VIEW",
    "LEVEL_EDIT",
    "ACTION_CAPABILITIES",
    "CAPABILITIES",
    "Principal",
    "authorize",
]
