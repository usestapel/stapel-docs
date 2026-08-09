"""comm surface of stapel-docs.

Every Function/Action carries a JSON schema in ``schemas/`` — tests run
with ``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails
loudly. Registration happens on import from ``apps.py:ready()``; re-imports
are no-ops.
"""
from stapel_core.comm import function

from .conf import docs_settings


@function("docs.ping")
def ping(payload):
    """Scaffold example Function — replace with the real comm surface.

    Input: ``{}``; output: ``{"greeting": str}``.
    """
    return {"greeting": docs_settings.GREETING}
