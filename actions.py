"""Action subscriptions of stapel-docs.

Handlers are idempotent-minded (delivery is at-least-once — outbox retries,
broker redelivery). Transport is chosen by ``STAPEL_COMM`` (in-process in a
monolith, bus consumer in microservices); the handler code is identical.

The erasure protocol is NOT here. ``apps.ready()`` calls
``stapel_core.gdpr.register_gdpr_owner("docs", SUBJECT_TYPES,
erase_subject)``, and core subscribes ``gdpr.erasure.requested``,
``gdpr.owner.probe`` and the deprecated ``user.deleted`` from one module —
the same handlers this file used to carry. The probe is still answered by
the subscriber that erases, which is the whole point of the probe: an
answer proves the erasure path is consumed, not that a container is
deployed. What stays ours is :func:`stapel_docs.erasure.erase_subject`.

Consumers living here:

- ``user.merged`` → the other half of the account life cycle: a guest
  folded into an existing account keeps its authorship, re-parented rather
  than anonymized;
- the INGEST seam (design §2/§6): ``STAPEL_DOCS["INGEST"]`` maps
  ``{action_name: dotted-path mapper}`` so a host gets event-driven ingest
  without writing a subscriber. Docs never learns a foreign event schema —
  the mapper (host code) turns the payload into ``create_document`` kwargs.
"""
import logging
from typing import Callable

from django.core.exceptions import ImproperlyConfigured, ValidationError
from stapel_core.comm import on_action, subscribe_action

logger = logging.getLogger(__name__)


class MergeTargetNotReady(RuntimeError):
    """A ``user.merged`` arrived before the surviving account exists here.

    Transient, not a bug: the guest has authored rows to carry over but
    there is no local user row to point their FKs at yet. Raising is the
    comm layer's retry signal — ``deliver()`` wraps a failing handler in
    ``ActionDeliveryError`` and the outbox redelivers — so the transfer
    completes once the survivor's user projection lands. An operator seeing
    this in a redelivery loop is looking at an ordering lag, not a defect.
    """


