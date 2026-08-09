"""i18n error keys of stapel-docs.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_400_UNKNOWN_TYPE = "error.400.docs_unknown_type"
ERR_400_TYPE_NOT_EDITABLE = "error.400.docs_type_not_editable"
ERR_400_UPDATES_NOT_CRDT = "error.400.docs_updates_not_crdt"
ERR_400_FOLDER_DEPTH = "error.400.docs_folder_depth"
ERR_400_FOLDER_CYCLE = "error.400.docs_folder_cycle"
ERR_400_DUPLICATE_NAME = "error.400.docs_duplicate_name"
ERR_400_EXPORT_FORMAT = "error.400.docs_export_format"
ERR_400_BAD_SINCE = "error.400.docs_bad_since"
ERR_400_NOT_TRASHED = "error.400.docs_not_trashed"
ERR_400_UPLOAD_STATE = "error.400.docs_upload_state"
ERR_403_FORBIDDEN = "error.403.docs_forbidden"
ERR_404_DOCUMENT = "error.404.docs_document_not_found"
ERR_404_FOLDER = "error.404.docs_folder_not_found"
ERR_404_REVISION = "error.404.docs_revision_not_found"
ERR_404_UPLOAD = "error.404.docs_upload_not_found"
ERR_409_SEQ_CONFLICT = "error.409.docs_seq_conflict"
ERR_412_MISSING_IF_MATCH = "error.412.docs_missing_if_match"
ERR_503_WORKSPACES = "error.503.docs_workspaces_unavailable"
ERR_503_EXPORTER = "error.503.docs_exporter_unavailable"

STAPEL_DOCS_ERRORS = {
    ERR_400_UNKNOWN_TYPE: "Unknown document type",
    ERR_400_TYPE_NOT_EDITABLE: "This document type has no editable body",
    ERR_400_UPDATES_NOT_CRDT: "Update journal writes are only legal for crdt-discipline types",
    ERR_400_FOLDER_DEPTH: "Folder tree depth limit exceeded",
    ERR_400_FOLDER_CYCLE: "A folder cannot be moved under itself",
    ERR_400_DUPLICATE_NAME: "An item with this name already exists here",
    ERR_400_EXPORT_FORMAT: "Unknown export format",
    ERR_400_BAD_SINCE: "Invalid ?since= sequence number",
    ERR_400_NOT_TRASHED: "The item is not in the trash",
    ERR_400_UPLOAD_STATE: "Upload session is not in an operable state",
    ERR_403_FORBIDDEN: "You do not have access to this document",
    ERR_404_DOCUMENT: "Document not found",
    ERR_404_FOLDER: "Folder not found",
    ERR_404_REVISION: "Revision not found",
    ERR_404_UPLOAD: "Upload session not found",
    ERR_409_SEQ_CONFLICT: "A newer version was saved by someone else",
    ERR_412_MISSING_IF_MATCH: "Snapshot saves require an If-Match sequence",
    ERR_503_WORKSPACES: "Workspace membership service is unavailable",
    ERR_503_EXPORTER: "The export backend is not installed",
}

register_service_errors(STAPEL_DOCS_ERRORS)

__all__ = [name for name in dir() if name.startswith("ERR_")] + ["STAPEL_DOCS_ERRORS"]
