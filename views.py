"""DRF views for stapel-docs.

Presenter-canonical from birth (§55): a view resolves its presenter through
``get_presenter`` (see ``presenters.py``) and returns
``StapelResponse(Serializer(presenter.present(...)))`` — it never
instantiates a ``dto.py`` dataclass itself (SWAP002) and never imports the
concrete presenter class (SWAP001).

Authorization: every view routes its decision through
``stapel_docs.authz.authorize`` — the single choke point (sharing-axis §7).
``deny`` -> 403, ``unavailable`` -> 503, never 403-on-outage. Trashed
objects are 404 on normal endpoints; only trash/restore/purge see them.
"""
from __future__ import annotations

import functools
import uuid as uuid_module

from django.http import HttpResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers as drf_serializers
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.parsers import BaseParser
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
from stapel_core.django.api.permissions import IsNotAnonymousUser

from . import services
from .authz import DENY, UNAVAILABLE, Principal, authorize
from .errors import (
    ERR_400_BAD_SINCE,
    ERR_400_EXPORT_FORMAT,
    ERR_400_TYPE_NOT_EDITABLE,
    ERR_403_FORBIDDEN,
    ERR_403_UPLOAD_OWNER,
    ERR_412_MISSING_IF_MATCH,
    ERR_413_EXPORT_TOO_LARGE,
    ERR_503_EXPORTER,
    ERR_503_WORKSPACES,
)
from .exporters import (
    ExporterUnavailable,
    ExportFormatUnknown,
    ExportUnsupportedType,
    get_exporter,
)
from .presenters import (
    get_document_presenter,
    get_folder_presenter,
    get_revision_presenter,
    present_append_result,
    present_download_url,
    present_purge_result,
    present_resync,
    present_save_result,
    present_updates_feed,
    present_upload_ticket,
)
from .serializers import (
    AppendResultSerializer,
    DocumentCreateSerializer,
    DocumentListQuerySerializer,
    DocumentPatchSerializer,
    DocumentSerializer,
    DownloadUrlSerializer,
    FolderCreateSerializer,
    FolderPatchSerializer,
    FolderSerializer,
    NamedRevisionSerializer,
    ResyncSerializer,
    RevisionSerializer,
    SaveResultSerializer,
    TrashEmptySerializer,
    TrashPurgeResultSerializer,
    UpdatesAppendSerializer,
    UpdatesFeedSerializer,
    UploadCreateSerializer,
    UploadTicketSerializer,
    WorkspaceQuerySerializer,
)


class SerializerSeamMixin:
    """Overridable serializer seam for every stapel-docs APIView.

    Host projects can swap the request/response serializer of any view by
    subclassing and setting ``request_serializer_class`` /
    ``response_serializer_class`` (or overriding the getters for
    per-request decisions) — no need to rewrite the HTTP method bodies.
    """

    request_serializer_class = None
    response_serializer_class = None

    def get_request_serializer_class(self):
        return self.request_serializer_class

    def get_response_serializer_class(self):
        return self.response_serializer_class


