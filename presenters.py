"""Presenters for stapel-docs — the DTO-building layer (§55).

Presenter discipline (docs/pending/extensibility-presenters.md; enforced by
SWAP001/SWAP002 in `stapel-verify`): views NEVER instantiate a `dto.py`
dataclass directly — every DTO is built by a presenter resolved through
`get_presenter(KEY, default=...)`, so a host project can swap the
presentation of any endpoint via `STAPEL_SWAP` without forking this module.
Etalon: stapel_core/django/users/presenters.py.

Envelope shapes with no backing model (save results, upload tickets, the
journal feed) are built by the ``present_*`` functions at the bottom — the
same "views build nothing" rule, without the model machinery.
"""
from __future__ import annotations

import base64
from typing import Optional

from stapel_core.django.api.presenters import Presenter, PresenterField
from stapel_core.django.swappable import declare_swap, get_presenter

from .doc_types import COLLAB_SNAPSHOT, get_doc_types
from .dto import (
    AppendResultDTO,
    BreadcrumbNodeDTO,
    DownloadUrlDTO,
    JournalUpdateDTO,
    ResyncDTO,
    SaveResultDTO,
    SearchHitDTO,
    TrashPurgeResultDTO,
    UpdatesFeedDTO,
    UploadTicketDTO,
)
from .models import Document, Folder, Revision

FOLDER_PRESENTER_KEY = "DOCS_FOLDER_PRESENTER"
DEFAULT_FOLDER_PRESENTER = "stapel_docs.presenters.FolderPresenter"
DOCUMENT_PRESENTER_KEY = "DOCS_DOCUMENT_PRESENTER"
DEFAULT_DOCUMENT_PRESENTER = "stapel_docs.presenters.DocumentPresenter"
REVISION_PRESENTER_KEY = "DOCS_REVISION_PRESENTER"
DEFAULT_REVISION_PRESENTER = "stapel_docs.presenters.RevisionPresenter"

declare_swap(FOLDER_PRESENTER_KEY, DEFAULT_FOLDER_PRESENTER)
declare_swap(DOCUMENT_PRESENTER_KEY, DEFAULT_DOCUMENT_PRESENTER)
declare_swap(REVISION_PRESENTER_KEY, DEFAULT_REVISION_PRESENTER)


def _spec_of(dao):
    """Effective type spec, or None when the type vanished from the registry
    (the document then degrades to file-type presentation — verdict §7.3)."""
    return get_doc_types().get(dao.type)


class FolderPresenter(Presenter):
    """Presents a Folder row as the API tree node.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "workspace_id": "5cc26b64-0717-4562-b3fc-2c963f66a001",
            "parent_id": null,
            "name": "Meetings",
            "created_at": "2026-08-09T10:00:00+00:00",
            "updated_at": "2026-08-09T10:00:00+00:00",
            "deleted_at": null,
            "is_starred": false
        }
    """

    model = Folder
    fields = ("name",)
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "workspace_id": PresenterField(type=str, source=lambda dao: str(dao.workspace_id)),
        "parent_id": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.parent_id) if dao.parent_id else None,
            default=None,
            help_text="Parent folder id; null for workspace roots.",
        ),
        "created_at": PresenterField(type=str, source=lambda dao: dao.created_at.isoformat()),
        "updated_at": PresenterField(type=str, source=lambda dao: dao.updated_at.isoformat()),
        "deleted_at": PresenterField(
            type=Optional[str],
            source=lambda dao: dao.deleted_at.isoformat() if dao.deleted_at else None,
            default=None,
            help_text="Set while the folder sits in the trash.",
        ),
        "is_starred": PresenterField(
            type=Optional[bool],
            source=lambda dao: getattr(dao, "is_starred", None),
            default=None,
            help_text="Whether the requesting user starred this; null when "
            "the request carries no user (not applicable is not false).",
        ),
    }


class DocumentPresenter(Presenter):
    """Presents a Document row as the API envelope. The three registry-derived
    fields (editor_hint/collab/diffable) degrade to file-type presentation
    when the type is unknown — never an error (storage-verdict §7.3).

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "workspace_id": "5cc26b64-0717-4562-b3fc-2c963f66a001",
            "folder_id": null,
            "type": "md",
            "title": "Notes",
            "head_seq": 3,
            "snapshot_seq": 3,
            "size_bytes": 42,
            "mime_type": "",
            "metadata": {},
            "editor_hint": "markdown",
            "collab": "snapshot",
            "diffable": true,
            "created_at": "2026-08-09T10:00:00+00:00",
            "updated_at": "2026-08-09T10:05:00+00:00",
            "deleted_at": null,
            "is_starred": false
        }
    """

    model = Document
    fields = ("type", "title", "head_seq", "snapshot_seq", "size_bytes", "mime_type")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "workspace_id": PresenterField(type=str, source=lambda dao: str(dao.workspace_id)),
        "folder_id": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.folder_id) if dao.folder_id else None,
            default=None,
            help_text="Containing folder id; null for the workspace root.",
        ),
        "metadata": PresenterField(type=dict, source=lambda dao: dao.metadata or {}),
        "editor_hint": PresenterField(
            type=str,
            source=lambda dao: (_spec_of(dao).editor_hint if _spec_of(dao) else ""),
            help_text='Frontend editor dispatch key ("" = download-only).',
        ),
        "collab": PresenterField(
            type=str,
            source=lambda dao: (_spec_of(dao).collab if _spec_of(dao) else COLLAB_SNAPSHOT),
            help_text='Write discipline of the type: "crdt" or "snapshot".',
        ),
        "diffable": PresenterField(
            type=bool,
            source=lambda dao: bool(_spec_of(dao).diffable) if _spec_of(dao) else False,
            help_text="Whether line-diff rendering is meaningful for this type.",
        ),
        "created_at": PresenterField(type=str, source=lambda dao: dao.created_at.isoformat()),
        "updated_at": PresenterField(type=str, source=lambda dao: dao.updated_at.isoformat()),
        "deleted_at": PresenterField(
            type=Optional[str],
            source=lambda dao: dao.deleted_at.isoformat() if dao.deleted_at else None,
            default=None,
            help_text="Set while the document sits in the trash.",
        ),
        "is_starred": PresenterField(
            type=Optional[bool],
            source=lambda dao: getattr(dao, "is_starred", None),
            default=None,
            help_text="Whether the requesting user starred this; null when "
            "the request carries no user (not applicable is not false).",
        ),
    }


