from django.apps import AppConfig


class DocsConfig(AppConfig):
    name = "stapel_docs"
    label = "docs"
    verbose_name = "Documents: storage, revisions and per-type editors"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects: comm functions/actions, system checks,
        # error-key registration. Keep each in its own module.
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from . import functions  # noqa: F401

        # Action subscriptions (in-process in a monolith, bus consumer in
        # microservices — same code, transport chosen by STAPEL_COMM):
        # user.deleted consumer + the INGEST seam.
        from . import actions

        actions.wire_ingest()

        # GDPR provider registration (monolith mode).
        from stapel_core.gdpr import gdpr_registry

        from .gdpr import DocsGDPRProvider

        if DocsGDPRProvider().section not in gdpr_registry.sections:
            gdpr_registry.register(DocsGDPRProvider())
