"""Channels route — discovered, not hand-wired.

``stapel_realtime.build_websocket_application()`` walks INSTALLED_APPS and
collects every ``<app>.routing.websocket_urlpatterns``, so a host that
assembles its ASGI app the canonical way gets the docs socket without
naming it::

    # asgi.py — the whole file
    from django.core.asgi import get_asgi_application
    from stapel_realtime.asgi import build_websocket_application

    application = build_websocket_application(
        http_application=get_asgi_application()
    )

One mount: ``ws/docs/<document_id>`` — the document's update journal
(resumable, read-only; writes stay REST). Importing this module requires
the ``[realtime]`` extra (it imports the consumer, which imports Channels);
a polling-only host never imports it.
"""
from django.urls import path

from .consumers import DocUpdatesConsumer

websocket_urlpatterns = [
    path("ws/docs/<uuid:document_id>", DocUpdatesConsumer.as_asgi()),
]
