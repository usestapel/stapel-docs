"""Action subscriptions of stapel-docs.

Handlers are idempotent-minded (delivery is at-least-once — outbox retries,
broker redelivery). Transport is chosen by ``STAPEL_COMM`` (in-process in a
monolith, bus consumer in microservices); the handler code is identical.

Two consumers live here:

- ``user.deleted`` → the GDPR provider's erasure (anonymize authorship,
  never destroy surviving workspace content — storage-verdict §3);
- the INGEST seam (design §2/§6): ``STAPEL_DOCS["INGEST"]`` maps
  ``{action_name: dotted-path mapper}`` so a host gets event-driven ingest
  without writing a subscriber. Docs never learns a foreign event schema —
  the mapper (host code) turns the payload into ``create_document`` kwargs.
"""
import logging
from typing import Callable

from django.core.exceptions import ImproperlyConfigured
from stapel_core.comm import on_action, subscribe_action

logger = logging.getLogger(__name__)


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase a user's docs slice (GDPR Art. 17). Anonymize semantics: the
    provider nulls authorship and keeps documents (idempotent — a nulled
    row nulls to itself on redelivery)."""
    from .gdpr import DocsGDPRProvider

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    DocsGDPRProvider().delete(user_id)
    logger.info("docs authorship anonymized for deleted user %s", user_id)


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


__all__ = ["handle_user_deleted", "wire_ingest"]
