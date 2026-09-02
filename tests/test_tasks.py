"""Scheduled retention (audit DOCS-02): the purge has a runner and a cron.

TRASH_RETENTION_DAYS and the docs_purge_expired command existed, but
nothing ran them — soft-deleted documents lived forever while the config
claimed a 30-day retention. These pin the schedulable form.
"""
import uuid
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_docs import services
from stapel_docs.models import Document
from stapel_docs.tasks import PURGE_TASK_NAME, purge_expired_trash

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


def _trashed_days_ago(workspace_id, days):
    doc = services.create_document(
        workspace_id=workspace_id, type="txt", title=f"old-{days}", body=b"x"
    )
    Document.objects.filter(pk=doc.pk).update(
        deleted_at=timezone.now() - timedelta(days=days)
    )
    return doc


def test_purge_task_destroys_only_expired_trash():
    ws = uuid.uuid4()
    expired = _trashed_days_ago(ws, 60)
    recent = _trashed_days_ago(ws, 1)

    result = purge_expired_trash()

    assert result == {"documents": 1, "folders": 0}
    assert not Document.objects.filter(pk=expired.pk).exists()
    assert Document.objects.filter(pk=recent.pk).exists()


def test_retention_window_is_configuration():
    ws = uuid.uuid4()
    doc = _trashed_days_ago(ws, 5)

    with override_settings(STAPEL_DOCS={"TRASH_RETENTION_DAYS": 90}):
        assert purge_expired_trash()["documents"] == 0
    with override_settings(STAPEL_DOCS={"TRASH_RETENTION_DAYS": 1}):
        assert purge_expired_trash()["documents"] == 1
    assert not Document.objects.filter(pk=doc.pk).exists()


def test_beat_schedule_points_at_the_task_on_the_configured_cadence(monkeypatch):
    """celery is not a dependency of this module, so the schedule builder is
    exercised against a stand-in for celery.schedules.crontab — what is
    pinned here is our wiring: the task name and the configured cadence."""
    import sys
    import types

    captured = []

    def crontab(**kwargs):
        captured.append(kwargs)
        return ("crontab", kwargs)

    stub = types.ModuleType("celery.schedules")
    stub.crontab = crontab
    monkeypatch.setitem(sys.modules, "celery", types.ModuleType("celery"))
    monkeypatch.setitem(sys.modules, "celery.schedules", stub)

    from stapel_docs.tasks import ASSEMBLE_TASK_NAME, get_docs_beat_schedule

    with override_settings(STAPEL_DOCS={"TRASH_PURGE_SCHEDULE": {"hour": 2, "minute": 5}}):
        schedule = get_docs_beat_schedule()

    entry = schedule["docs-trash-retention-purge"]
    assert entry["task"] == PURGE_TASK_NAME
    assert {"hour": 2, "minute": 5} in captured
    # The 0.7.0 sweep rides the same builder on its own cadence.
    assert schedule["docs-crdt-idle-assembly"]["task"] == ASSEMBLE_TASK_NAME
    assert {"minute": "*/5"} in captured


def test_unscheduled_retention_is_reported_by_a_system_check():
    """A host that drives a beat schedule without the purge in it is told."""
    from stapel_docs.checks import check_retention_is_scheduled

    with override_settings(CELERY_BEAT_SCHEDULE={}):
        assert [w.id for w in check_retention_is_scheduled(None)] == ["stapel_docs.W030"]

    with override_settings(
        CELERY_BEAT_SCHEDULE={"x": {"task": PURGE_TASK_NAME, "schedule": 1}}
    ):
        assert check_retention_is_scheduled(None) == []