class IgnoreFormatSuffixNegotiation(DefaultContentNegotiation):
    """The export endpoint's ``?format=`` names the EXPORT format (fixed by
    the endpoint contract), not DRF's renderer suffix — without this an
    unknown value would 404 in content negotiation before the view runs.
    Success responses are raw ``HttpResponse`` bytes; only the JSON error
    envelope ever renders through DRF, so the first renderer is the one."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class RawBodyParser(BaseParser):
    """Raw request-body bytes for the content PUT — the body is opaque
    document content of any mime type, never a parsed structure."""

    media_type = "*/*"

    def parse(self, stream, media_type=None, parser_context=None):
        return stream.read()


def _access_error(request, workspace_id, *actions):
    """authorize() each required action; an error response or None.
    deny -> 403, unavailable -> 503 (never 403-on-outage)."""
    principal = Principal.from_request(request)
    for action in actions:
        verdict = authorize(
            workspace_id=workspace_id, principal=principal, action=action
        )
        if verdict == DENY:
            return StapelErrorResponse(403, ERR_403_FORBIDDEN)
        if verdict == UNAVAILABLE:
            return StapelErrorResponse(503, ERR_503_WORKSPACES)
    return None


def _acting_user(request):
    user = getattr(request, "user", None)
    return user if user is not None and user.is_authenticated else None


def _maps_docs_errors(method):
    """Translate service refusals into the unified error envelope."""

    @functools.wraps(method)
    def wrapper(self, request, *args, **kwargs):
        try:
            return method(self, request, *args, **kwargs)
        except services.DocsError as exc:
            return StapelErrorResponse(exc.status, exc.error_key, exc.params)

    return wrapper


_WORKSPACE_PARAM = OpenApiParameter(
    name="workspace_id", type=str, location=OpenApiParameter.QUERY, required=True
)


# ─────────────────────────────────────────────────────────────────────
# Folders
# ─────────────────────────────────────────────────────────────────────


@extend_schema(tags=["Docs / folders"])
class FolderListCreateView(SerializerSeamMixin, APIView):
    """List live folders of a workspace, or create one."""

    permission_classes = [IsNotAnonymousUser]
    request_serializer_class = FolderCreateSerializer
    response_serializer_class = FolderSerializer

    @extend_schema(
        parameters=[
            _WORKSPACE_PARAM,
            OpenApiParameter(
                name="parent_id",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Restrict to children of this folder; pass empty "
                "for workspace roots. Absent = the whole tree.",
            ),
        ],
        responses={200: FolderSerializer(many=True)},
    )
    @_maps_docs_errors
    def get(self, request):
        query = WorkspaceQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        workspace_id = query.validated_data["workspace_id"]
        denied = _access_error(request, workspace_id, "view")
        if denied:
            return denied
        parent_id = ...
        if "parent_id" in request.query_params:
            raw = request.query_params["parent_id"]
            if raw == "":
                parent_id = None
            else:
                try:
                    parent_id = uuid_module.UUID(raw)
                except ValueError:
                    raise drf_serializers.ValidationError({"parent_id": "invalid uuid"})
        rows = services.list_folders(workspace_id, parent_id=parent_id)
        presenter = get_folder_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present_many(rows), many=True)
        )

    @extend_schema(request=FolderCreateSerializer, responses={201: FolderSerializer})
    @_maps_docs_errors
    def post(self, request):
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data
        denied = _access_error(request, data["workspace_id"], "edit")
        if denied:
            return denied
        folder = services.create_folder(
            workspace_id=data["workspace_id"],
            name=data["name"],
            parent_id=data.get("parent_id"),
            user=_acting_user(request),
        )
        presenter = get_folder_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present(folder)), status=201
        )


@extend_schema(tags=["Docs / folders"])
class FolderDetailView(SerializerSeamMixin, APIView):
    """Fetch, rename/move (rename=edit, move=manage) or trash a folder.
    Trashing soft-deletes the whole live subtree, documents included."""

    permission_classes = [IsNotAnonymousUser]
    request_serializer_class = FolderPatchSerializer
    response_serializer_class = FolderSerializer

    @extend_schema(responses={200: FolderSerializer})
    @_maps_docs_errors
    def get(self, request, folder_id):
        folder = services.get_live_folder(folder_id)
        denied = _access_error(request, folder.workspace_id, "view")
        if denied:
            return denied
        presenter = get_folder_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present(folder))
        )

    @extend_schema(request=FolderPatchSerializer, responses={200: FolderSerializer})
    @_maps_docs_errors
    def patch(self, request, folder_id):
        folder = services.get_live_folder(folder_id)
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data
        actions = []
        if "name" in data:
            actions.append("edit")
        if "parent_id" in data:
            actions.append("manage")
        denied = _access_error(request, folder.workspace_id, *actions)
        if denied:
            return denied
        if "name" in data:
            folder = services.rename_folder(folder, data["name"])
        if "parent_id" in data:
            folder = services.move_folder(folder, data["parent_id"])
        presenter = get_folder_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present(folder))
        )

    @extend_schema(responses={204: None})
    @_maps_docs_errors
    def delete(self, request, folder_id):
        folder = services.get_live_folder(folder_id)
        denied = _access_error(request, folder.workspace_id, "manage")
        if denied:
            return denied
        services.trash_folder(folder)
        return StapelResponse(status=204)


@extend_schema(tags=["Docs / folders"])
class FolderRestoreView(SerializerSeamMixin, APIView):
    """Untrash a folder subtree."""

    permission_classes = [IsNotAnonymousUser]
    response_serializer_class = FolderSerializer

    @extend_schema(request=None, responses={200: FolderSerializer})
    @_maps_docs_errors
    def post(self, request, folder_id):
        folder = services.get_trashed_folder(folder_id)
        denied = _access_error(request, folder.workspace_id, "manage")
        if denied:
            return denied
        folder = services.restore_folder(folder)
        presenter = get_folder_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present(folder))
        )


# ─────────────────────────────────────────────────────────────────────
# Documents
# ─────────────────────────────────────────────────────────────────────


@extend_schema(tags=["Docs / documents"])
class DocumentListCreateView(SerializerSeamMixin, APIView):
    """List live documents of a workspace (pending uploads excluded), or
    create one (unknown type -> 400; absent body -> the type's empty body)."""

    permission_classes = [IsNotAnonymousUser]
    request_serializer_class = DocumentCreateSerializer
    response_serializer_class = DocumentSerializer

    @extend_schema(
        parameters=[
            _WORKSPACE_PARAM,
            OpenApiParameter(name="folder_id", type=str, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(name="type", type=str, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(
                name="q", type=str, location=OpenApiParameter.QUERY, required=False,
                description="Case-insensitive title substring.",
            ),
        ],
        responses={200: DocumentSerializer(many=True)},
    )
    @_maps_docs_errors
    def get(self, request):
        query = DocumentListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        denied = _access_error(request, data["workspace_id"], "view")
        if denied:
            return denied
        rows = services.list_documents(
            data["workspace_id"],
            folder_id=data.get("folder_id"),
            type=data.get("type"),
            q=data.get("q"),
        )
        presenter = get_document_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present_many(rows), many=True)
        )

    @extend_schema(request=DocumentCreateSerializer, responses={201: DocumentSerializer})
    @_maps_docs_errors
    def post(self, request):
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data
        denied = _access_error(request, data["workspace_id"], "edit")
        if denied:
            return denied
        document = services.create_document(
            workspace_id=data["workspace_id"],
            type=data["type"],
            title=data["title"],
            folder_id=data.get("folder_id"),
            metadata=data.get("metadata"),
            body=data.get("body"),
            user=_acting_user(request),
        )
        presenter = get_document_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present(document)), status=201
        )


