"""Single-module Django settings for stapel-docs's contract harness.

The ``settings.configure(...)`` block for:

  - the contract-emission harness (``_codegen.py`` / ``make contract``) —
    mounts docs on its *canonical* public prefix
    (``stapel_docs.codegen_urls`` → ``docs/`` — the module's own ``urls.py``
    bakes the ``api/v1`` segment in, so the full canonical prefix is
    ``/docs/api/v1``) and enables drf-spectacular, so the emitted
    ``schema.json`` / ``flows.json`` paths match the module's documented
    mount recipe (``urls.py``: ``path("docs/", include("stapel_docs.urls"))``)
    (contract-pipeline.md §2); and
  - the capabilities emitter (``_capabilities.py``), which reuses
    ``_codegen._configure``.

Shape copied from stapel-recordings' harness (itself from the stapel-auth
etalon via stapel-profiles). One deliberate deviation from the recordings
mold: ``conftest.py`` keeps its historical inline ``settings.configure``
block instead of importing :func:`settings_kwargs` — the conftest belongs to
the test workstream, and folding it in here is its call. Until then this
file MIRRORS the conftest config (INSTALLED_APPS / comm / migrations) with
only the contract-required additions:

  - ``stapel_core.django.apps.CommonDjangoConfig`` + ``drf_spectacular`` —
    the conftest predates contract emission and carries neither;
    ``CommonDjangoConfig`` supplies the ``generate_flow_docs`` /
    ``generate_error_keys`` management commands the codegen harness calls;
  - ``contract=True`` swaps in the *production* ``REST_FRAMEWORK`` (the
    canonical stapel-core config, inlined as plain dotted paths — importing
    it would trip the same chicken-and-egg as spectacular). This matters for
    byte-identity: a real deployment emits with
    ``DEFAULT_SCHEMA_CLASS=PermissionAwareAutoSchema`` and the real
    permission/renderer classes, and DRF caches ``REST_FRAMEWORK`` on first
    access, so it must be right at ``configure()`` time.

``SPECTACULAR_SETTINGS`` is deliberately *not* set: drf-spectacular builds
its settings singleton at *import* time, before a ``configure()``-based
harness can populate it, so the emitter runs on drf defaults — the same
state every other pair-backend's harness emits under. The one knob that
still must be forced, ``SCHEMA_PATH_PREFIX``, is patched on the singleton
directly by the harness (see ``_codegen._configure``).
"""
from __future__ import annotations


def settings_kwargs(
    *,
    root_urlconf: str = "stapel_docs.tests.urls",
    contract: bool = False,
) -> dict:
    """Return the ``settings.configure(**kwargs)`` for a single-module docs
    instance. ``root_urlconf`` selects the mount: bare
    (``stapel_docs.tests.urls``) mirrors the test layout, canonical-prefix
    (``stapel_docs.codegen_urls`` → ``docs/``) is what contract emission
    uses."""
    if contract:
        # Mirror stapel_core.django.settings.REST_FRAMEWORK exactly (the
        # config a real deployment emits under; auth/profiles/recordings
        # inline the same block). Inlined, not imported, to dodge the
        # import-time settings read.
        rest_framework = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
                "rest_framework.renderers.BrowsableAPIRenderer",
            ],
            "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
            "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
        }
    else:
        rest_framework = None

    kwargs = dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.admin",
            "django.contrib.messages",
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            "rest_framework",
            "drf_spectacular",
            "stapel_docs",
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        # Same comm shape as the conftest: synchronous in-process comm with
        # schema validation ON. Schema emission never executes an action or
        # Function, so this only needs to be present, not exercised.
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        STAPEL_COMM={
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "VALIDATE_SCHEMAS": True,
        },
        MIGRATION_MODULES={
            "users": None,
            "docs": None,
        },
    )
    if rest_framework is not None:
        kwargs["REST_FRAMEWORK"] = rest_framework
    return kwargs


# The multi-module common path prefix drf-spectacular auto-detects when every
# pair-backend's schema is emitted inside an all-modules aggregate. Forced on
# the drf-spectacular settings singleton by the harness so a single-module
# instance derives the same operationIds (see _codegen._configure). Uniform
# across all pair-backends (contract-pipeline.md §2).
CODEGEN_SCHEMA_PATH_PREFIX = "/"
