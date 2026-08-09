"""v1 URL set — paths here are relative to the ``api/v1/`` mount
contributed by the root ``urls.py`` (api-versioning.md §2).
"""
from django.urls import path

from .views import PingView

urlpatterns = [
    path("ping", PingView.as_view(), name="docs-ping"),
]
