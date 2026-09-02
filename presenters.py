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
    ArchiveEntryDTO,
    ArchiveListingDTO,
    BreadcrumbNodeDTO,
    DownloadUrlDTO,
    JournalUpdateDTO,
    ResyncDTO,
    SaveResultDTO,
    SearchHitDTO,
    SharedDocumentDTO,
    TrashPurgeResultDTO,
    UpdatesFeedDTO,
    UploadTicketDTO,
)
from .models import Document, DocumentAccess, DocumentLink, Folder, Revision

FOLDER_PRESENTER_KEY = "DOCS_FOLDER_PRESENTER"
DEFAULT_FOLDER_PRESENTER = "stapel_docs.presenters.FolderPresenter"
DOCUMENT_PRESENTER_KEY = "DOCS_DOCUMENT_PRESENTER"
DEFAULT_DOCUMENT_PRESENTER = "stapel_docs.presenters.DocumentPresenter"
REVISION_PRESENTER_KEY = "DOCS_REVISION_PRESENTER"
DEFAULT_REVISION_PRESENTER = "stapel_docs.presenters.RevisionPresenter"
ACCESS_PRESENTER_KEY = "DOCS_ACCESS_PRESENTER"
DEFAULT_ACCESS_PRESENTER = "stapel_docs.presenters.DocumentAccessPresenter"
LINK_PRESENTER_KEY = "DOCS_LINK_PRESENTER"
DEFAULT_LINK_PRESENTER = "stapel_docs.presenters.DocumentLinkPresenter"

declare_swap(FOLDER_PRESENTER_KEY, DEFAULT_FOLDER_PRESENTER)
declare_swap(DOCUMENT_PRESENTER_KEY, DEFAULT_DOCUMENT_PRESENTER)
declare_swap(REVISION_PRESENTER_KEY, DEFAULT_REVISION_PRESENTER)
declare_swap(ACCESS_PRESENTER_KEY, DEFAULT_ACCESS_PRESENTER)
declare_swap(LINK_PRESENTER_KEY, DEFAULT_LINK_PRESENTER)


def _spec_of(dao):
    """Effective type spec, or None when the type vanished from the registry
    (the document then degrades to file-type presentation — verdict §7.3)."""
    return get_doc_types().get(dao.type)


def _socket_path(dao):
    """``ws/docs/<id>`` when this deployment serves the docs socket, else
    None — a polling-only host must not advertise an address nothing
    answers on (the chat canon: the envelope carries its own live path)."""
    from . import realtime

    return realtime.socket_path(dao.id) if realtime.socket_available() else None


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
            "socket_path": null,
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
        "socket_path": PresenterField(
            type=Optional[str],
            source=_socket_path,
            default=None,
            help_text="Where to open the document's realtime stream "
            "(ws/docs/<id>), relative to the deployment's WebSocket prefix; "
            "null when this deployment serves no socket — clients then poll "
            "the ?since= feed, which is first-class.",
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


class DocumentAccessPresenter(Presenter):
    """Presents one whitelist grant as a share-sheet row.

    ``suspended`` is the kill-switch state (axis §3): true when the row
    exists but its mode is switched off, so the sheet says "paused by
    configuration" instead of hiding a grant the admin would then believe
    was revoked.

    Example:
        {
            "id": "1aa85f64-5717-4562-b3fc-2c963f66afa6",
            "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "subject_kind": "user",
            "subject": "7bb85f64-5717-4562-b3fc-2c963f66af11",
            "level": "view",
            "granted_by": null,
            "suspended": false,
            "created_at": "2026-09-02T10:00:00+00:00"
        }
    """

    model = DocumentAccess
    fields = ("subject_kind", "level")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "document_id": PresenterField(type=str, source=lambda dao: str(dao.document_id)),
        "subject": PresenterField(
            type=str,
            source=lambda dao: dao.subject,
            help_text="The user id for subject_kind=user, the container "
            "reference for subject_kind=ref.",
        ),
        "granted_by": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.granted_by_id) if dao.granted_by_id else None,
            default=None,
        ),
        "suspended": PresenterField(
            type=bool,
            source=lambda dao: bool(getattr(dao, "is_suspended", False)),
            default=False,
            help_text="The grant exists but its sharing mode is switched off "
            "for this deployment — inert, not revoked.",
        ),
        "created_at": PresenterField(type=str, source=lambda dao: dao.created_at.isoformat()),
    }


