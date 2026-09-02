"""Server-side snapshot assembly (0.7.0) — a MATERIALIZATION, not a mutation.

Invariant: the stored snapshot equals the fold of updates 1..snapshot_seq.
Assembly introduces no operations, so it mints no seq — ``head_seq`` never
moves; ``snapshot_seq`` catches up to it.
"""
import uuid
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

pycrdt = pytest.importorskip("pycrdt")

from stapel_docs import crdt, services  # noqa: E402
from stapel_docs.models import Document, DocumentUpdate, Revision  # noqa: E402

pytestmark = pytest.mark.django_db


def _update(base: bytes, text: str) -> tuple[bytes, bytes]:
    """(diff to append, new full state) — one client keystroke batch."""
    doc = pycrdt.Doc()
    if base:
        doc.apply_update(base)
    else:
        doc["content"] = pycrdt.Text()
    content = doc.get("content", type=pycrdt.Text)
    content += text
    return doc.get_update(), doc.get_update()


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


@pytest.fixture
def workspace_id():
    return uuid.uuid4()


@pytest.fixture
def ymd(workspace_id):
    return services.create_document(workspace_id=workspace_id, type="ymd", title="Live")


def _append_text(document, text):
    diff, _ = _update(b"", text)
    return services.append_updates(document.pk, [diff])


def test_assembly_materializes_the_journal(ymd):
    _append_text(ymd, "hello ")
    _append_text(ymd, "world")
    ymd.refresh_from_db()
    assert ymd.head_seq == 2
    assert ymd.snapshot_seq == 0

    result = services.assemble_crdt_snapshot(ymd.pk)
    assert result == 2

    ymd.refresh_from_db()
    assert ymd.head_seq == 2  # assembly mints no seq
    assert ymd.snapshot_seq == 2
    assert ymd.snapshot_key
    assert ymd.size_bytes > 0

    body, mime, head_seq = services.read_content(ymd)
    assert head_seq == 2
    assert "hello" in crdt.extract_text(body)
    assert "world" in crdt.extract_text(body)


def test_assembly_mints_an_auto_revision_at_head(ymd):
    _append_text(ymd, "a")
    services.assemble_crdt_snapshot(ymd.pk)
    revision = Revision.objects.get(document=ymd)
    assert revision.kind == Revision.KIND_AUTO
    assert revision.seq == 1


def test_second_run_is_a_no_op(ymd):
    _append_text(ymd, "a")
    assert services.assemble_crdt_snapshot(ymd.pk) == 1
    assert services.assemble_crdt_snapshot(ymd.pk) is None
    ymd.refresh_from_db()
    assert ymd.head_seq == 1
    assert ymd.snapshot_seq == 1


def test_assembly_folds_onto_the_previous_snapshot(ymd):
    _append_text(ymd, "one ")
    services.assemble_crdt_snapshot(ymd.pk)
    _append_text(ymd, "two")
    assert services.assemble_crdt_snapshot(ymd.pk) == 2
    ymd.refresh_from_db()
    body, _, _ = services.read_content(ymd)
    text = crdt.extract_text(body)
    assert "one" in text and "two" in text


def test_assembly_fires_compaction(ymd):
    """After assembly at seq S, journal rows at seq <= S - REPLAY_WINDOW die
    — the same chat-pattern window every snapshot save applies."""
    for i in range(5):
        _append_text(ymd, f"w{i} ")
    with override_settings(STAPEL_DOCS={"REPLAY_WINDOW": 2}):
        services.assemble_crdt_snapshot(ymd.pk)
    remaining = list(
        DocumentUpdate.objects.filter(document=ymd).values_list("seq", flat=True)
    )
    assert remaining == [4, 5]


def test_snapshot_type_documents_are_untouched(workspace_id):
    doc = services.create_document(
        workspace_id=workspace_id, type="md", title="Plain", body=b"# text"
    )
    assert services.assemble_crdt_snapshot(doc.pk) is None
    fresh = Document.objects.get(pk=doc.pk)
    assert fresh.head_seq == doc.head_seq
    assert fresh.snapshot_key == doc.snapshot_key


