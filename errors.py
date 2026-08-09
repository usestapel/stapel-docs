"""i18n error keys of stapel-docs.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_400_EXAMPLE = "error.400.docs_example"

STAPEL_DOCS_ERRORS = {
    ERR_400_EXAMPLE: "Example error — replace with real keys",
}

register_service_errors(STAPEL_DOCS_ERRORS)

__all__ = ["STAPEL_DOCS_ERRORS", "ERR_400_EXAMPLE"]
