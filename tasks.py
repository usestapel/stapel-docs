"""Scheduled work of stapel-docs — retention that actually runs.

A retention policy nobody schedules is a promise, not a mechanism (security
audit DOCS-02): ``TRASH_RETENTION_DAYS`` and the ``docs_purge_expired``
command existed, but nothing ran them, so soft-deleted documents lived
forever. This module is the schedulable form.

Celery is OPTIONAL here. :func:`purge_expired_trash` is a plain callable
that a cron, a systemd timer or any scheduler can invoke; when celery is
installed it is also registered as a shared task under the stable name
``stapel_docs.tasks.purge_expired_trash``.

Wire it into a host's beat schedule:

    from stapel_docs.tasks import get_docs_beat_schedule

    CELERY_BEAT_SCHEDULE = {
        **get_docs_beat_schedule(),
        ...
    }

The cadence is configuration (``STAPEL_DOCS["TRASH_PURGE_SCHEDULE"]``, a
crontab kwargs dict), not a literal, and ``checks.py`` warns when celery is
installed but nothing in the schedule points at the task — a silent
non-running retention job is exactly the state the audit found.

Since 0.7.0 the same schedulable form carries the crdt idle-assembly sweep
(:func:`assemble_idle_crdt_snapshots`, cadence
``STAPEL_DOCS["CRDT_ASSEMBLE_SCHEDULE"]``): active documents assemble
opportunistically from ``append_updates``; the sweep folds the journal
tails that go quiet below the interval.
"""
import logging

logger = logging.getLogger(__name__)

#: The names a beat schedule must reference (stable across refactors).
PURGE_TASK_NAME = "stapel_docs.tasks.purge_expired_trash"
ASSEMBLE_TASK_NAME = "stapel_docs.tasks.assemble_idle_crdt_snapshots"


def purge_expired_trash() -> dict:
    """Purge everything soft-deleted longer than TRASH_RETENTION_DAYS ago.

    Returns the counts it destroyed and logs them: retention that runs
    invisibly cannot be monitored, and a job nobody can observe is
    indistinguishable from a job that stopped running.
    """
    from .services import purge_expired

    folders, documents = purge_expired()
    logger.info(
        "docs retention purge: %s document(s), %s folder(s)", documents, folders
    )
    return {"documents": documents, "folders": folders}


def assemble_idle_crdt_snapshots() -> dict:
    """Assemble every yjs-codec document whose journal went idle.

    The interval trigger in ``append_updates`` covers active documents; a
    burst that stops below the interval leaves a journal tail nothing would
    ever fold. This sweep folds documents whose NEWEST journal row past the
    snapshot is at least ``CRDT_ASSEMBLE_IDLE_SECONDS`` old — a document
    still being typed into is left alone (its own appends will trigger, or
    a later sweep will catch it once quiet).

    Same observability rule as the purge: counts are returned and logged,
    because a background job nobody can observe is indistinguishable from
    one that stopped running.
    """
    from datetime import timedelta

    from django.db.models import F, Max
    from django.utils import timezone

    from . import services
    from .conf import docs_settings
    from .models import DocumentUpdate

    idle = int(docs_settings.CRDT_ASSEMBLE_IDLE_SECONDS)
    cutoff = timezone.now() - timedelta(seconds=idle)
    pending = (
        DocumentUpdate.objects.filter(
            seq__gt=F("document__snapshot_seq"),
            document__deleted_at__isnull=True,
        )
        .values("document_id")
        .annotate(newest=Max("created_at"))
        .filter(newest__lte=cutoff)
    )
    assembled = 0
    for row in pending:
        # No-ops (host-codec crdt types, races with a concurrent assembly)
        # answer None and are not counted — the count is folds, not visits.
        if services.assemble_crdt_snapshot(row["document_id"]) is not None:
            assembled += 1
    logger.info("docs crdt idle assembly: %s document(s)", assembled)
    return {"documents": assembled}


def get_docs_beat_schedule() -> dict:
    """Beat entries for the retention purge and the crdt idle-assembly
    sweep, each on its configured cadence."""
    from celery.schedules import crontab

    from .conf import docs_settings

    purge_schedule = dict(docs_settings.TRASH_PURGE_SCHEDULE or {})
    assemble_schedule = dict(docs_settings.CRDT_ASSEMBLE_SCHEDULE or {})
    return {
        "docs-trash-retention-purge": {
            "task": PURGE_TASK_NAME,
            "schedule": crontab(**purge_schedule),
        },
        "docs-crdt-idle-assembly": {
            "task": ASSEMBLE_TASK_NAME,
            "schedule": crontab(**assemble_schedule),
        },
    }


try:  # pragma: no cover — exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    purge_expired_trash = shared_task(name=PURGE_TASK_NAME)(purge_expired_trash)
    assemble_idle_crdt_snapshots = shared_task(name=ASSEMBLE_TASK_NAME)(
        assemble_idle_crdt_snapshots
    )


__all__ = [
    "ASSEMBLE_TASK_NAME",
    "PURGE_TASK_NAME",
    "assemble_idle_crdt_snapshots",
    "get_docs_beat_schedule",
    "purge_expired_trash",
]
