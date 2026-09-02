"""Action subscriptions of stapel-docs.

Handlers are idempotent-minded (delivery is at-least-once — outbox retries,
broker redelivery). Transport is chosen by ``STAPEL_COMM`` (in-process in a
monolith, bus consumer in microservices); the handler code is identical.

Consumers living here:

- ``gdpr.erasure.requested`` → :mod:`stapel_docs.erasure` for the named
  subject (``account`` | ``workspace`` | ``document``), answered with a
  ``gdpr.section.erased`` receipt carrying what was actually removed;
- ``gdpr.owner.probe`` → ``gdpr.owner.alive``, **from this same module** —
  that co-location is the whole point of the probe: an answer proves the
  erasure path is consumed, not that a container is deployed;
- ``user.deleted`` → the same erasure, subject ``account`` (deprecated by
  stapel-gdpr 0.5.0, removed there in 0.6.0; kept working here for one
  minor so a host on either version erases);
- ``user.merged`` → the other half of that life cycle: a guest folded into
  an existing account keeps its authorship, re-parented rather than
  anonymized;
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


@on_action("gdpr.erasure.requested")
def handle_erasure_requested(event):
    """Erase the docs slice of one subject and receipt what was removed.

    Subjects this owner does not claim are ignored in silence — gdpr only
    creates an ``ErasurePart`` for owners that declared the subject type,
    so answering for a foreign subject would certify an erasure nobody was
    asked for. Erasure and receipt share one transaction (outbox
    discipline): the receipt leaves iff the erasure committed, so a
    half-done purge can never mark the request complete.
    """
    from django.db import transaction
    from stapel_core.comm import emit

    from .erasure import OWNER, SUBJECT_TYPES, erase

    payload = event.payload or {}
    subject_type = payload.get("subject_type")
    subject_key = payload.get("subject_key")
    correlation_id = payload.get("correlation_id")
    if not subject_type or not subject_key or not correlation_id:
        logger.error(
            "malformed gdpr.erasure.requested event: %s",
            getattr(event, "event_id", "?"),
        )
        return
    if subject_type not in SUBJECT_TYPES:
        return

    with transaction.atomic():
        counts = erase(
            subject_type, subject_key, workspace_id=payload.get("workspace_id")
        )
        emit(
            "gdpr.section.erased",
            {
                "correlation_id": str(correlation_id),
                "owner": OWNER,
                "subject_type": str(subject_type),
                "subject_key": str(subject_key),
                # Durable, deterministic proof: a redelivery receipts the
                # same id for the same request instead of inventing a second
                # "erasure" in the audit trail.
                "receipt_id": f"{OWNER}:{correlation_id}",
                "counts": counts,
            },
            key=str(subject_key),
        )


@on_action("gdpr.owner.probe")
def handle_owner_probe(event):
    """Answer the liveness probe with what this owner claims.

    Deliberately in the same module (and the same process) as the erasure
    handler above: gdpr's ``W006`` reads these answers to name owners whose
    consumer was never deployed, and an answer from anywhere else would
    make that check lie.
    """
    from stapel_core.comm import emit

    from .erasure import OWNER, SUBJECT_TYPES

    payload = event.payload or {}
    answer = {"owner": OWNER, "subject_types": list(SUBJECT_TYPES)}
    correlation_id = payload.get("correlation_id")
    if correlation_id:
        answer["correlation_id"] = str(correlation_id)
    emit("gdpr.owner.alive", answer, key=OWNER)


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase a user's docs slice (GDPR Art. 17) — the pre-0.5.0 account
    path, now routed through the same :func:`stapel_docs.erasure.erase`.

    Anonymize semantics (documents are co-produced workspace content and
    survive their authors); no receipt is emitted here, because the account
    erasure that carries a correlation_id arrives as
    ``gdpr.erasure.requested`` and is receipted there — two receipts for
    one request would be noise, and this event fires alongside it."""
    from .erasure import erase

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    erase("account", user_id)


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
            )
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
        moved["Star"] = _fold_stars(from_user_id, into_user_id)
        moved["RecentEntry"] = _fold_recents(from_user_id, into_user_id)

    logger.info(
        "user.merged %s -> %s: docs authorship and per-user state carried over (%s)",
        from_user_id, into_user_id, moved,
    )


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
    "handle_erasure_requested",
    "handle_owner_probe",
    "handle_user_deleted",
    "handle_user_merged",
    "wire_ingest",
]
