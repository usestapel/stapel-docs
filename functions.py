"""comm surface of stapel-docs (Functions).

Every Function carries a JSON schema in ``schemas/functions/`` — tests run
with ``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails
loudly. Registration happens on import from ``apps.py:ready()``; re-imports
are no-ops.

Provided:

- ``docs.create_document`` — the main ingest seam (design §6): ironmemo
  dumps transcripts/summaries through it. The event-driven variant is the
  ``STAPEL_DOCS["INGEST"]`` registry (``actions.py``);
- ``docs.usage`` — the metering surface billing composes with (drive-spec
  §3.4). Read-only, and gated by the SAME caller authority as the write
  seam: how full a workspace is, and of what, is workspace data. A read
  that skipped the gate because "it only reads" would let any bus
  participant enumerate every workspace's corpus size and type mix by id.

Emitted actions live in ``events.py``.

Authority on this surface (security audit DOCS-02): a comm call has no
session, so the payload must carry the authority. ``actor_id`` is
authorized through the SAME choke point as an HTTP caller
(``authz.authorize`` -> ``docs.edit`` in the target workspace); a service
acting without a user actor must be named in
``STAPEL_DOCS["INTERNAL_TRUSTED_SERVICES"]``. Anything else is refused
while ``INTERNAL_REQUIRE_CALLER`` is on (the default) — an internal caller
is a caller, not an exemption.
"""
from stapel_core.comm import function


def _authorized_actor(payload, *, action: str = "edit"):
    """Bind the call to an authorized actor; returns the actor user or None.

    Refusals are loud: :class:`~stapel_docs.services.CallerNotAuthorized`
    for a caller that may not act here, a 503-mapped ``DocsError`` when
    the workspaces service rendered no verdict (fail-closed — an outage is
    never an allow).

    ``action`` is the capability the call needs in the target workspace:
    ``edit`` to write a document, ``view`` to read an aggregate over them.
    """
    from django.contrib.auth import get_user_model

    from .authz import ALLOW, UNAVAILABLE, Principal, authorize
    from .conf import docs_settings
    from .errors import ERR_503_WORKSPACES
    from .services import CallerNotAuthorized, DocsError

    workspace_id = payload["workspace_id"]
    actor_id = payload.get("actor_id")
    caller_service = payload.get("caller_service")

    if actor_id:
        verdict = authorize(
            workspace_id=workspace_id,
            principal=Principal(user_id=actor_id),
            action=action,
        )
        if verdict == UNAVAILABLE:
            raise DocsError(503, ERR_503_WORKSPACES)
        if verdict != ALLOW:
            raise CallerNotAuthorized({"actor_id": str(actor_id)})
        # A vanished actor degrades to None (same shape GDPR anonymize
        # leaves behind): authorship is optional, authority is not.
        return get_user_model().objects.filter(pk=actor_id).first()

    trusted = [str(name) for name in (docs_settings.INTERNAL_TRUSTED_SERVICES or [])]
    if caller_service and caller_service in trusted:
        return None

    if docs_settings.INTERNAL_REQUIRE_CALLER:
        raise CallerNotAuthorized({"caller_service": caller_service or ""})
    return None


def _resolve_owner(payload, actor):
    """The document's owner: the actor by default.

    A different owner is attribution of content to another person, so that
    person must be a member of the workspace too — otherwise this seam can
    seed documents "owned" by any user id a caller cares to name.
    """
    from django.contrib.auth import get_user_model

    from .authz import ALLOW, UNAVAILABLE, Principal, authorize
    from .errors import ERR_503_WORKSPACES
    from .services import CallerNotAuthorized, DocsError

    owner_id = payload.get("owner_id")
    if not owner_id:
        return actor
    if actor is not None and str(owner_id) == str(actor.pk):
        return actor
    if not get_user_model().objects.filter(pk=owner_id).exists():
        # A vanished owner degrades to None: documents are workspace content
        # and authorship is optional (the same shape GDPR anonymize leaves
        # behind), so ingest never fails over an erased user.
        return None
    verdict = authorize(
        workspace_id=payload["workspace_id"],
        principal=Principal(user_id=owner_id),
        action="view",
    )
    if verdict == UNAVAILABLE:
        raise DocsError(503, ERR_503_WORKSPACES)
    if verdict != ALLOW:
        raise CallerNotAuthorized({"owner_id": str(owner_id)})
    return get_user_model().objects.filter(pk=owner_id).first()


@function("docs.create_document")
def create_document(payload):
    """Create a document. Output: ``{"document_id": str}``.

    ``body`` is a utf-8 string (the Function payload is JSON — opaque
    binaries go through the upload-session flow, not this seam);
    ``folder_path`` like ``/Meetings/2026-08`` creates folders idempotently.
    An unknown ``type`` raises :class:`~stapel_docs.doc_types.DocTypeNotRegistered`
    — loud, so a caller never silently loses content into a mistyped slug.
    """
    # Authority first: an unauthorized caller learns nothing about the
    # registry, and no work is done on its behalf.
    actor = _authorized_actor(payload)
    owner = _resolve_owner(payload, actor)

    from .doc_types import get_doc_type

    get_doc_type(payload["type"])  # unknown type -> DocTypeNotRegistered

    body = payload.get("body")

    from . import services  # lazy: the comm surface must import alone

    document = services.create_document(
        workspace_id=payload["workspace_id"],
        type=payload["type"],
        title=payload["title"],
        folder_path=payload.get("folder_path"),
        body=body.encode("utf-8") if body is not None else None,
        mime_type=payload.get("mime_type") or "",
        metadata=payload.get("metadata"),
        owner=owner,
    )
    return {"document_id": str(document.id)}


@function("docs.usage")
def usage(payload):
    """Stored-byte and item metering for one workspace (drive-spec §3.4).

    Output::

        {"bytes_live": int, "bytes_trash": int, "bytes_total": int,
         "documents": int, "folders": int,
         "by_type": {slug: {"documents": int, "bytes": int}}}

    ``bytes_total`` is the SAME sum the 507 quota refuses against
    (``services.workspace_usage_bytes``) — one number, so a meter and a
    refusal can never tell an operator two different stories about the same
    workspace. Composing an entitlement ceiling out of it
    (``billing.check_entitlement``) is the HOST's glue: docs owns the
    measurement, never the price.

    Authority: identical to ``docs.create_document`` except that the
    capability asked for is ``docs.view`` rather than ``docs.edit`` — an
    ``actor_id`` authorized through the same choke point, or a
    ``caller_service`` the host listed in ``INTERNAL_TRUSTED_SERVICES``.
    """
    _authorized_actor(payload, action="view")

    from . import services  # lazy: the comm surface must import alone

    return services.workspace_usage(payload["workspace_id"])