@on_action("user.merged")
def handle_user_merged(event):
    """Carry a merged-away account's authorship over to the survivor.

    Re-parents every row this module keys by a user, in one transaction:

    * :class:`~stapel_docs.models.Document` ``owner`` — who the document
      belongs to;
    * :class:`~stapel_docs.models.Folder` ``created_by``;
    * :class:`~stapel_docs.models.Revision` ``created_by`` — the version
      history keeps naming the person who saved each revision;
    * :class:`~stapel_docs.models.DocumentUpdate` ``author_id`` — the CRDT
      journal's attributed writes (a bare UUID column, deliberately FK-less);
    * :class:`~stapel_docs.models.UploadSession` ``created_by``, so an
      in-flight upload can still be finalized by the account that now holds
      the ticket;
    * :class:`~stapel_docs.models.DocumentAccess` ``granted_by`` and
      :class:`~stapel_docs.models.DocumentLink` ``created_by`` — who shared,
      and therefore whose capability keeps every bearer link alive
      (``authorize`` asks the CURRENT holder, so re-parenting a link is what
      stops a merge from silently killing links the survivor still sponsors);
    * :class:`~stapel_docs.models.DocumentAccess` rows where the guest was
      the SUBJECT — the access they were given follows them into the
      surviving account, with COLLISION FOLDING to the HIGHER level: two
      grants on one document for what turns out to be one person is one
      grant, and the person can do the most either of them allowed. Folding
      down would silently revoke access nobody asked to revoke;
    * :class:`~stapel_docs.models.Star` and
      :class:`~stapel_docs.models.RecentEntry` — the guest's own view of the
      corpus, re-parented with COLLISION FOLDING, because both tables are
      unique per (user, target) and a blind update would violate that the
      moment the survivor had already starred or opened the same document.
      A star folds to "still starred" (drop the guest's duplicate); a recent
      folds to the NEWER timestamp, since "when did I last reach this" has
      one answer for one person and it is the later one.

    The opposite instruction to ``user.deleted``, which *anonymizes* the same
    columns: an account erasure means "nobody wrote this any more", a merge
    means "somebody else did". Answering only the first would leave a guest's
    documents owned by an id that can no longer sign in — never listed for
    the survivor, and never erased either, because no erasure is requested
    for an account that was merged rather than closed.

    Two different "unknown id" situations, and conflating them loses data:

    * the guest authored nothing here (or a previous delivery already moved
      it all) — a genuine no-op, returned quietly;
    * the guest authored rows but the survivor has no user row here yet —
      NOT a no-op. :class:`MergeTargetNotReady` is raised so the event is
      redelivered, because returning success would let the outbox mark it
      delivered and strand the documents.
    """
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from .models import (
        Document,
        DocumentAccess,
        DocumentLink,
        DocumentUpdate,
        Folder,
        RecentEntry,
        Revision,
        Star,
        UploadSession,
    )

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error("user.merged without from/into user id: %s", event.event_id)
        return
    if str(from_user_id) == str(into_user_id):
        return

    #: model -> the column naming a user on it. Straight re-parenting: no
    #: uniqueness constrains these columns, so an UPDATE is the whole move.
    owned = (
        (Document, "owner_id"),
        (Folder, "created_by_id"),
        (Revision, "created_by_id"),
        (DocumentUpdate, "author_id"),
        (UploadSession, "created_by_id"),
        (DocumentAccess, "granted_by_id"),
        (DocumentLink, "created_by_id"),
    )
    #: Per-user state, unique per (user, target) — these fold rather than
    #: move (see the docstring).
    per_user = (Star, RecentEntry)

    with transaction.atomic():
        # Both reads and the decision they feed happen inside the transaction
        # and before the first write, so the "not yet" path below can never
        # leave half the authorship moved.
        try:
            owns_something = any(
                model.objects.filter(**{column: from_user_id}).exists()
                for model, column in owned
            ) or any(
                model.objects.filter(user_id=from_user_id).exists()
                for model in per_user
            ) or DocumentAccess.objects.filter(
                subject_kind=DocumentAccess.SUBJECT_USER, user_id=from_user_id
            ).exists()
            # The survivor probe is read here, under the same guard, because a
            # malformed *into* id must not escape as a poison pill either.
            survivor_exists = (
                get_user_model().objects.filter(pk=into_user_id).exists()
            )
        except (ValidationError, ValueError, TypeError):
            # Django raises ValidationError (not ValueError) for a malformed
            # UUID; an id that cannot address a row here names nothing, and an
            # escaping exception is a poison pill no redelivery repairs.
            logger.warning("user.merged with unusable user ids: %s", event.event_id)
            return
        if not owns_something:
            # Quiet by design — this is also the at-least-once idempotency
            # path: a redelivery finds nothing left under the guest.
            return
        if not survivor_exists:
            raise MergeTargetNotReady(
                f"user.merged {from_user_id} -> {into_user_id}: the surviving "
                f"account has no user row in stapel-docs yet; redeliver once "
                f"its projection has landed"
            )

        moved = {
            model.__name__: model.objects.filter(**{column: from_user_id}).update(
                **{column: into_user_id}
            )
            for model, column in owned
        }
        moved["DocumentAccess.subject"] = _fold_access(from_user_id, into_user_id)
        moved["Star"] = _fold_stars(from_user_id, into_user_id)
        moved["RecentEntry"] = _fold_recents(from_user_id, into_user_id)

    logger.info(
        "user.merged %s -> %s: docs authorship and per-user state carried over (%s)",
        from_user_id, into_user_id, moved,
    )


def _fold_access(from_user_id, into_user_id) -> int:
    """Re-parent the guest's whitelist grants, keeping the HIGHER level.

    Unlike a star, a grant carries a power, so a collision is not "drop
    one": the merged person may do whatever either identity could, and
    keeping the lower level would revoke access as a side effect of a
    merge — a silent, invisible loss on exactly the table where losing
    access is hardest to diagnose.
    """
    from .authz import LEVEL_ORDER
    from .models import DocumentAccess

    guest_rows = DocumentAccess.objects.filter(
        subject_kind=DocumentAccess.SUBJECT_USER, user_id=from_user_id
    )
    survivor = {
        row["document_id"]: row["level"]
        for row in DocumentAccess.objects.filter(
            subject_kind=DocumentAccess.SUBJECT_USER, user_id=into_user_id
        ).values("document_id", "level")
    }
    moved = 0
    for row in list(guest_rows):
        existing = survivor.get(row.document_id)
        if existing is None:
            DocumentAccess.objects.filter(pk=row.pk).update(user_id=into_user_id)
            moved += 1
            continue
        if LEVEL_ORDER[row.level] > LEVEL_ORDER[existing]:
            DocumentAccess.objects.filter(
                subject_kind=DocumentAccess.SUBJECT_USER,
                user_id=into_user_id,
                document_id=row.document_id,
            ).update(level=row.level)
        row.delete()
    return moved


