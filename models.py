"""Models for stapel-docs.

House rules (docs/library-standard.md §3.8):
- cross-service references are UUID fields, not FKs;
- the user model is only ``settings.AUTH_USER_MODEL``;
- index names must be <= 30 characters (models.E034);
- journal-style models get a read-only ModelAdmin.
"""
# from django.db import models
