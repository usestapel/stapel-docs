"""DRF views for stapel-docs.

Presenter-canonical from birth (§55): a view resolves its presenter through
``get_presenter`` (see ``presenters.py``) and returns
``StapelResponse(Serializer(presenter.present(...)))`` — it never
instantiates a ``dto.py`` dataclass itself (SWAP002) and never imports the
concrete presenter class (SWAP001).
"""
from drf_spectacular.utils import extend_schema
from rest_framework import permissions
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelResponse

from .presenters import get_ping_presenter
from .serializers import PingResponseSerializer


class SerializerSeamMixin:
    """Overridable serializer seam for every stapel-docs APIView.

    Host projects can swap the request/response serializer of any view by
    subclassing and setting ``request_serializer_class`` /
    ``response_serializer_class`` (or overriding the getters for
    per-request decisions) — no need to rewrite the HTTP method bodies.
    """

    request_serializer_class = None
    response_serializer_class = None

    def get_request_serializer_class(self):
        return self.request_serializer_class

    def get_response_serializer_class(self):
        return self.response_serializer_class


@extend_schema(tags=["Documents: storage, revisions and per-type editors"])
class PingView(SerializerSeamMixin, APIView):
    """Scaffold example — replace with real views, keep both seams
    (serializer mixin + presenter indirection)."""

    permission_classes = [permissions.AllowAny]
    response_serializer_class = PingResponseSerializer

    @extend_schema(responses={200: PingResponseSerializer})
    def get(self, request):
        response_cls = self.get_response_serializer_class()
        dto = get_ping_presenter()().present()
        return StapelResponse(response_cls(dto))
