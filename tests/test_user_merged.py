"""``user.merged``: a guest folded into an existing account keeps authorship.

The other half of the account life cycle ``user.deleted`` already answered,
and the opposite instruction. Erasure ANONYMIZES the authorship columns
("nobody wrote this any more"); a merge RE-PARENTS them ("somebody else
did"). A module that answers only the first leaves a guest's documents owned
by an id that can no longer sign in — never listed for the survivor and never
erased either, because no erasure is ever requested for an account that was
merged rather than closed.

Pinned here: the rows move, a redelivery moves nothing further, every
malformed payload is ACKed instead of poisoning the bus, and an event naming
users this deployment has no rows for does nothing.
"""
import uuid
from types import SimpleNamespace

import pytest

from stapel_docs.actions import MergeTargetNotReady, handle_user_merged
from stapel_docs.models import (
    Document,
    DocumentUpdate,
    Folder,
    Revision,
    UploadSession,
)

pytestmark = pytest.mark.django_db

BAD_IDS = ["not-a-uuid", "", "  ", "['x']"]


def _user(username):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="x"
    )


@pytest.fixture
def guest(db):
    return _user("guest")


@pytest.fixture
def survivor(db):
    return _user("survivor")


def _event(**payload):
    return SimpleNamespace(payload=payload, event_id=str(uuid.uuid4()))


def _corpus(user, title="Guest notes"):
    """One of every authorship-bearing row, all pointing at *user*."""
    ws = uuid.uuid4()
    folder = Folder.objects.create(workspace_id=ws, name=f"Root {uuid.uuid4()}", created_by=user)
    doc = Document.objects.create(
        workspace_id=ws, folder=folder, type="md", title=title, owner=user
    )
    update = DocumentUpdate.objects.create(
        document=doc, seq=1, payload=b"crdt-insert: user text", author_id=user.pk
    )
    revision = Revision.objects.create(
        document=doc, seq=1, storage_key=f"docs/{ws}/{doc.id}/h.snap", created_by=user
    )
    session = UploadSession.objects.create(
        workspace_id=ws, title="up", key=f"docs/{ws}/staging", created_by=user
    )
    return doc, folder, update, revision, session


def _snapshot():
    return (
        sorted(Document.objects.values_list("id", "owner_id")),
        sorted(Folder.objects.values_list("id", "created_by_id")),
        sorted(Revision.objects.values_list("id", "created_by_id")),
        sorted(DocumentUpdate.objects.values_list("id", "author_id")),
        sorted(UploadSession.objects.values_list("id", "created_by_id")),
    )


class TestHappyPath:
    def test_every_authorship_column_is_re_parented(self, guest, survivor):
        doc, folder, update, revision, session = _corpus(guest)

        handle_user_merged(
            _event(from_user_id=str(guest.pk), into_user_id=str(survivor.pk))
        )

        for row in (doc, folder, update, revision, session):
            row.refresh_from_db()
        assert doc.owner_id == survivor.pk
        assert folder.created_by_id == survivor.pk
        assert revision.created_by_id == survivor.pk
        assert update.author_id == survivor.pk
        assert session.created_by_id == survivor.pk

    def test_the_content_itself_is_untouched(self, guest, survivor):
        doc, _folder, update, *_ = _corpus(guest)

        handle_user_merged(_event(from_user_id=guest.pk, into_user_id=survivor.pk))

        update.refresh_from_db()
        assert Document.objects.filter(id=doc.id).exists()
        assert bytes(update.payload) == b"crdt-insert: user text"

    def test_a_third_partys_authorship_is_untouched(self, guest, survivor):
        third = _user("third")
        theirs, *_ = _corpus(third, title="Their notes")
        _corpus(guest)

        handle_user_merged(_event(from_user_id=guest.pk, into_user_id=survivor.pk))

        theirs.refresh_from_db()
        assert theirs.owner_id == third.pk

    def test_the_survivors_own_rows_stay_theirs(self, guest, survivor):
        theirs, *_ = _corpus(survivor, title="Their notes")
        _corpus(guest)

        handle_user_merged(_event(from_user_id=guest.pk, into_user_id=survivor.pk))

        theirs.refresh_from_db()
        assert theirs.owner_id == survivor.pk
        assert Document.objects.filter(owner_id=survivor.pk).count() == 2


