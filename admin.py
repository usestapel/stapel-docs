"""Admin for stapel-docs.

Read-only across the board: every row here is workspace content or write
machinery with no staff add/change/delete workflow — mutations go through
the API (where authorize() and the outbox emits live), never the admin.
"""
from django.contrib import admin

from .models import (
    Document,
    DocumentAccess,
    DocumentLink,
    DocumentUpdate,
    Folder,
    Revision,
    UploadSession,
)


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Folder)
class FolderAdmin(_ReadOnlyAdmin):
    list_display = ("id", "name", "workspace_id", "parent", "deleted_at", "created_at")
    search_fields = ("id", "name", "workspace_id")


@admin.register(Document)
class DocumentAdmin(_ReadOnlyAdmin):
    list_display = (
        "id", "title", "type", "workspace_id", "head_seq", "size_bytes",
        "deleted_at", "created_at",
    )
    list_filter = ("type",)
    search_fields = ("id", "title", "workspace_id")


@admin.register(DocumentUpdate)
class DocumentUpdateAdmin(_ReadOnlyAdmin):
    list_display = ("id", "document", "seq", "author_id", "created_at")


@admin.register(Revision)
class RevisionAdmin(_ReadOnlyAdmin):
    list_display = ("id", "document", "seq", "kind", "name", "size_bytes", "created_at")
    list_filter = ("kind",)


@admin.register(UploadSession)
class UploadSessionAdmin(_ReadOnlyAdmin):
    list_display = ("id", "title", "workspace_id", "state", "size_bytes", "created_at")
    list_filter = ("state",)


@admin.register(DocumentAccess)
class DocumentAccessAdmin(_ReadOnlyAdmin):
    list_display = (
        "id", "document", "subject_kind", "subject", "level", "granted_by",
        "created_at",
    )
    list_filter = ("subject_kind", "level")
    search_fields = ("id", "user_id", "ref", "workspace_id")


@admin.register(DocumentLink)
class DocumentLinkAdmin(_ReadOnlyAdmin):
    """The token is MASKED, never listed (the ``WorkspaceInvitation`` admin's
    rule): the model is ``@access.secret``, so the row is superuser-only to
    begin with, and a bearer secret rendered into a list view is a secret in
    a screenshot, a support ticket and a browser history."""

    list_display = (
        "id", "document", "level", "status", "expires_at", "revoked_at",
        "first_redeemed_at", "created_by", "created_at",
    )
    list_filter = ("level",)
    search_fields = ("id", "workspace_id")
    exclude = ("token",)
