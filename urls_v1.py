"""v1 URL set — paths here are relative to the ``api/v1/`` mount
contributed by the root ``urls.py`` (api-versioning.md §2).
"""
from typing import NamedTuple

from django.urls import path

from .views import (
    DocumentContentView,
    DocumentDetailView,
    DocumentDownloadView,
    DocumentExportView,
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
    StarredView,
    TrashEmptyView,
    TrashView,
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
    path("starred", StarredView.as_view(), name="docs-starred"),
    path("recents", RecentsView.as_view(), name="docs-recents"),
    path("search", SearchView.as_view(), name="docs-search"),
    path("trash", TrashView.as_view(), name="docs-trash"),
    path("trash/empty", TrashEmptyView.as_view(), name="docs-trash-empty"),
    path("uploads", UploadCreateView.as_view(), name="docs-uploads"),
    path("uploads/<uuid:upload_id>/finalize", UploadFinalizeView.as_view(), name="docs-upload-finalize"),
]


class GateEntry(NamedTuple):
    """One gated URL block (capability-config.md §2 p.2). ``flags`` compose
    with OR; empty flags = always on."""

    name: str
    flags: tuple
    patterns: tuple


#: docs has no per-method config gates in v1 — the sharing axis ships
#: closed (checks.py guards it) and the seams swap strategies, so the
#: whole URL surface is a single always-on block.
GATE_REGISTRY: dict = {
    "docs.api": GateEntry("docs.api", (), tuple(urlpatterns)),
}