@extend_schema(tags=["Docs / documents"])
class DocumentDetailView(SerializerSeamMixin, APIView):
    """Fetch the envelope, patch (title/metadata=edit, folder move=manage)
    or trash a document."""

    permission_classes = [IsNotAnonymousUser]
    request_serializer_class = DocumentPatchSerializer
    response_serializer_class = DocumentSerializer

    @extend_schema(responses={200: DocumentSerializer})
    @_maps_docs_errors
    def get(self, request, document_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "view")
        if denied:
            return denied
        presenter = get_document_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present(document))
        )

    @extend_schema(request=DocumentPatchSerializer, responses={200: DocumentSerializer})
    @_maps_docs_errors
    def patch(self, request, document_id):
        document = services.get_live_document(document_id)
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data
        actions = []
        if "title" in data or "metadata" in data:
            actions.append("edit")
        if "folder_id" in data:
            actions.append("manage")
        denied = _access_error(request, document.workspace_id, *actions)
        if denied:
            return denied
        if "title" in data or "metadata" in data:
            document = services.update_document(
                document, title=data.get("title"), metadata=data.get("metadata")
            )
        if "folder_id" in data:
            document = services.move_document(document, data["folder_id"])
        presenter = get_document_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present(document))
        )

    @extend_schema(responses={204: None})
    @_maps_docs_errors
    def delete(self, request, document_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "manage")
        if denied:
            return denied
        services.trash_document(document)
        return StapelResponse(status=204)


@extend_schema(tags=["Docs / documents"])
class DocumentRestoreView(SerializerSeamMixin, APIView):
    """Untrash a document (re-announced via document.created)."""

    permission_classes = [IsNotAnonymousUser]
    response_serializer_class = DocumentSerializer

    @extend_schema(request=None, responses={200: DocumentSerializer})
    @_maps_docs_errors
    def post(self, request, document_id):
        document = services.get_trashed_document(document_id)
        denied = _access_error(request, document.workspace_id, "manage")
        if denied:
            return denied
        document = services.restore_document(document)
        presenter = get_document_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present(document))
        )


