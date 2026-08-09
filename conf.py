"""Settings namespace for stapel-docs.

All configuration is read through ``docs_settings`` (lazily, at call
time) — never via module-level ``os.getenv`` (values would freeze at import).
Resolution order per key: ``settings.STAPEL_DOCS`` dict -> flat Django
setting of the same name -> environment variable -> default below.

Dotted-path keys listed in ``import_strings`` are resolved with
``import_string`` — the fork-free escape hatch for swappable behavior.
"""
from stapel_core.conf import AppSettings

docs_settings = AppSettings(
    "STAPEL_DOCS",
    defaults={
        # Example knob — replace with real settings, document each in
        # MODULE.md ("Settings" table) as you add them.
        "GREETING": "pong",
    },
    import_strings=(),
)

__all__ = ["docs_settings"]