class DocumentLinkPresenter(Presenter):
    """Presents one bearer link to whoever may administer sharing.

    The token IS in this envelope — a share sheet that cannot re-show the
    link it minted is a sheet that makes people mint a second one — which is
    exactly why the listing endpoint is gated on ``docs.share.link`` and why
    no ``document.share.*`` EVENT carries it.

    Example:
        {
            "id": "4cc85f64-5717-4562-b3fc-2c963f66afa6",
            "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "token": "0xk3…",
            "level": "view",
            "status": "active",
            "expires_at": "2026-10-02T10:00:00+00:00",
            "revoked_at": null,
            "first_redeemed_at": null,
            "created_by": null,
            "suspended": false,
            "created_at": "2026-09-02T10:00:00+00:00"
        }
    """

    model = DocumentLink
    fields = ("token", "level")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "document_id": PresenterField(type=str, source=lambda dao: str(dao.document_id)),
        "status": PresenterField(
            type=str,
            source=lambda dao: dao.status,
            help_text='Derived: "revoked" beats "expired" beats "active".',
        ),
        "expires_at": PresenterField(
            type=str, source=lambda dao: dao.expires_at.isoformat()
        ),
        "revoked_at": PresenterField(
            type=Optional[str],
            source=lambda dao: dao.revoked_at.isoformat() if dao.revoked_at else None,
            default=None,
        ),
        "first_redeemed_at": PresenterField(
            type=Optional[str],
            source=lambda dao: (
                dao.first_redeemed_at.isoformat() if dao.first_redeemed_at else None
            ),
            default=None,
            help_text="When the link was first opened successfully; stamped once.",
        ),
        "created_by": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.created_by_id) if dao.created_by_id else None,
            default=None,
        ),
        "suspended": PresenterField(
            type=bool,
            source=lambda dao: bool(getattr(dao, "is_suspended", False)),
            default=False,
            help_text="The link exists but link sharing is switched off for "
            "this deployment — inert, not revoked.",
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


def get_access_presenter() -> type[Presenter]:
    """The active (possibly host-swapped) share-grant presenter."""
    return get_presenter(ACCESS_PRESENTER_KEY, default=DEFAULT_ACCESS_PRESENTER)


def get_link_presenter() -> type[Presenter]:
    """The active (possibly host-swapped) share-link presenter."""
    return get_presenter(LINK_PRESENTER_KEY, default=DEFAULT_LINK_PRESENTER)


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


def present_archive_listing(listing: dict) -> ArchiveListingDTO:
    """Build the zip-as-folder listing from ``archives.list_entries`` data."""
    return ArchiveListingDTO(
        entry_count=listing["entry_count"],
        total_uncompressed_bytes=listing["total_uncompressed_bytes"],
        archive_encrypted=listing["archive_encrypted"],
        entries=[ArchiveEntryDTO(**entry) for entry in listing["entries"]],
    )


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


def present_shared_document(document, level: str) -> SharedDocumentDTO:
    """Build the stripped bearer envelope (axis §6).

    A separate DTO rather than the document envelope with fields blanked:
    a null ``workspace_id`` still tells the holder a workspace field exists
    and invites a client to ask for it, and a shape that has to remember to
    blank things is a shape that will forget one day.
    """
    spec = _spec_of(document)
    return SharedDocumentDTO(
        id=str(document.id),
        type=document.type,
        title=document.title,
        head_seq=document.head_seq,
        size_bytes=document.size_bytes,
        mime_type=document.mime_type,
        editor_hint=spec.editor_hint if spec else "",
        collab=spec.collab if spec else COLLAB_SNAPSHOT,
        diffable=bool(spec.diffable) if spec else False,
        level=level,
        updated_at=document.updated_at.isoformat(),
    )


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
    "ACCESS_PRESENTER_KEY",
    "LINK_PRESENTER_KEY",
    "DEFAULT_FOLDER_PRESENTER",
    "DEFAULT_DOCUMENT_PRESENTER",
    "DEFAULT_REVISION_PRESENTER",
    "DEFAULT_ACCESS_PRESENTER",
    "DEFAULT_LINK_PRESENTER",
    "FolderPresenter",
    "DocumentPresenter",
    "RevisionPresenter",
    "DocumentAccessPresenter",
    "DocumentLinkPresenter",
    "get_folder_presenter",
    "get_document_presenter",
    "get_revision_presenter",
    "get_access_presenter",
    "get_link_presenter",
    "present_save_result",
    "present_append_result",
    "present_updates_feed",
    "present_resync",
    "present_download_url",
    "present_archive_listing",
    "present_upload_ticket",
    "present_purge_result",
    "present_shared_document",
    "present_search_hits",
]
