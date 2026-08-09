"""Presenters for stapel-docs — the DTO-building layer (§55).

Presenter discipline (docs/pending/extensibility-presenters.md; enforced by
SWAP001/SWAP002 in `stapel-verify`): views NEVER instantiate a `dto.py`
dataclass directly — every DTO is built by a presenter resolved through
`get_presenter(KEY, default=...)`, so a host project can swap the
presentation of any endpoint via `STAPEL_SWAP` without forking this module.

The scaffold ships a working example over the ping endpoint. It has no DAO
model yet (`models.py` is empty at scaffold time), so `PingPresenter` is a
plain presenter class with a `present()` method. The moment you add a real
model, base its presenter on stapel-core's DAO→DTO primitive instead —
`stapel_core.django.api.presenters.Presenter` (declares `model`/`fields`/
`custom_fields`, generates the DTO dataclass AND the serializer, lands in
the auto-catalog PRESENTERS.MD) — and keep the same declare_swap/
get_presenter plumbing shown here. Etalon:
stapel_core/django/users/presenters.py.
"""
from stapel_core.django.swappable import declare_swap, get_presenter

from .conf import docs_settings
from .dto import PingResponse

#: Swap key for the host presenter override (STAPEL_SWAP registry).
PING_PRESENTER_KEY = "STAPEL_DOCS_PING_PRESENTER"

#: Dotted path of the default presenter — single source for both the
#: declare_swap() catalog registration and the get_presenter() fallback.
DEFAULT_PING_PRESENTER = "stapel_docs.presenters.PingPresenter"

# Import-time declaration: makes the swap point visible to the auto-catalog
# (PRESENTERS.MD, `manage.py presenter_catalog`) even before the first
# get_ping_presenter() call.
declare_swap(PING_PRESENTER_KEY, DEFAULT_PING_PRESENTER)


class PingPresenter:
    """Builds the ping response DTO — the only place PingResponse is
    instantiated (SWAP002: views go through a presenter, never the DTO)."""

    def present(self) -> PingResponse:
        return PingResponse(greeting=docs_settings.GREETING)


def get_ping_presenter() -> type:
    """The active (possibly host-swapped) ping presenter.

    Consumers call this instead of importing :class:`PingPresenter`
    directly — a direct import is exactly what a
    ``STAPEL_SWAP["STAPEL_DOCS_PING_PRESENTER"]`` override would silently
    fail to reach (SWAP001, ``stapel_tools.swap_lint``).
    """
    return get_presenter(PING_PRESENTER_KEY, default=DEFAULT_PING_PRESENTER)


__all__ = [
    "PING_PRESENTER_KEY",
    "DEFAULT_PING_PRESENTER",
    "PingPresenter",
    "get_ping_presenter",
]
