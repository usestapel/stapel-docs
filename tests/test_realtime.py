"""The docs realtime stream (0.7.0): store-first delivery + the doc socket.

REST append is the write path; the stream is delivery only. Frames go out
per journal row from ``transaction.on_commit`` — a subscriber that misses
one replays the durable row, which is why the transport may be best-effort.
The socket authorizes through the SAME choke point HTTP uses
(``authz.authorize`` with the document), so a whitelist grantee works over
the socket exactly as over HTTP.
"""
import base64
import uuid

import pytest
from django.test import override_settings

pycrdt = pytest.importorskip("pycrdt")
pytest.importorskip("channels")

from channels.db import database_sync_to_async  # noqa: E402
from stapel_realtime import envelope as wire  # noqa: E402
from stapel_realtime.testing import open_stream  # noqa: E402

from stapel_docs import realtime, services  # noqa: E402
from stapel_docs.consumers import DocUpdatesConsumer  # noqa: E402

API = "/docs/api/v1"


def _y_update(text: str) -> bytes:
    doc = pycrdt.Doc()
    doc["content"] = pycrdt.Text()
    doc.get("content", type=pycrdt.Text).insert(0, text)
    return doc.get_update()


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


@pytest.fixture
def workspace_id():
    return uuid.uuid4()


def _make_user(name="u"):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username=f"{name}-{uuid.uuid4().hex[:8]}")


# ── stream key + payload shapes ──────────────────────────────────────


def test_doc_stream_key_is_canonical():
    doc_id = uuid.uuid4()
    assert realtime.doc_stream(doc_id) == f"docs:doc:{doc_id}"


def test_update_payload_shape():
    author = uuid.uuid4()
    payload = realtime.update_payload(b"\x01\x02", author, "c-1")
    assert payload == {
        "update": base64.b64encode(b"\x01\x02").decode("ascii"),
        "author_id": str(author),
        "client_id": "c-1",
    }
    assert realtime.update_payload(b"x", None, "")["author_id"] is None


