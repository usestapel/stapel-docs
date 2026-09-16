"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns,
and the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body
it gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the
  family of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of
  substitution, naming the operation;
* the operations that genuinely cannot be driven in-process are listed by
  name in ``UNDRIVABLE`` with a one-line reason each. That list is asserted
  to be exactly current: a stale entry, or a missing reason, fails.

Runs on every interpreter: it reads the committed schema and never emits.
``tests/test_contract.py`` skips off Python 3.12 because it regenerates the
document and compares text; this file compares BODIES against a file that
is already on disk, so no interpreter can excuse it from running.

The urlconf below is the emission mount (``codegen_urls.py``): the module
root under ``docs/``, which the module's own ``urls.py`` turns into
``/docs/api/v1/…``.
"""
import base64
import copy
import io
import json
import re
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlencode

import jsonschema
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import include, path as url_path
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client.
urlpatterns = [
    url_path("docs/", include("stapel_docs.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/docs/api/v1"

#: A crdt document type, registered at runtime exactly as a host would.
#: No builtin type is crdt unless the ``[crdt]`` extra is installed, and the
#: update-journal endpoints are declared unconditionally — so the gate
#: brings its own rather than leaving two operations undriven on a machine
#: without pycrdt.
WIRE_CRDT_TYPE = "wirecrdt"

LINK_ON = {"SHARING": {"MODES": ["link"]}}
WHITELIST_ON = {"SHARING": {"MODES": ["whitelist"]}}
BOTH_ON = {"SHARING": {"MODES": ["whitelist", "link"]}}
#: The dev storage backend cannot sign a URL, so the presigned-URL
#: endpoints refuse by default (a permanent public link is a second read
#: path around authorize()). The host opt-in is what the declared 200 is
#: about, so the gate takes it.
UNEXPIRING = {"ALLOW_UNEXPIRING_DOWNLOAD_URLS": True}


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    """Keep the storage backend's files out of the checkout.

    ``MEDIA_ROOT`` is unset in the harness settings, so it defaults to the
    working directory and every upload/save writes under the repo root —
    where, under this package's flat layout, a stray directory can shadow a
    real module.
    """
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


@pytest.fixture(autouse=True)
def _runtime_doc_types():
    """The gate's crdt type never outlives the test that needed it."""
    from stapel_docs.doc_types import unregister_doc_type

    yield
    unregister_doc_type(WIRE_CRDT_TYPE)


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _blank_string_alternative(node):
    """A ``oneOf`` branch that means "or the empty string".

    ``CharField(allow_blank=True)`` with a pattern is emitted as
    ``oneOf: [{pattern: …}, {maxLength: 0}]``. The branches are disjoint
    only under format-ASSERTING semantics; a validator that treats them as
    alternatives is reading the document the way the emitter meant it.
    """
    branches = node.get("oneOf")
    if not isinstance(branches, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "string" and b.get("maxLength") == 0
        for b in branches
    )


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergences that matter here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type``;
    JSON Schema has no such keyword and would refuse the null. The second
    conversion is the blank-string ``oneOf`` above. Everything else
    drf-spectacular emits (``$ref``, ``allOf``, ``enum``, ``required``,
    ``readOnly``) is JSON Schema as written.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if _blank_string_alternative(rebuilt):
        rebuilt["anyOf"] = rebuilt.pop("oneOf")
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def make_user():
    return get_user_model().objects.create(username=_unique("wire-"))