# ─────────────────────────────────────────────────────────────────────
# Content
# ─────────────────────────────────────────────────────────────────────


@extend_schema(tags=["Docs / content"])
class DocumentContentView(SerializerSeamMixin, APIView):
    """The versioning heart: GET raw body bytes / PUT a whole-state save
    under optimistic lock (If-Match carries the client's head_seq)."""

    permission_classes = [IsNotAnonymousUser]
    parser_classes = [RawBodyParser]
    response_serializer_class = SaveResultSerializer

    @extend_schema(responses={200: None})
    @_maps_docs_errors
    def get(self, request, document_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "view")
        if denied:
            return denied
        body, mime, head_seq = services.read_content(document)
        response = HttpResponse(body, content_type=mime)
        response["ETag"] = f'"{head_seq}"'
        response["X-Docs-Head-Seq"] = str(head_seq)
        return response

    @extend_schema(request=None, responses={200: SaveResultSerializer})
    @_maps_docs_errors
    def put(self, request, document_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "edit")
        if denied:
            return denied
        raw = request.headers.get("If-Match")
        expected_seq = None
        if raw is not None:
            try:
                expected_seq = int(raw.strip().strip('"'))
            except ValueError:
                expected_seq = None
        if expected_seq is None:
            return StapelErrorResponse(412, ERR_412_MISSING_IF_MATCH)
        body = request.data if isinstance(request.data, bytes) else request.body
        document, revision = services.save_content(
            document.pk, body, expected_seq=expected_seq, user=_acting_user(request)
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                present_save_result(document.head_seq, revision)
            )
        )


@extend_schema(tags=["Docs / content"])
class DocumentDownloadView(SerializerSeamMixin, APIView):
    """Presigned GET URL for the current body — opaque, never assume shape."""

    permission_classes = [IsNotAnonymousUser]
    response_serializer_class = DownloadUrlSerializer

    @extend_schema(responses={200: DownloadUrlSerializer})
    @_maps_docs_errors
    def get(self, request, document_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "view")
        if denied:
            return denied
        url = services.download_url(document.snapshot_key)
        return StapelResponse(
            self.get_response_serializer_class()(present_download_url(url))
        )


@extend_schema(tags=["Docs / content"])
class DocumentExportView(SerializerSeamMixin, APIView):
    """Render the body through the exporter registry (?format=pdf)."""

    permission_classes = [IsNotAnonymousUser]
    content_negotiation_class = IgnoreFormatSuffixNegotiation

    @extend_schema(
        parameters=[
            OpenApiParameter(name="format", type=str, location=OpenApiParameter.QUERY, required=True),
        ],
        responses={200: None},
    )
    @_maps_docs_errors
    def get(self, request, document_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "view")
        if denied:
            return denied
        fmt = request.query_params.get("format", "")
        try:
            exporter = get_exporter(fmt)
        except ExportFormatUnknown:
            return StapelErrorResponse(400, ERR_400_EXPORT_FORMAT)
        body, _, _ = services.read_content(document)
        # Exporters parse the body in-process (fpdf2 for pdf): an unbounded
        # input is unbounded CPU and memory on a request thread.
        export_limit = services.resource_limit("MAX_EXPORT_BYTES")
        if export_limit and len(body) > export_limit:
            return StapelErrorResponse(
                413, ERR_413_EXPORT_TOO_LARGE, {"limit_bytes": export_limit}
            )
        spec = services.effective_spec(document)
        try:
            rendered, mime = exporter.export(document, body, spec)
        except ExportUnsupportedType:
            return StapelErrorResponse(400, ERR_400_TYPE_NOT_EDITABLE)
        except ExporterUnavailable:
            return StapelErrorResponse(503, ERR_503_EXPORTER)
        return HttpResponse(rendered, content_type=mime)


# ─────────────────────────────────────────────────────────────────────
# Update journal
# ─────────────────────────────────────────────────────────────────────


