"""v1 URL set — paths here are relative to the ``api/v1/`` mount
contributed by the root ``urls.py`` (api-versioning.md §2).
"""
from typing import NamedTuple

from django.urls import path

from .views import (
    DocumentAccessDetailView,
    DocumentAccessView,
    DocumentContentView,
    DocumentDetailView,
    DocumentDownloadView,
    DocumentExportView,
    DocumentLinkDetailView,
    DocumentLinkView,
    DocumentListCreateView,
    DocumentRestoreView,
    DocumentStarView,
    DocumentThumbnailView,
    DocumentUpdatesView,
    FolderDetailView,
    FolderListCreateView,
    FolderRestoreView,
    FolderStarView,
    RecentsView,
    RevisionContentView,
    RevisionDownloadView,
    RevisionListCreateView,
    RevisionRestoreView,
    SearchView,
    SharedContentView,
    SharedDocumentView,
    SharedDownloadView,
    StarredView,
    TrashEmptyView,
    TrashView,
    UploadContentPutView,
    UploadCreateView,
    UploadFinalizeView,
)

urlpatterns = [
    path("folders", FolderListCreateView.as_view(), name="docs-folders"),
    path("folders/<uuid:folder_id>", FolderDetailView.as_view(), name="docs-folder-detail"),
    path("folders/<uuid:folder_id>/restore", FolderRestoreView.as_view(), name="docs-folder-restore"),
    path("folders/<uuid:folder_id>/star", FolderStarView.as_view(), name="docs-folder-star"),
    path("documents", DocumentListCreateView.as_view(), name="docs-documents"),
    path("documents/<uuid:document_id>", DocumentDetailView.as_view(), name="docs-document-detail"),
    path("documents/<uuid:document_id>/restore", DocumentRestoreView.as_view(), name="docs-document-restore"),
    path("documents/<uuid:document_id>/content", DocumentContentView.as_view(), name="docs-document-content"),
    path("documents/<uuid:document_id>/download", DocumentDownloadView.as_view(), name="docs-document-download"),
    path("documents/<uuid:document_id>/export", DocumentExportView.as_view(), name="docs-document-export"),
    path("documents/<uuid:document_id>/star", DocumentStarView.as_view(), name="docs-document-star"),
    path(
        "documents/<uuid:document_id>/thumbnail",
        DocumentThumbnailView.as_view(),
        name="docs-document-thumbnail",
    ),
    path(
        "documents/<uuid:document_id>/access",
        DocumentAccessView.as_view(),
        name="docs-document-access",
    ),
    path(
        "documents/<uuid:document_id>/access/<uuid:access_id>",
        DocumentAccessDetailView.as_view(),
        name="docs-document-access-detail",
    ),
    path(
        "documents/<uuid:document_id>/links",
        DocumentLinkView.as_view(),
        name="docs-document-links",
    ),
    path(
        "documents/<uuid:document_id>/links/<uuid:link_id>",
        DocumentLinkDetailView.as_view(),
        name="docs-document-link-detail",
    ),
    path("documents/<uuid:document_id>/updates", DocumentUpdatesView.as_view(), name="docs-document-updates"),
    path("documents/<uuid:document_id>/revisions", RevisionListCreateView.as_view(), name="docs-revisions"),
    path(
        "documents/<uuid:document_id>/revisions/<uuid:revision_id>/content",
        RevisionContentView.as_view(),
        name="docs-revision-content",
    ),
    path(
        "documents/<uuid:document_id>/revisions/<uuid:revision_id>/download",
        RevisionDownloadView.as_view(),
        name="docs-revision-download",
    ),
    path(
        "documents/<uuid:document_id>/revisions/<uuid:revision_id>/restore",
        RevisionRestoreView.as_view(),
        name="docs-revision-restore",
    ),
    # The bearer path. Deliberately NOT under /documents/<id>: a link
    # holder addresses the document BY the token and never learns its id
    # from a URL they were handed (axis §6 — the surface of a link is the
    # document, not its coordinates in somebody's workspace).
    path("shared/<str:token>", SharedDocumentView.as_view(), name="docs-shared-document"),
    path(
        "shared/<str:token>/content",
        SharedContentView.as_view(),
        name="docs-shared-content",
    ),
    path(
        "shared/<str:token>/download",
        SharedDownloadView.as_view(),
        name="docs-shared-download",
    ),
    path("starred", StarredView.as_view(), name="docs-starred"),
    path("recents", RecentsView.as_view(), name="docs-recents"),
    path("search", SearchView.as_view(), name="docs-search"),
    path("trash", TrashView.as_view(), name="docs-trash"),
    path("trash/empty", TrashEmptyView.as_view(), name="docs-trash-empty"),
    path("uploads", UploadCreateView.as_view(), name="docs-uploads"),
    # Module-intake PUT: the direct-to-storage leg for backends with
    # accepts_direct_put=False. Signature-authenticated (the credential
    # rides in the URL, presigned-style) — see UploadContentPutView.
    path("uploads/<uuid:upload_id>/content", UploadContentPutView.as_view(), name="docs-upload-content"),
    path("uploads/<uuid:upload_id>/finalize", UploadFinalizeView.as_view(), name="docs-upload-finalize"),
]


class GateEntry(NamedTuple):
    """One gated URL block (capability-config.md §2 p.2). ``flags`` compose
    with OR; empty flags = always on."""

    name: str
    flags: tuple
    patterns: tuple


#: docs has no per-method config gates: the sharing endpoints are gated by
#: workspace CAPABILITIES and by the axis at request time (a mode that is
#: off refuses to mint and marks its rows suspended), not by mounting or
#: unmounting URLs — a route that disappears with a setting is a route no
#: client can tell from a deploy that broke.
GATE_REGISTRY: dict = {
    "docs.api": GateEntry("docs.api", (), tuple(urlpatterns)),
}