class TestIdempotency:
    def test_a_redelivery_changes_nothing_further(self, guest, survivor):
        _corpus(guest)
        event = _event(from_user_id=guest.pk, into_user_id=survivor.pk)

        handle_user_merged(event)
        after_first = _snapshot()

        handle_user_merged(event)
        handle_user_merged(event)

        assert _snapshot() == after_first


class TestPoisonPayloads:
    """A raise here is a poison pill: the bus redelivers a payload no retry
    can repair. ``not-a-uuid`` is the one that bites — Django answers an
    uncoercible UUID with ``ValidationError``, which is NOT a ``ValueError``.
    """

    def test_a_malformed_from_id_acks_and_moves_nothing(self, guest, survivor):
        _corpus(guest)
        before = _snapshot()
        for bad in BAD_IDS:
            handle_user_merged(_event(from_user_id=bad, into_user_id=str(survivor.pk)))
        assert _snapshot() == before

    def test_a_malformed_into_id_acks_and_moves_nothing(self, guest, survivor):
        _corpus(guest)
        before = _snapshot()
        for bad in BAD_IDS:
            handle_user_merged(_event(from_user_id=str(guest.pk), into_user_id=bad))
        assert _snapshot() == before

    def test_a_missing_id_acks_and_moves_nothing(self, guest, survivor):
        _corpus(guest)
        before = _snapshot()
        handle_user_merged(_event())
        handle_user_merged(_event(from_user_id=str(guest.pk)))
        handle_user_merged(_event(into_user_id=str(survivor.pk)))
        assert _snapshot() == before

    def test_a_payload_that_is_not_a_mapping_acks(self, guest):
        handle_user_merged(SimpleNamespace(payload=None, event_id="evt-empty"))

    def test_a_self_merge_is_a_no_op(self, guest):
        doc, *_ = _corpus(guest)
        handle_user_merged(_event(from_user_id=guest.pk, into_user_id=guest.pk))
        doc.refresh_from_db()
        assert doc.owner_id == guest.pk


class TestUnknownUsers:
    def test_an_event_about_users_with_no_rows_here_does_nothing(self, survivor):
        stranger = _user("stranger")
        theirs, *_ = _corpus(survivor, title="Their notes")

        handle_user_merged(_event(from_user_id=stranger.pk, into_user_id=survivor.pk))

        theirs.refresh_from_db()
        assert theirs.owner_id == survivor.pk
        assert Document.objects.count() == 1

    def test_a_survivor_with_no_user_row_yet_is_retried_not_dropped(self, guest):
        """The guest HAS documents and no FK can point at a user that does not
        exist here yet. Returning success would let the outbox mark the event
        delivered and strand them, so the handler raises — the comm layer's
        retry signal."""
        doc, *_ = _corpus(guest)

        with pytest.raises(MergeTargetNotReady):
            handle_user_merged(
                _event(from_user_id=guest.pk, into_user_id=str(uuid.uuid4()))
            )

        doc.refresh_from_db()
        assert doc.owner_id == guest.pk


class TestLifecycleCheck:
    """stapel_core.lifecycle.E001 — an app that answers ``user.deleted`` and
    not ``user.merged`` is a system-check ERROR. Registered here so the pair
    cannot be broken by a later refactor without a red test."""

    def test_the_lifecycle_pair_check_is_green(self):
        from stapel_core.comm.lifecycle_checks import check_lifecycle_pairs

        assert check_lifecycle_pairs() == []
