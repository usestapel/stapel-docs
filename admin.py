"""Admin for stapel-docs.

Read-only across the board: every row here is workspace content or write
machinery with no staff add/change/delete workflow — mutations go through
the API (where authorize() and the outbox emits live), never the admin.
"""
from django.contrib import admin

from .models import Document, DocumentUpdate, Folder, Revision, UploadSession


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
