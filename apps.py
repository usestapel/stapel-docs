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
        # the user.merged consumer + the INGEST seam.
        from . import actions

        actions.wire_ingest()

        # GDPR provider registration (monolith mode).
        from stapel_core.gdpr import gdpr_registry

        from .gdpr import DocsGDPRProvider

        if DocsGDPRProvider().section not in gdpr_registry.sections:
            gdpr_registry.register(DocsGDPRProvider())

        # The erasure protocol (stapel-gdpr 0.5.0+), implemented once in
        # stapel-core: gdpr.erasure.requested -> erase -> gdpr.section.erased
        # with a deterministic receipt inside the erase's transaction, the
        # gdpr.owner.probe answer from the same module, and the deprecated
        # user.deleted. What stays ours is erase_subject (erasure.py).
        #
        # Registering by name is also what stands core's provider bridge
        # down for this section exactly: until 0.10.0 this module carried
        # its own copy of the protocol, and the bridge could only tell they
        # were the same APP, not the same section (gdpr.W012).
        from stapel_core.gdpr import register_gdpr_owner

        from .erasure import OWNER, SUBJECT_TYPES, erase_subject

        register_gdpr_owner(OWNER, SUBJECT_TYPES, erase_subject)