class RevisionPresenter(Presenter):
    """Presents a Revision pointer row (a self-contained full snapshot, I1).

    Example:
        {
            "id": "9aa85f64-5717-4562-b3fc-2c963f66afa6",
            "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "seq": 3,
            "kind": "named",
            "name": "Before rewrite",
            "size_bytes": 42,
            "created_by": null,
            "created_at": "2026-08-09T10:05:00+00:00"
        }
    """

    model = Revision
    fields = ("seq", "kind", "name", "size_bytes")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "document_id": PresenterField(type=str, source=lambda dao: str(dao.document_id)),
        "created_by": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.created_by_id) if dao.created_by_id else None,
            default=None,
        ),
        "created_at": PresenterField(type=str, source=lambda dao: dao.created_at.isoformat()),
    }


def get_folder_presenter() -> type[Presenter]:
    """The active (possibly host-swapped) folder presenter."""
    return get_presenter(FOLDER_PRESENTER_KEY, default=DEFAULT_FOLDER_PRESENTER)


def get_document_presenter() -> type[Presenter]:
    """The active (possibly host-swapped) document presenter."""
    return get_presenter(DOCUMENT_PRESENTER_KEY, default=DEFAULT_DOCUMENT_PRESENTER)


def get_revision_presenter() -> type[Presenter]:
    """The active (possibly host-swapped) revision presenter."""
    return get_presenter(REVISION_PRESENTER_KEY, default=DEFAULT_REVISION_PRESENTER)


# ── Envelope builders (no backing model) ─────────────────────────────


def present_save_result(head_seq: int, revision) -> SaveResultDTO:
    return SaveResultDTO(
        head_seq=head_seq,
        revision_id=str(revision.id) if revision is not None else None,
    )


def present_append_result(head_seq: int) -> AppendResultDTO:
    return AppendResultDTO(head_seq=head_seq)


def present_updates_feed(rows, head_seq: int) -> UpdatesFeedDTO:
    return UpdatesFeedDTO(
        head_seq=head_seq,
        updates=[
            JournalUpdateDTO(
                seq=row.seq,
                payload=base64.b64encode(bytes(row.payload)).decode("ascii"),
                author_id=str(row.author_id) if row.author_id else None,
                created_at=row.created_at.isoformat(),
            )
            for row in rows
        ],
    )


def present_resync(document) -> ResyncDTO:
    return ResyncDTO(
        resync=True, head_seq=document.head_seq, snapshot_seq=document.snapshot_seq
    )


def present_download_url(url: str) -> DownloadUrlDTO:
    return DownloadUrlDTO(url=url)


def present_upload_ticket(session, put_url: str) -> UploadTicketDTO:
    return UploadTicketDTO(
        upload_id=str(session.id),
        document_id=str(session.document_id),
        key=session.key,
        put_url=put_url,
        expires_at=session.expires_at.isoformat() if session.expires_at else None,
    )


def present_purge_result(folders: int, documents: int) -> TrashPurgeResultDTO:
    return TrashPurgeResultDTO(folders=folders, documents=documents)


def present_search_hits(hits) -> list[SearchHitDTO]:
    """Build the mixed folder/document hit list of ``GET /search``.

    A hit is not a folder envelope and not a document envelope — it is the
    shape a result row needs (kind + name + where it lives), so it gets its
    own DTO rather than a union that is half-null either way.
    """
    built = []
    for kind, row, breadcrumb in hits:
        built.append(
            SearchHitDTO(
                kind=kind,
                id=str(row.id),
                workspace_id=str(row.workspace_id),
                name=row.name if kind == "folder" else row.title,
                parent_id=(
                    str(row.parent_id)
                    if kind == "folder" and row.parent_id
                    else str(row.folder_id)
                    if kind == "document" and row.folder_id
                    else None
                ),
                type=None if kind == "folder" else row.type,
                is_starred=getattr(row, "is_starred", None),
                breadcrumb=[
                    BreadcrumbNodeDTO(id=str(node_id), name=name)
                    for node_id, name in breadcrumb
                ],
            )
        )
    return built


__all__ = [
    "FOLDER_PRESENTER_KEY",
    "DOCUMENT_PRESENTER_KEY",
    "REVISION_PRESENTER_KEY",
    "DEFAULT_FOLDER_PRESENTER",
    "DEFAULT_DOCUMENT_PRESENTER",
    "DEFAULT_REVISION_PRESENTER",
    "FolderPresenter",
    "DocumentPresenter",
    "RevisionPresenter",
    "get_folder_presenter",
    "get_document_presenter",
    "get_revision_presenter",
    "present_save_result",
    "present_append_result",
    "present_updates_feed",
    "present_resync",
    "present_download_url",
    "present_upload_ticket",
    "present_purge_result",
    "present_search_hits",
]
