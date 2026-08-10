"""Retention expiry for the docs trash (storage-verdict §9.3).

Purges every item soft-deleted longer than TRASH_RETENTION_DAYS ago —
rows, journal and storage objects, irreversibly. The audit trail is the
emitted ``document.*`` events, not recoverable bytes.

    python manage.py docs_purge_expired
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Purge trash items older than TRASH_RETENTION_DAYS"

    def handle(self, *args, **options):
        from ...services import purge_expired

        folders, documents = purge_expired()
        self.stdout.write(
            f"docs_purge_expired: purged {documents} documents, {folders} folders"
        )
