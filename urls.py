"""Root URLconf for stapel-docs — v1 canon mount (api-versioning.md §2).

Canon: ``/<mod>/api/v1/...`` — the version segment sits right after ``api/``;
bare ``/<mod>/api/...`` paths do not exist. The host project mounts this
module root:

    path("docs/", include("stapel_docs.urls"))   # -> /docs/api/v1/...

The actual v1 URL set lives in ``urls_v1.py``; a ``v2`` appears only when a
classified breaking change forces it (api-versioning.md §3).
"""
from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("stapel_docs.urls_v1")),
]
