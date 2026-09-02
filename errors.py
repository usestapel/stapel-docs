"""i18n error keys of stapel-docs.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_400_UNKNOWN_TYPE = "error.400.docs_unknown_type"
ERR_400_TYPE_NOT_EDITABLE = "error.400.docs_type_not_editable"
ERR_400_UPDATES_NOT_CRDT = "error.400.docs_updates_not_crdt"
ERR_400_INVALID_CRDT_PAYLOAD = "error.400.docs_invalid_crdt_payload"
ERR_400_FOLDER_DEPTH = "error.400.docs_folder_depth"
ERR_400_FOLDER_CYCLE = "error.400.docs_folder_cycle"
ERR_400_DUPLICATE_NAME = "error.400.docs_duplicate_name"
ERR_400_EXPORT_FORMAT = "error.400.docs_export_format"
ERR_400_BAD_SINCE = "error.400.docs_bad_since"
ERR_400_NOT_TRASHED = "error.400.docs_not_trashed"
ERR_400_UPLOAD_STATE = "error.400.docs_upload_state"
ERR_400_UPLOAD_MISMATCH = "error.400.docs_upload_mismatch"
ERR_400_UPLOAD_MIME = "error.400.docs_upload_mime"
ERR_400_UPLOAD_EXPIRED = "error.400.docs_upload_expired"
ERR_400_UPLOAD_UNMEASURABLE = "error.400.docs_upload_unmeasurable"
ERR_400_TOO_MANY_UPDATES = "error.400.docs_too_many_updates"
ERR_400_TOO_MANY_UPLOADS = "error.400.docs_too_many_uploads"
ERR_400_THUMBNAIL_TIER = "error.400.docs_thumbnail_tier"
ERR_400_THUMBNAIL_UNSUPPORTED = "error.400.docs_thumbnail_unsupported"
ERR_400_SHARE_MODE_DISABLED = "error.400.docs_share_mode_disabled"
ERR_400_SHARE_LEVEL = "error.400.docs_share_level"
ERR_400_SHARE_SUBJECT = "error.400.docs_share_subject"
ERR_400_SHARE_REF_KIND = "error.400.docs_share_ref_kind"
ERR_401_SHARE_AUTH = "error.401.docs_share_auth_required"
ERR_403_FORBIDDEN = "error.403.docs_forbidden"
ERR_403_UPLOAD_OWNER = "error.403.docs_upload_owner"
ERR_404_DOCUMENT = "error.404.docs_document_not_found"
ERR_404_FOLDER = "error.404.docs_folder_not_found"
ERR_404_REVISION = "error.404.docs_revision_not_found"
ERR_404_UPLOAD = "error.404.docs_upload_not_found"
ERR_404_SHARE_ACCESS = "error.404.docs_access_not_found"
ERR_404_SHARE_LINK = "error.404.docs_link_not_found"
ERR_409_SEQ_CONFLICT = "error.409.docs_seq_conflict"
ERR_412_MISSING_IF_MATCH = "error.412.docs_missing_if_match"
ERR_413_BODY_TOO_LARGE = "error.413.docs_body_too_large"
ERR_413_UPDATE_TOO_LARGE = "error.413.docs_update_too_large"
ERR_413_UPLOAD_TOO_LARGE = "error.413.docs_upload_too_large"
ERR_413_EXPORT_TOO_LARGE = "error.413.docs_export_too_large"
ERR_507_WORKSPACE_QUOTA = "error.507.docs_workspace_quota"
ERR_503_WORKSPACES = "error.503.docs_workspaces_unavailable"
ERR_503_EXPORTER = "error.503.docs_exporter_unavailable"
ERR_503_DOWNLOAD_URL = "error.503.docs_download_url_unavailable"
ERR_503_THUMBNAILS = "error.503.docs_thumbnails_unavailable"

STAPEL_DOCS_ERRORS = {
    ERR_400_UNKNOWN_TYPE: "Unknown document type",
    ERR_400_TYPE_NOT_EDITABLE: "This document type has no editable body",
    ERR_400_UPDATES_NOT_CRDT: "Update journal writes are only legal for crdt-discipline types",
    ERR_400_INVALID_CRDT_PAYLOAD: "The payload is not a valid CRDT update for this document type",
    ERR_400_FOLDER_DEPTH: "Folder tree depth limit exceeded",
    ERR_400_FOLDER_CYCLE: "A folder cannot be moved under itself",
    ERR_400_DUPLICATE_NAME: "An item with this name already exists here",
    ERR_400_EXPORT_FORMAT: "Unknown export format",
    ERR_400_BAD_SINCE: "Invalid ?since= sequence number",
    ERR_400_NOT_TRASHED: "The item is not in the trash",
    ERR_400_UPLOAD_STATE: "Upload session is not in an operable state",
    ERR_400_UPLOAD_MISMATCH: "The uploaded object does not match what the upload session declared",
    ERR_400_UPLOAD_MIME: "This content type may not be uploaded",
    ERR_400_UPLOAD_EXPIRED: "The upload session has expired",
    ERR_400_UPLOAD_UNMEASURABLE: "The size of the uploaded object could not be determined",
    ERR_400_TOO_MANY_UPDATES: "Too many updates in one request",
    ERR_400_TOO_MANY_UPLOADS: "Too many upload sessions are already open in this workspace",
    ERR_400_THUMBNAIL_TIER: "Unknown thumbnail size",
    ERR_400_THUMBNAIL_UNSUPPORTED: "This document has no image preview",
    ERR_400_SHARE_MODE_DISABLED: "This way of sharing is switched off for this deployment",
    ERR_400_SHARE_LEVEL: "That access level may not be granted here",
    ERR_400_SHARE_SUBJECT: "A share grant names exactly one subject",
    ERR_400_SHARE_REF_KIND: "No resolver is registered for this kind of reference",
    ERR_401_SHARE_AUTH: "Sign in to open this shared document",
    ERR_403_FORBIDDEN: "You do not have access to this document",
    ERR_403_UPLOAD_OWNER: "Only the user who opened this upload may finalize it",
    ERR_404_DOCUMENT: "Document not found",
    ERR_404_FOLDER: "Folder not found",
    ERR_404_REVISION: "Revision not found",
    ERR_404_UPLOAD: "Upload session not found",
    ERR_404_SHARE_ACCESS: "Share grant not found",
    ERR_404_SHARE_LINK: "Share link not found",
    ERR_409_SEQ_CONFLICT: "A newer version was saved by someone else",
    ERR_412_MISSING_IF_MATCH: "Snapshot saves require an If-Match sequence",
    ERR_413_BODY_TOO_LARGE: "The document body exceeds the size limit",
    ERR_413_UPDATE_TOO_LARGE: "The update payload exceeds the size limit",
    ERR_413_UPLOAD_TOO_LARGE: "The upload exceeds the size limit",
    ERR_413_EXPORT_TOO_LARGE: "The document is too large to export",
    ERR_507_WORKSPACE_QUOTA: "The workspace storage quota is exhausted",
    ERR_503_WORKSPACES: "Workspace membership service is unavailable",
    ERR_503_EXPORTER: "The export backend is not installed",
    ERR_503_DOWNLOAD_URL: "Download links are not available with this storage configuration",
    ERR_503_THUMBNAILS: "The thumbnail renderer is not installed",
}

register_service_errors(STAPEL_DOCS_ERRORS)

__all__ = [name for name in dir() if name.startswith("ERR_")] + ["STAPEL_DOCS_ERRORS"]
