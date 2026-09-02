"""E2E host settings — a real minimal host mounting auth + workspaces + docs.

Not shipped in the wheel (setuptools packages list is explicit). Used by
``e2e/run_e2e.py`` to prove the document lifecycle over real HTTP with the
real storage seam — SQLite + filesystem storage, in-process comm, outbox on.
"""
import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = "e2e-only-not-a-secret"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "stapel_core.django.apps.CommonDjangoConfig",
    "stapel_core.django.users",
    "stapel_core.django.outbox",
    "stapel_auth",
    # stapel-workspaces 0.24+ journals its audit into core.s event store;
    # its migrations depend on the app (label stapel_eventstore) being mounted.
    "stapel_core.django.eventstore",
    "stapel_workspaces",
    "stapel_docs",
]

AUTH_USER_MODEL = "users.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "e2e.urls"

from stapel_core.django.settings import get_common_templates  # noqa: E402

TEMPLATES = get_common_templates(BASE_DIR)

_STATE_DIR = Path(os.environ.get("STAPEL_DOCS_E2E_DIR", tempfile.gettempdir() + "/stapel-docs-e2e"))
_STATE_DIR.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _STATE_DIR / "db.sqlite3",
    }
}

# The REAL storage seam target: filesystem default_storage.
MEDIA_ROOT = str(_STATE_DIR / "media")
MEDIA_URL = "/media/"

STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
    ],
    "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}
SPECTACULAR_SETTINGS = {"TITLE": "stapel-docs E2E", "VERSION": "0.1.0"}

STAPEL_COMM = {
    "OUTBOX_ENABLED": True,
    "ACTION_TRANSPORT": "inprocess",
}

STAPEL_AUTH = {
    "AUTH_PASSWORD_LOGIN": True,
    "AUTH_EMAIL_LOGIN": False,
    "AUTH_OAUTH_LOGIN": False,
    "AUTH_EMAIL_REGISTRATION": False,
    "AUTH_OAUTH_REGISTRATION": False,
}

STAPEL_DOCS = {
    # This host serves MEDIA_URL off the local filesystem and has no signing
    # backend, so it makes the explicit choice the library refuses to make
    # for a deployment: download URLs here are permanent served links. A
    # real deployment either points STORAGE at a signing backend or accepts
    # this the same deliberate way.
    "ALLOW_UNEXPIRING_DOWNLOAD_URLS": True,
}

STAPEL_SERVICES = [{"name": "stapel-docs E2E", "prefix": ""}]
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# The fleet promoted two HOST-QUALITY checks to errors after this scratch
# host was written: strict template variables (core) and the profiles
# display-name provider (workspaces). Neither gates anything the e2e flow
# proves about stapel-docs, and a scratch host that installs half the
# fleet to satisfy them stops being minimal — silenced by name, on purpose.
SILENCED_SYSTEM_CHECKS = [
    "stapel_core.templates.W001",
    "stapel_workspaces.W001",
]

# This scratch host sells nothing: no billing service is mounted, and the
# workspaces plan ceiling would otherwise fail closed on every create.
STAPEL_WORKSPACES = {"ALLOW_UNBILLED": True}

# stapel-auth gates the bare POST /token/ login behind a flag now (the
# fleet's cookie/session flow is the default door). The scratch runner
# authenticates by that one endpoint, so the flag goes on here.
STAPEL_AUTH = {"AUTH_LEGACY_TOKEN_LOGIN": True, "AUTH_PASSWORD_LOGIN": True}

# No broker on the scratch host: whatever a login fires as a shared_task
# runs inline (the runner asserts outcomes, not queue mechanics). The
# default Celery app is configured HERE, at settings import, because the
# fleet's shared_task decorators bind to the current default app — the
# stapel-auth test conftest's own bootstrap, verbatim.
from celery import Celery as _Celery  # noqa: E402

_celery = _Celery("stapel_docs_e2e")
_celery.config_from_object({
    "task_always_eager": True,
    "task_eager_propagates": True,
    "broker_url": "memory://",
    "result_backend": "cache+memory://",
})
_celery.set_default()