def client_for(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


class Wire:
    """The state a recipe builds: members, documents, folders, uploads.

    One object per driven operation, holding the workspace the operation is
    performed in and the capability grants that make it authorized. The
    capability provider is the conftest test double (fail-closed: nothing is
    allowed until granted), so "an authorized member" is a thing this gate
    has to say out loud rather than inherit.
    """

    def __init__(self, grant):
        self._grant = grant
        self.workspace_id = uuid.uuid4()

    # ── principals ───────────────────────────────────────────────────────

    def member(self, *caps):
        """A user holding *caps* in this workspace (no caps = all of them)."""
        user = make_user()
        self._grant(self.workspace_id, user.pk, *caps)
        return user

    def actor(self, *caps):
        """An authenticated client for a member of this workspace."""
        return client_for(self.member(*caps))

    def outsider(self):
        """An authenticated account with no capability in this workspace."""
        return client_for(make_user())

    # ── objects ──────────────────────────────────────────────────────────

    def folder(self, client, name="Meetings", **extra):
        payload = {"workspace_id": str(self.workspace_id), "name": name}
        payload.update(extra)
        response = client.post(f"{V1}/folders", payload, format="json")
        assert response.status_code == 201, response.content
        return response.json()

    def document(self, client, type="md", title="Notes", body=None, **extra):
        payload = {
            "workspace_id": str(self.workspace_id),
            "type": type,
            "title": title,
        }
        if body is not None:
            payload["body"] = body
        payload.update(extra)
        response = client.post(f"{V1}/documents", payload, format="json")
        assert response.status_code == 201, response.content
        return response.json()

    def crdt_document(self, client, title="Live"):
        from stapel_docs.doc_types import COLLAB_CRDT, DocTypeSpec, register_doc_type

        register_doc_type(
            DocTypeSpec(
                slug=WIRE_CRDT_TYPE,
                label="Wire CRDT",
                collab=COLLAB_CRDT,
                editor_hint="wire",
            )
        )
        return self.document(client, type=WIRE_CRDT_TYPE, title=title)

    def save(self, client, document, body, expected_seq):
        """A content PUT under the optimistic lock — mints a revision."""
        with override_settings(STAPEL_DOCS={"AUTO_REVISION_INTERVAL_SECONDS": 0}):
            response = client.put(
                f"{V1}/documents/{document['id']}/content",
                data=body,
                content_type="text/markdown",
                HTTP_IF_MATCH=f'"{expected_seq}"',
            )
        assert response.status_code == 200, response.content
        return response.json()

    def uploaded(self, client, payload, mime="text/plain", title="report.txt"):
        """A finalized ``type=file`` document carrying *payload* bytes."""
        from stapel_docs.storage import get_storage

        ticket = self.upload_ticket(client, mime=mime, title=title)
        get_storage().put_bytes(ticket["key"], payload, content_type=mime)
        response = client.post(f"{V1}/uploads/{ticket['upload_id']}/finalize")
        assert response.status_code == 200, response.content
        return response.json()

    def upload_ticket(self, client, mime="text/plain", title="report.txt"):
        response = client.post(
            f"{V1}/uploads",
            {
                "workspace_id": str(self.workspace_id),
                "title": title,
                "mime_type": mime,
            },
            format="json",
        )
        assert response.status_code == 201, response.content
        return response.json()

    def zip_document(self, client):
        return self.uploaded(
            client, _zip_bytes(), mime="application/zip", title="bundle.zip"
        )

    def link(self, client, document):
        """A minted bearer link — the mode must be on for the mint to land."""
        response = client.post(
            f"{V1}/documents/{document['id']}/links", {}, format="json"
        )
        assert response.status_code == 201, response.content
        return response.json()

    def grant(self, client, document, subject):
        response = client.post(
            f"{V1}/documents/{document['id']}/access",
            {"subject_kind": "user", "user_id": str(subject.pk), "level": "view"},
            format="json",
        )
        assert response.status_code == 201, response.content
        return response.json()


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("readme.txt", b"hello from the archive\n")
        zf.writestr("src/main.py", b"print('hi')\n")
    return buf.getvalue()


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def __call__(self, client, params=None, data=None, query=None, **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        if query:
            url = f"{url}?{urlencode(query)}"
        send = getattr(client, self.method.lower())
        if self.method in ("GET", "DELETE"):
            return send(url, **extra)
        if "content_type" in extra:
            return send(url, data, **extra)
        return send(url, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template)``. Each recipe receives a ``Call``
#: bound to that operation and a fresh ``Wire``, and returns the response.
RECIPES = {}


def recipe(method, path):
    def register(fn):
        key = (method, V1 + path)
        assert key not in RECIPES, f"duplicate recipe for {method} {path}"
        RECIPES[key] = fn
        return fn

    return register


#: Operations that cannot be driven in-process, by name and with the reason.
#: EMPTY, and that is the finding: all 31 declared JSON responses are
#: reachable from a test client with nothing mocked but the workspace
#: capability provider the harness already stands in for.
UNDRIVABLE: dict = {}


# ── folders ──────────────────────────────────────────────────────────────────


@recipe("GET", "/folders")
def _folders_list(call, wire):
    actor = wire.actor()
    wire.folder(actor)
    return call(actor, query={"workspace_id": str(wire.workspace_id)})


@recipe("POST", "/folders")
def _folders_create(call, wire):
    return call(
        wire.actor(),
        data={"workspace_id": str(wire.workspace_id), "name": "Quarterly"},
    )


@recipe("GET", "/folders/{folder_id}")
def _folder_get(call, wire):
    actor = wire.actor()
    folder = wire.folder(actor)
    return call(actor, params={"folder_id": folder["id"]})


@recipe("PATCH", "/folders/{folder_id}")
def _folder_patch(call, wire):
    actor = wire.actor()
    folder = wire.folder(actor)
    return call(actor, params={"folder_id": folder["id"]}, data={"name": "Renamed"})


@recipe("POST", "/folders/{folder_id}/restore")
def _folder_restore(call, wire):
    actor = wire.actor()
    folder = wire.folder(actor)
    assert actor.delete(f"{V1}/folders/{folder['id']}").status_code == 204
    return call(actor, params={"folder_id": folder["id"]})


# ── documents ────────────────────────────────────────────────────────────────


@recipe("GET", "/documents")
def _documents_list(call, wire):
    actor = wire.actor()
    wire.document(actor)
    return call(actor, query={"workspace_id": str(wire.workspace_id)})


@recipe("POST", "/documents")
def _documents_create(call, wire):
    return call(
        wire.actor(),
        data={
            "workspace_id": str(wire.workspace_id),
            "type": "md",
            "title": "Minutes",
            "body": "# minutes",
        },
    )


@recipe("GET", "/documents/{document_id}")
def _document_get(call, wire):
    actor = wire.actor()
    document = wire.document(actor, body="# hi")
    return call(actor, params={"document_id": document["id"]})


@recipe("PATCH", "/documents/{document_id}")
def _document_patch(call, wire):
    actor = wire.actor()
    document = wire.document(actor)
    return call(
        actor, params={"document_id": document["id"]}, data={"title": "Retitled"}
    )


@recipe("POST", "/documents/{document_id}/restore")
def _document_restore(call, wire):
    actor = wire.actor()
    document = wire.document(actor, body="# hi")
    assert actor.delete(f"{V1}/documents/{document['id']}").status_code == 204
    return call(actor, params={"document_id": document["id"]})


# ── content ──────────────────────────────────────────────────────────────────


@recipe("PUT", "/documents/{document_id}/content")
def _content_put(call, wire):
    actor = wire.actor()
    document = wire.document(actor)
    return call(
        actor,
        params={"document_id": document["id"]},
        data=b"# a whole new state",
        content_type="text/markdown",
        HTTP_IF_MATCH='"0"',
    )


@recipe("GET", "/documents/{document_id}/download")
def _document_download(call, wire):
    actor = wire.actor()
    document = wire.document(actor, body="# hi")
    with override_settings(STAPEL_DOCS=UNEXPIRING):
        return call(actor, params={"document_id": document["id"]})


# ── archive ──────────────────────────────────────────────────────────────────


@recipe("GET", "/documents/{document_id}/archive")
def _document_archive(call, wire):
    actor = wire.actor()
    document = wire.zip_document(actor)
    return call(actor, params={"document_id": document["id"]})


# ── update journal ───────────────────────────────────────────────────────────


@recipe("GET", "/documents/{document_id}/updates")
def _updates_feed(call, wire):
    actor = wire.actor()
    document = wire.crdt_document(actor)
    appended = actor.post(
        f"{V1}/documents/{document['id']}/updates",
        {"updates": [_b64(b"u1"), _b64(b"u2")]},
        format="json",
    )
    assert appended.status_code == 200, appended.content
    return call(actor, params={"document_id": document["id"]}, query={"since": 0})


@recipe("POST", "/documents/{document_id}/updates")
def _updates_append(call, wire):
    actor = wire.actor()
    document = wire.crdt_document(actor)
    return call(
        actor,
        params={"document_id": document["id"]},
        data={"updates": [_b64(b"u1")], "client_id": "wire-contract"},
    )


# ── revisions ────────────────────────────────────────────────────────────────


@recipe("GET", "/documents/{document_id}/revisions")
def _revisions_list(call, wire):
    actor = wire.actor()
    document = wire.document(actor, body="# v1")
    return call(actor, params={"document_id": document["id"]})


@recipe("POST", "/documents/{document_id}/revisions")
def _revisions_create(call, wire):
    actor = wire.actor()
    document = wire.document(actor, body="# v1")
    return call(
        actor, params={"document_id": document["id"]}, data={"name": "before the edit"}
    )


@recipe("GET", "/documents/{document_id}/revisions/{revision_id}/download")
def _revision_download(call, wire):
    actor = wire.actor()
    document = wire.document(actor, body="# v1")
    revision = actor.get(f"{V1}/documents/{document['id']}/revisions").json()[0]
    with override_settings(STAPEL_DOCS=UNEXPIRING):
        return call(
            actor,
            params={"document_id": document["id"], "revision_id": revision["id"]},
        )


@recipe("POST", "/documents/{document_id}/revisions/{revision_id}/restore")
def _revision_restore(call, wire):
    actor = wire.actor()
    document = wire.document(actor, body="# v1")
    wire.save(actor, document, b"# v2", expected_seq=1)
    revision = actor.get(f"{V1}/documents/{document['id']}/revisions").json()[-1]
    return call(
        actor, params={"document_id": document["id"], "revision_id": revision["id"]}
    )


# ── drive surfaces ───────────────────────────────────────────────────────────


@recipe("GET", "/recents")
def _recents(call, wire):
    actor = wire.actor()
    document = wire.document(actor, body="# hi")
    # Recents are written by the service layer on a content read, never by
    # this endpoint — so the listing has a row only after one.
    assert actor.get(f"{V1}/documents/{document['id']}/content").status_code == 200
    return call(actor, query={"workspace_id": str(wire.workspace_id)})


@recipe("GET", "/search")
def _search(call, wire):
    actor = wire.actor()
    wire.document(actor, title="Quarterly plan")
    wire.folder(actor, name="Quarterly folder")
    return call(
        actor, query={"workspace_id": str(wire.workspace_id), "q": "quarterly"}
    )


# ── trash ────────────────────────────────────────────────────────────────────


@recipe("POST", "/trash/empty")
def _trash_empty(call, wire):
    actor = wire.actor()
    document = wire.document(actor, body="# hi")
    assert actor.delete(f"{V1}/documents/{document['id']}").status_code == 204
    return call(actor, data={"workspace_id": str(wire.workspace_id)})


# ── uploads ──────────────────────────────────────────────────────────────────


@recipe("POST", "/uploads")
def _uploads_create(call, wire):
    return call(
        wire.actor(),
        data={
            "workspace_id": str(wire.workspace_id),
            "title": "meeting.txt",
            "mime_type": "text/plain",
        },
    )


@recipe("POST", "/uploads/{upload_id}/finalize")
def _uploads_finalize(call, wire):
    from stapel_docs.storage import get_storage

    actor = wire.actor()
    ticket = wire.upload_ticket(actor)
    get_storage().put_bytes(ticket["key"], b"raw bytes", content_type="text/plain")
    return call(actor, params={"upload_id": ticket["upload_id"]})


# ── sharing: the share sheet ─────────────────────────────────────────────────


@recipe("GET", "/documents/{document_id}/access")
def _access_list(call, wire):
    with override_settings(STAPEL_DOCS=WHITELIST_ON):
        actor = wire.actor()
        document = wire.document(actor, body="# hi")
        wire.grant(actor, document, make_user())
        return call(actor, params={"document_id": document["id"]})


@recipe("POST", "/documents/{document_id}/access")
def _access_create(call, wire):
    with override_settings(STAPEL_DOCS=WHITELIST_ON):
        actor = wire.actor()
        document = wire.document(actor, body="# hi")
        return call(
            actor,
            params={"document_id": document["id"]},
            data={
                "subject_kind": "user",
                "user_id": str(make_user().pk),
                "level": "view",
            },
        )


@recipe("GET", "/documents/{document_id}/links")
def _links_list(call, wire):
    with override_settings(STAPEL_DOCS=LINK_ON):
        actor = wire.actor()
        document = wire.document(actor, body="# hi")
        wire.link(actor, document)
        return call(actor, params={"document_id": document["id"]})


@recipe("POST", "/documents/{document_id}/links")
def _links_create(call, wire):
    with override_settings(STAPEL_DOCS=LINK_ON):
        actor = wire.actor()
        document = wire.document(actor, body="# hi")
        return call(actor, params={"document_id": document["id"]}, data={})


# ── sharing: the bearer surface ──────────────────────────────────────────────


@recipe("GET", "/shared/{token}")
def _shared_document(call, wire):
    with override_settings(STAPEL_DOCS=LINK_ON):
        actor = wire.actor()
        document = wire.document(actor, body="# hi")
        link = wire.link(actor, document)
        # Anonymous redemption is off by default, so the bearer is a real
        # account that holds nothing in this workspace: the link is the
        # whole of its access.
        return call(wire.outsider(), params={"token": link["token"]})


@recipe("GET", "/shared/{token}/archive")
def _shared_archive(call, wire):
    with override_settings(STAPEL_DOCS=LINK_ON):
        actor = wire.actor()
        document = wire.zip_document(actor)
        link = wire.link(actor, document)
        return call(wire.outsider(), params={"token": link["token"]})


@recipe("GET", "/shared/{token}/download")
def _shared_download(call, wire):
    with override_settings(STAPEL_DOCS=LINK_ON):
        actor = wire.actor()
        document = wire.document(actor, body="# hi")
        link = wire.link(actor, document)
        bearer = wire.outsider()
    with override_settings(STAPEL_DOCS={**LINK_ON, **UNEXPIRING}):
        return call(bearer, params={"token": link["token"]})


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Three of the first four libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: one mounted a different
    prefix AND one segment short, one mounted the paths bare, and this one
    mounted less than the emission did — so the gdpr half of its own contract
    was unreachable. In every case the operations were "covered" by a file
    that could not have reached a single one of them.

    That is the same family as a gate nobody asks: the recipes can all be
    written, the run can be green, and not one request went where the contract
    says it goes. A missing recipe already fails loudly; this fails when the
    MOUNT is wrong, which no per-operation check can see, because when the
    mount is wrong every operation is equally and silently unreachable.

    Asserted against the urlconf this module declares, so it fails at the one
    moment it is cheap to fix: when somebody changes a mount.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment, and this urlconf uses
    # several converters — uuid, int, slug. A path counts as reachable if any
    # one shape resolves: the question here is whether the mount exists, not
    # whether a particular id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    declared = {(method, path) for method, path, _, _ in OPERATIONS}
    covered = set(RECIPES) | set(UNDRIVABLE)

    missing = sorted(declared - covered)
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    stale = sorted(covered - declared)
    assert not stale, (
        "recipes/exclusions for operations the contract no longer declares:\n"
        + "\n".join(f"  {m} {p}" for m, p in stale)
    )
    both = sorted(set(RECIPES) & set(UNDRIVABLE))
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason.

    Without this, an operation that is renamed or removed leaves an entry
    that silences nothing and reads like a known problem forever.
    """
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for key, reason in KNOWN_MISMATCHES.items():
        assert key in declared, (
            f"{key} is recorded as a known mismatch but the contract no longer "
            "declares it — delete the entry"
        )
        assert reason and reason.strip(), f"{key} is recorded with no reason"


#: Operations whose declared body the wire does not send.
#:
#: EMPTY on the day this gate was written: all 31 declared JSON responses
#: matched the bodies the views sent. The mechanism stays because the next
#: wave will need it: an entry must name the defect and its owner, and
#: ``strict=True`` turns a fixed one into a failure until the entry is
#: deleted, so a finding can be neither forgotten nor quietly kept.
KNOWN_MISMATCHES: dict = {}


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p}" for m, p, _, _ in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(
    method, path, code, body_schema, grant_capabilities, request
):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    perform = RECIPES.get((method, path))
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    response = perform(Call(method, path), Wire(grant_capabilities))
    assert response.status_code == code, (
        f"{method} {path}: expected the declared {code}, got "
        f"{response.status_code}: {response.content[:400]}"
    )

    body = response.json()
    errors = sorted(
        _validator(body_schema).iter_errors(body), key=lambda e: list(e.path)
    )
    assert not errors, (
        f"{method} {path} answers a body the contract does not describe:\n"
        + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
        + f"\n  body: {json.dumps(body)[:600]}"
    )
    # An empty list validates against any item schema, so a list response
    # must actually carry a row for the check to have looked at anything.
    if isinstance(body, list):
        assert body, f"{method} {path}: the declared list came back empty"
