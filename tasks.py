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
"""
import logging

logger = logging.getLogger(__name__)

#: The name a beat schedule must reference (stable across refactors).
PURGE_TASK_NAME = "stapel_docs.tasks.purge_expired_trash"


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


def get_docs_beat_schedule() -> dict:
    """Beat entry for the retention purge, on the configured cadence."""
    from celery.schedules import crontab

    from .conf import docs_settings

    schedule = dict(docs_settings.TRASH_PURGE_SCHEDULE or {})
    return {
        "docs-trash-retention-purge": {
            "task": PURGE_TASK_NAME,
            "schedule": crontab(**schedule),
        },
    }


try:  # pragma: no cover — exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    purge_expired_trash = shared_task(name=PURGE_TASK_NAME)(purge_expired_trash)


__all__ = ["PURGE_TASK_NAME", "purge_expired_trash", "get_docs_beat_schedule"]
