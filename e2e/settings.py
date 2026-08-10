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

STAPEL_SERVICES = [{"name": "stapel-docs E2E", "prefix": ""}]
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