@extend_schema(tags=["Docs / updates"])
class DocumentUpdatesView(SerializerSeamMixin, APIView):
    """Append opaque commutative updates (crdt types only) / replay the
    journal from ``?since=`` with chat-pattern resync semantics."""

    permission_classes = [IsNotAnonymousUser]
    request_serializer_class = UpdatesAppendSerializer
    response_serializer_class = UpdatesFeedSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(name="since", type=int, location=OpenApiParameter.QUERY, required=False),
        ],
        responses={200: UpdatesFeedSerializer},
    )
    @_maps_docs_errors
    def get(self, request, document_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "view")
        if denied:
            return denied
        raw = request.query_params.get("since", "0")
        try:
            since = int(raw)
            if since < 0:
                raise ValueError
        except ValueError:
            return StapelErrorResponse(400, ERR_400_BAD_SINCE)
        kind, rows = services.read_updates(document, since)
        if kind == "resync":
            return StapelResponse(ResyncSerializer(present_resync(document)))
        return StapelResponse(
            self.get_response_serializer_class()(
                present_updates_feed(rows, document.head_seq)
            )
        )

    @extend_schema(request=UpdatesAppendSerializer, responses={200: AppendResultSerializer})
    @_maps_docs_errors
    def post(self, request, document_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "edit")
        if denied:
            return denied
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data
        head_seq = services.append_updates(
            document.pk,
            data["updates"],
            client_id=data.get("client_id", ""),
            client_seq=data.get("client_seq"),
            principal=Principal.from_request(request),
        )
        return StapelResponse(AppendResultSerializer(present_append_result(head_seq)))


# ─────────────────────────────────────────────────────────────────────
# Revisions
# ─────────────────────────────────────────────────────────────────────


@extend_schema(tags=["Docs / revisions"])
class RevisionListCreateView(SerializerSeamMixin, APIView):
    """List the version history / name the current head snapshot."""

    permission_classes = [IsNotAnonymousUser]
    request_serializer_class = NamedRevisionSerializer
    response_serializer_class = RevisionSerializer

    @extend_schema(responses={200: RevisionSerializer(many=True)})
    @_maps_docs_errors
    def get(self, request, document_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "view")
        if denied:
            return denied
        presenter = get_revision_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(
                presenter.present_many(document.revisions.all()), many=True
            )
        )

    @extend_schema(request=NamedRevisionSerializer, responses={201: RevisionSerializer})
    @_maps_docs_errors
    def post(self, request, document_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "manage")
        if denied:
            return denied
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        revision = services.create_named_revision(
            document, req.validated_data["name"], user=_acting_user(request)
        )
        presenter = get_revision_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present(revision)), status=201
        )


@extend_schema(tags=["Docs / revisions"])
class RevisionContentView(SerializerSeamMixin, APIView):
    """The revision's full bytes — a self-contained snapshot (I1)."""

    permission_classes = [IsNotAnonymousUser]

    @extend_schema(responses={200: None})
    @_maps_docs_errors
    def get(self, request, document_id, revision_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "view")
        if denied:
            return denied
        revision = services.get_revision(document, revision_id)
        body, mime = services.revision_content(document, revision)
        return HttpResponse(body, content_type=mime)


@extend_schema(tags=["Docs / revisions"])
class RevisionDownloadView(SerializerSeamMixin, APIView):
    """Presigned GET URL for a revision's snapshot."""

    permission_classes = [IsNotAnonymousUser]
    response_serializer_class = DownloadUrlSerializer

    @extend_schema(responses={200: DownloadUrlSerializer})
    @_maps_docs_errors
    def get(self, request, document_id, revision_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "view")
        if denied:
            return denied
        revision = services.get_revision(document, revision_id)
        url = services.download_url(revision.storage_key)
        return StapelResponse(
            self.get_response_serializer_class()(present_download_url(url))
        )


@extend_schema(tags=["Docs / revisions"])
class RevisionRestoreView(SerializerSeamMixin, APIView):
    """Restore-as-new-head: history is never rewritten."""

    permission_classes = [IsNotAnonymousUser]
    response_serializer_class = SaveResultSerializer

    @extend_schema(request=None, responses={200: SaveResultSerializer})
    @_maps_docs_errors
    def post(self, request, document_id, revision_id):
        document = services.get_live_document(document_id)
        denied = _access_error(request, document.workspace_id, "edit")
        if denied:
            return denied
        revision = services.get_revision(document, revision_id)
        document, minted = services.restore_revision(
            document, revision, user=_acting_user(request)
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                present_save_result(document.head_seq, minted)
            )
        )


# ─────────────────────────────────────────────────────────────────────
# Trash
# ─────────────────────────────────────────────────────────────────────


