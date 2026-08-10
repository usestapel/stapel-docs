"""Canonical-prefix URLconf for contract emission (contract-pipeline.md §2).

Mounts the module root at ``docs/`` — the module's own ``urls.py`` bakes the
``api/v1/`` segment in (api-versioning.md §2), so the resulting public prefix
is ``/docs/api/v1/…``, exactly the mount recipe ``urls.py`` documents for
hosts (``path("docs/", include("stapel_docs.urls"))``).

This file exists (rather than pointing the harness at the bare test urlconf,
``stapel_docs.tests.urls``) so the contract-emission mount is declared
independently of the test layout and can never silently drift from the
module's documented public mount recipe (contract-pipeline.md §2, §9) — the
same one-small-file-per-concern shape as every other pair-backend's
``codegen_urls.py``.

stapel-docs is **not mounted in stapel-example-monolith** (grep-confirmed
2026-08-10: no ``include("stapel_docs.urls")`` in ``svc-app/core/urls.py``) —
there is no monolith aggregate slice to reproduce byte-for-byte. Validation in
``tests/test_contract.py`` is therefore standalone (determinism + closure +
canonical prefix + security presence), the recordings precedent.
"""
from django.urls import include, path

urlpatterns = [
    path("docs/", include("stapel_docs.urls")),
]