def test_deliver_frame_survives_a_missing_substrate(monkeypatch):
    """ImportError = debug log + False, never a hard dependency."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("stapel_realtime"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert realtime._deliver_frame("docs:doc:x", {"a": 1}, seq=1) is False


# ── delivery on append (store first, tell the socket second) ─────────


@pytest.mark.django_db
def test_append_delivers_one_frame_per_journal_row(
    workspace_id, monkeypatch, django_capture_on_commit_callbacks
):
    delivered = []
    monkeypatch.setattr(
        realtime,
        "_deliver_frame",
        lambda stream, payload, seq: delivered.append((stream, payload, seq)) or True,
    )
    document = services.create_document(
        workspace_id=workspace_id, type="ymd", title="Live"
    )
    first, second = _y_update("a"), _y_update("b")
    with django_capture_on_commit_callbacks(execute=True):
        services.append_updates(document.pk, [first, second], client_id="c-9")

    assert [seq for _, _, seq in delivered] == [1, 2]
    stream, payload, _ = delivered[0]
    assert stream == f"docs:doc:{document.pk}"
    assert base64.b64decode(payload["update"]) == first
    assert payload["client_id"] == "c-9"
    assert payload["author_id"] is None  # no principal on this call


@pytest.mark.django_db
def test_duplicate_client_batch_delivers_nothing(
    workspace_id, monkeypatch, django_capture_on_commit_callbacks
):
    delivered = []
    monkeypatch.setattr(
        realtime,
        "_deliver_frame",
        lambda stream, payload, seq: delivered.append(seq) or True,
    )
    document = services.create_document(
        workspace_id=workspace_id, type="ymd", title="Live"
    )
    batch = {"client_id": "c-1", "client_seq": 1}
    with django_capture_on_commit_callbacks(execute=True):
        services.append_updates(document.pk, [_y_update("a")], **batch)
    with django_capture_on_commit_callbacks(execute=True):
        services.append_updates(document.pk, [_y_update("a")], **batch)
    assert delivered == [1]


# ── the consumer ─────────────────────────────────────────────────────


async def _open(document_id, user):
    return await open_stream(
        DocUpdatesConsumer,
        f"/ws/docs/{document_id}",
        user=user,
        url_kwargs={"document_id": str(document_id)},
        expect_accept=False,
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_member_connects_and_resumes(workspace_id, grant_capabilities):
    def _setup():
        user = _make_user("member")
        grant_capabilities(workspace_id, user.pk)
        document = services.create_document(
            workspace_id=workspace_id, type="ymd", title="Live"
        )
        services.append_updates(document.pk, [_y_update("a"), _y_update("b")])
        return user, document

    user, document = await database_sync_to_async(_setup)()
    sock = await _open(document.pk, user)
    assert sock.connected

    welcome = await sock.hello(last_seq=0)
    assert welcome.payload["server_seq"] == 2
    first = await sock.expect(wire.REPLAY)
    assert first.seq == 1
    assert base64.b64decode(first.payload["update"])  # the row's bytes
    assert "author_id" in first.payload and "client_id" in first.payload
    second = await sock.expect(wire.REPLAY)
    assert second.seq == 2
    done = await sock.expect(wire.REPLAY_DONE)
    assert done.payload["up_to_seq"] == 2
    await sock.close()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_live_frame_reaches_an_open_socket(workspace_id, grant_capabilities):
    def _setup():
        user = _make_user("member")
        grant_capabilities(workspace_id, user.pk)
        document = services.create_document(
            workspace_id=workspace_id, type="ymd", title="Live"
        )
        return user, document

    user, document = await database_sync_to_async(_setup)()
    sock = await _open(document.pk, user)
    assert sock.connected

    payload = _y_update("live!")
    await database_sync_to_async(services.append_updates)(document.pk, [payload])

    frame = await sock.receive(timeout=3)
    assert frame.type == wire.LIVE
    assert frame.seq == 1
    assert frame.stream == f"docs:doc:{document.pk}"
    assert base64.b64decode(frame.payload["update"]) == payload
    await sock.close()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stranger_is_refused_4403(workspace_id):
    def _setup():
        stranger = _make_user("stranger")
        document = services.create_document(
            workspace_id=workspace_id, type="ymd", title="Private"
        )
        return stranger, document

    stranger, document = await database_sync_to_async(_setup)()
    sock = await _open(document.pk, stranger)
    assert not sock.connected
    assert sock.close_code == 4403


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_unauthenticated_is_refused_4401(workspace_id):
    def _setup():
        return services.create_document(
            workspace_id=workspace_id, type="ymd", title="Private"
        )

    document = await database_sync_to_async(_setup)()
    sock = await _open(document.pk, None)
    assert not sock.connected
    assert sock.close_code == 4401


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_trashed_document_is_refused(workspace_id, grant_capabilities):
    def _setup():
        user = _make_user("member")
        grant_capabilities(workspace_id, user.pk)
        document = services.create_document(
            workspace_id=workspace_id, type="ymd", title="Gone"
        )
        services.trash_document(document)
        return user, document

    user, document = await database_sync_to_async(_setup)()
    sock = await _open(document.pk, user)
    assert not sock.connected
    assert sock.close_code == 4403


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_whitelist_grantee_connects_over_the_socket(workspace_id):
    """Grants must work over the socket exactly as over HTTP — the consumer
    asks the SAME choke point, with the document."""
    def _setup():
        sharer = _make_user("sharer")
        grantee = _make_user("grantee")
        document = services.create_document(
            workspace_id=workspace_id, type="ymd", title="Shared"
        )
        services.grant_access(
            document,
            subject_kind="user",
            user_id=grantee.pk,
            level="view",
            granted_by=sharer,
        )
        return grantee, document

    with override_settings(STAPEL_DOCS={"SHARING": {"MODES": ["whitelist"]}}):
        grantee, document = await database_sync_to_async(_setup)()
        sock = await _open(document.pk, grantee)
        assert sock.connected
        welcome = await sock.hello(last_seq=0)
        assert welcome.payload["server_seq"] == 0
        await sock.close()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_revoking_a_grant_kicks_the_open_socket(workspace_id):
    def _setup():
        sharer = _make_user("sharer")
        grantee = _make_user("grantee")
        document = services.create_document(
            workspace_id=workspace_id, type="ymd", title="Shared"
        )
        row = services.grant_access(
            document,
            subject_kind="user",
            user_id=grantee.pk,
            level="view",
            granted_by=sharer,
        )
        return grantee, document, row

    with override_settings(STAPEL_DOCS={"SHARING": {"MODES": ["whitelist"]}}):
        grantee, document, row = await database_sync_to_async(_setup)()
        sock = await _open(document.pk, grantee)
        assert sock.connected

        await database_sync_to_async(services.revoke_access)(document, row.id)

        kick = await sock.receive(timeout=3)
        assert kick.type == wire.KICK
        assert await sock.wait_closed() == 4410


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_routing_resolves_the_real_path_to_the_consumer(
    workspace_id, grant_capabilities
):
    from channels.routing import URLRouter

    from stapel_docs.routing import websocket_urlpatterns

    def _setup():
        user = _make_user("member")
        grant_capabilities(workspace_id, user.pk)
        document = services.create_document(
            workspace_id=workspace_id, type="ymd", title="Routed"
        )
        services.append_updates(document.pk, [_y_update("r")])
        return user, document

    user, document = await database_sync_to_async(_setup)()

    class _InjectUser:
        """Stand-in for the G14 middleware: stamps the user on the scope."""

        def __init__(self, inner, scope_user):
            self.inner = inner
            self.scope_user = scope_user

        async def __call__(self, scope, receive, send):
            scope = dict(scope)
            scope["user"] = self.scope_user
            return await self.inner(scope, receive, send)

    from channels.testing.websocket import WebsocketCommunicator
    from stapel_realtime.testing import StreamClient

    comm = WebsocketCommunicator(
        _InjectUser(URLRouter(websocket_urlpatterns), user),
        f"/ws/docs/{document.pk}",
    )
    connected, _ = await comm.connect()
    assert connected
    sock = StreamClient(comm)
    welcome = await sock.hello(last_seq=0)
    assert welcome.payload["server_seq"] == 1
    assert (await sock.expect(wire.REPLAY)).seq == 1
    await sock.expect(wire.REPLAY_DONE)
    await comm.disconnect()


# ── presenter: socket_path on the document envelope ──────────────────


@pytest.mark.django_db
def test_socket_path_present_when_realtime_is_served(workspace_id, monkeypatch):
    from stapel_docs.presenters import get_document_presenter

    document = services.create_document(
        workspace_id=workspace_id, type="md", title="Doc"
    )
    monkeypatch.setattr(realtime, "socket_available", lambda: True)
    envelope = get_document_presenter().present(document)
    assert envelope.socket_path == f"ws/docs/{document.pk}"

    monkeypatch.setattr(realtime, "socket_available", lambda: False)
    envelope = get_document_presenter().present(document)
    assert envelope.socket_path is None


@pytest.mark.django_db
def test_socket_path_is_null_in_a_polling_only_host(workspace_id):
    """This harness does not install stapel_realtime as an app — the honest
    envelope answer is null, and polling stays first-class."""
    from stapel_docs.presenters import get_document_presenter

    document = services.create_document(
        workspace_id=workspace_id, type="md", title="Doc"
    )
    assert get_document_presenter().present(document).socket_path is None


# ── checks ───────────────────────────────────────────────────────────


def test_installed_extra_without_the_app_warns():
    from stapel_docs.checks import check_realtime_wiring

    # stapel_realtime is importable in this venv but not in INSTALLED_APPS.
    assert [w.id for w in check_realtime_wiring(None)] == ["stapel_docs.W034"]


def test_no_warning_when_the_app_is_installed(monkeypatch):
    from django.apps import apps

    from stapel_docs.checks import check_realtime_wiring

    monkeypatch.setattr(apps, "is_installed", lambda name: True)
    assert check_realtime_wiring(None) == []


def test_no_warning_when_the_substrate_is_absent(monkeypatch):
    import stapel_docs.checks as checks_module

    monkeypatch.setattr(
        checks_module, "_realtime_importable", lambda: False
    )
    assert checks_module.check_realtime_wiring(None) == []