@extend_schema(tags=["Docs / trash"])
class TrashView(SerializerSeamMixin, APIView):
    """Everything soft-deleted in the workspace."""

    permission_classes = [IsNotAnonymousUser]

    @extend_schema(parameters=[_WORKSPACE_PARAM], responses={200: None})
    @_maps_docs_errors
    def get(self, request):
        query = WorkspaceQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        workspace_id = query.validated_data["workspace_id"]
        denied = _access_error(request, workspace_id, "manage")
        if denied:
            return denied
        folders, documents = services.trash_listing(workspace_id)
        folder_presenter = get_folder_presenter()
        document_presenter = get_document_presenter()
        return StapelResponse(
            {
                "folders": FolderSerializer(
                    folder_presenter.present_many(folders), many=True
                ).data,
                "documents": DocumentSerializer(
                    document_presenter.present_many(documents), many=True
                ).data,
            }
        )


@extend_schema(tags=["Docs / trash"])
class TrashEmptyView(SerializerSeamMixin, APIView):
    """Irreversibly purge listed trashed items — or the whole trash."""

    permission_classes = [IsNotAnonymousUser]
    request_serializer_class = TrashEmptySerializer
    response_serializer_class = TrashPurgeResultSerializer

    @extend_schema(request=TrashEmptySerializer, responses={200: TrashPurgeResultSerializer})
    @_maps_docs_errors
    def post(self, request):
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data
        denied = _access_error(request, data["workspace_id"], "manage")
        if denied:
            return denied
        folders, documents = services.empty_trash(
            data["workspace_id"], ids=data.get("ids")
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                present_purge_result(folders, documents)
            )
        )


# ─────────────────────────────────────────────────────────────────────
# Uploads
# ─────────────────────────────────────────────────────────────────────


@extend_schema(tags=["Docs / uploads"])
class UploadCreateView(SerializerSeamMixin, APIView):
    """Open a presigned direct-to-storage upload (type=file). The document
    row exists immediately but stays hidden from listings until finalize."""

    permission_classes = [IsNotAnonymousUser]
    request_serializer_class = UploadCreateSerializer
    response_serializer_class = UploadTicketSerializer

    @extend_schema(request=UploadCreateSerializer, responses={201: UploadTicketSerializer})
    @_maps_docs_errors
    def post(self, request):
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data
        denied = _access_error(request, data["workspace_id"], "edit")
        if denied:
            return denied
        session, put_url = services.create_upload(
            workspace_id=data["workspace_id"],
            title=data["title"],
            folder_id=data.get("folder_id"),
            mime_type=data.get("mime_type", ""),
            size_bytes=data.get("size_bytes", 0),
            checksum=data.get("checksum", ""),
            user=_acting_user(request),
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                present_upload_ticket(session, put_url)
            ),
            status=201,
        )


@extend_schema(tags=["Docs / uploads"])
class UploadFinalizeView(SerializerSeamMixin, APIView):
    """Promote the uploaded object to the document's first version."""

    permission_classes = [IsNotAnonymousUser]
    response_serializer_class = DocumentSerializer

    @extend_schema(request=None, responses={200: DocumentSerializer})
    @_maps_docs_errors
    def post(self, request, upload_id):
        session = services.get_upload_session(upload_id)
        denied = _access_error(request, session.workspace_id, "edit")
        if denied:
            return denied
        # Owner binding: a ticket is spendable by the user who opened it.
        # Anyone else needs workspace `manage` — a leaked upload_id must not
        # be enough for another member to plant a document in the tree. A
        # ticket with no owner left (GDPR anonymize nulls created_by) has
        # nobody who satisfies the binding, so it takes the same escalation
        # as somebody else's ticket rather than falling open to every
        # editor.
        user = _acting_user(request)
        if user is None or user.pk != session.created_by_id:
            verdict = authorize(
                workspace_id=session.workspace_id,
                principal=Principal.from_request(request),
                action="manage",
            )
            if verdict == UNAVAILABLE:
                return StapelErrorResponse(503, ERR_503_WORKSPACES)
            if verdict == DENY:
                return StapelErrorResponse(403, ERR_403_UPLOAD_OWNER)
        document = services.finalize_upload(session)
        presenter = get_document_presenter()
        return StapelResponse(
            self.get_response_serializer_class()(presenter.present(document))
        )