def test_host_codec_crdt_documents_are_untouched(workspace_id):
    """Assembly is a yjs-codec mechanism: a host crdt type with its own
    codec keeps its journal — the server cannot fold what it cannot parse."""
    from stapel_docs.doc_types import (
        COLLAB_CRDT,
        DocTypeSpec,
        register_doc_type,
        unregister_doc_type,
    )

    register_doc_type(DocTypeSpec(slug="hostcrdt", label="H", collab=COLLAB_CRDT))
    try:
        doc = services.create_document(
            workspace_id=workspace_id, type="hostcrdt", title="H"
        )
        services.append_updates(doc.pk, [b"opaque"])
        assert services.assemble_crdt_snapshot(doc.pk) is None
        doc.refresh_from_db()
        assert doc.snapshot_seq == 0
        assert DocumentUpdate.objects.filter(document=doc).count() == 1
    finally:
        unregister_doc_type("hostcrdt")


def test_assembly_emits_updated_and_storage_delta(ymd, monkeypatch):
    """Assembly IS the debounce point: journal appends stay silent, the
    materialization announces the document once."""
    emitted = []
    monkeypatch.setattr(
        "stapel_docs.events.emit", lambda name, payload: emitted.append((name, payload))
    )
    _append_text(ymd, "a")
    services.assemble_crdt_snapshot(ymd.pk)
    names = [name for name, _ in emitted]
    assert "document.updated" in names
    assert "document.storage_changed" in names
    updated = dict(emitted)[("document.updated")]
    assert updated["head_seq"] == 1


def test_interval_trigger_schedules_assembly(ymd, django_capture_on_commit_callbacks):
    """When the journal outruns CRDT_ASSEMBLE_UPDATE_INTERVAL, the append
    schedules an assembly on commit (the repo's opportunistic-work canon is
    inline — same as recents trimming)."""
    with override_settings(STAPEL_DOCS={"CRDT_ASSEMBLE_UPDATE_INTERVAL": 2}):
        with django_capture_on_commit_callbacks(execute=True):
            _append_text(ymd, "a")
        ymd.refresh_from_db()
        assert ymd.snapshot_seq == 0  # below the interval: nothing assembled
        with django_capture_on_commit_callbacks(execute=True):
            _append_text(ymd, "b")
    ymd.refresh_from_db()
    assert ymd.snapshot_seq == 2
    assert ymd.head_seq == 2


def test_idle_sweep_assembles_stale_journals(ymd, workspace_id):
    from stapel_docs.tasks import assemble_idle_crdt_snapshots

    _append_text(ymd, "stale")
    fresh = services.create_document(workspace_id=workspace_id, type="ymd", title="F")
    _append_text(fresh, "fresh")
    DocumentUpdate.objects.filter(document=ymd).update(
        created_at=timezone.now() - timedelta(seconds=600)
    )

    result = assemble_idle_crdt_snapshots()

    assert result == {"documents": 1}
    ymd.refresh_from_db()
    fresh.refresh_from_db()
    assert ymd.snapshot_seq == ymd.head_seq == 1
    assert fresh.snapshot_seq == 0  # still being typed into — left alone


def test_idle_sweep_is_scheduled_by_the_beat_builder(monkeypatch):
    import sys
    import types

    calls = []

    def crontab(**kwargs):
        calls.append(kwargs)
        return ("crontab", tuple(sorted(kwargs.items())))

    stub = types.ModuleType("celery.schedules")
    stub.crontab = crontab
    monkeypatch.setitem(sys.modules, "celery", types.ModuleType("celery"))
    monkeypatch.setitem(sys.modules, "celery.schedules", stub)

    from stapel_docs.tasks import ASSEMBLE_TASK_NAME, get_docs_beat_schedule

    with override_settings(
        STAPEL_DOCS={"CRDT_ASSEMBLE_SCHEDULE": {"minute": "*/7"}}
    ):
        schedule = get_docs_beat_schedule()

    entry = schedule["docs-crdt-idle-assembly"]
    assert entry["task"] == ASSEMBLE_TASK_NAME
    assert {"minute": "*/7"} in calls


def test_interval_at_or_above_replay_window_warns():
    from stapel_docs.checks import check_crdt_assembly_window

    with override_settings(
        STAPEL_DOCS={"CRDT_ASSEMBLE_UPDATE_INTERVAL": 500, "REPLAY_WINDOW": 500}
    ):
        assert [w.id for w in check_crdt_assembly_window(None)] == ["stapel_docs.W033"]
    with override_settings(
        STAPEL_DOCS={"CRDT_ASSEMBLE_UPDATE_INTERVAL": 200, "REPLAY_WINDOW": 500}
    ):
        assert check_crdt_assembly_window(None) == []