def _fold_stars(from_user_id, into_user_id) -> int:
    """Re-parent the guest's stars, dropping the ones the survivor already has.

    A star is a boolean fact about (person, item), so a collision has an
    obvious right answer — the item stays starred, once. Deleting the
    guest's duplicate FIRST is what keeps the following UPDATE from hitting
    ``docs_star_user_document`` / ``docs_star_user_folder``.
    """
    from .models import Star

    survivor = Star.objects.filter(user_id=into_user_id)
    Star.objects.filter(
        user_id=from_user_id,
        document_id__in=list(
            survivor.exclude(document__isnull=True).values_list("document_id", flat=True)
        ),
    ).delete()
    Star.objects.filter(
        user_id=from_user_id,
        folder_id__in=list(
            survivor.exclude(folder__isnull=True).values_list("folder_id", flat=True)
        ),
    ).delete()
    return Star.objects.filter(user_id=from_user_id).update(user_id=into_user_id)


def _fold_recents(from_user_id, into_user_id) -> int:
    """Re-parent the guest's recents, keeping the NEWER timestamp on a clash.

    Unlike a star, a recent carries a value, so folding is not "drop one":
    the merged person reached that document at whichever moment is later,
    and keeping the survivor's older stamp would quietly reorder their list.
    """
    from .models import RecentEntry

    survivor = {
        row["document_id"]: row["accessed_at"]
        for row in RecentEntry.objects.filter(user_id=into_user_id).values(
            "document_id", "accessed_at"
        )
    }
    moved = 0
    for entry in RecentEntry.objects.filter(user_id=from_user_id):
        existing = survivor.get(entry.document_id)
        if existing is None:
            RecentEntry.objects.filter(pk=entry.pk).update(user_id=into_user_id)
            moved += 1
            continue
        if entry.accessed_at > existing:
            RecentEntry.objects.filter(
                user_id=into_user_id, document_id=entry.document_id
            ).update(accessed_at=entry.accessed_at)
        entry.delete()
    return moved


# ─── INGEST seam ─────────────────────────────────────────────────────

#: action name -> resolved mapper. Rebuilt atomically by :func:`wire_ingest`;
#: the single dispatcher below reads it at delivery time, so re-wiring
#: (tests, settings overlays) never stacks duplicate subscriptions.
_INGEST_MAPPERS: dict[str, Callable[[dict], dict]] = {}


def wire_ingest() -> None:
    """Resolve ``STAPEL_DOCS["INGEST"]`` and subscribe the dispatcher.

    Called from ``apps.py:ready()``; tests re-call it after overriding
    settings. Configured-but-broken must not be silent (system-check
    failure genre): an unimportable or non-callable mapper raises
    :class:`ImproperlyConfigured` instead of a log-and-skip.
    """
    from django.utils.module_loading import import_string

    from .conf import docs_settings

    resolved: dict[str, Callable[[dict], dict]] = {}
    for action_name, dotted in (docs_settings.INGEST or {}).items():
        try:
            mapper = import_string(dotted)
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"STAPEL_DOCS['INGEST'][{action_name!r}] = {dotted!r} cannot be imported"
            ) from exc
        if not callable(mapper):
            raise ImproperlyConfigured(
                f"STAPEL_DOCS['INGEST'][{action_name!r}] = {dotted!r} is not callable"
            )
        resolved[action_name] = mapper

    _INGEST_MAPPERS.clear()
    _INGEST_MAPPERS.update(resolved)
    for action_name in resolved:
        # subscribe() dedups an identical handler — re-wiring is safe.
        subscribe_action(action_name, _handle_ingest)


def _handle_ingest(event):
    """Route a configured host action into a document.

    Delivery is at-least-once; create is not naturally idempotent, so
    dedup (e.g. an idempotency key in metadata) is the mapper/host's call —
    same contract as any bus consumer creating rows.
    """
    mapper = _INGEST_MAPPERS.get(event.event_type)
    if mapper is None:
        # Stale subscription: a re-wire dropped this action (there is no
        # unsubscribe in the registry) — inert by design.
        return
    kwargs = mapper(event.payload)

    from . import services  # lazy: mirror functions.py

    services.create_document(**kwargs)


__all__ = [
    "MergeTargetNotReady",
    "handle_user_merged",
    "wire_ingest",
]
