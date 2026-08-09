"""DRF views for stapel-docs.

Presenter-canonical from birth (§55): a view resolves its presenter through
``get_presenter`` (see ``presenters.py``) and returns
``StapelResponse(Serializer(presenter.present(...)))`` — it never
instantiates a ``dto.py`` dataclass itself (SWAP002) and never imports the
concrete presenter class (SWAP001).

Authorization: every view routes its decision through
``stapel_docs.authz.authorize`` — the single choke point (sharing-axis §7).
``deny`` -> 403, ``unavailable`` -> 503, never 403-on-outage.
"""


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
