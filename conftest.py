def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret-key-not-for-production",
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.sessions",
                "django.contrib.admin",
                "django.contrib.messages",
                "stapel_core.django.users",
                "rest_framework",
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
            ROOT_URLCONF="stapel_docs.tests.urls",
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                }
            },
            # Synchronous in-process comm with schema validation ON, so the
            # committed contracts in schemas/ are enforced by the tests.
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
        import django
        django.setup()

        from stapel_core.comm.schemas import autoload_schemas
        autoload_schemas()


import pytest  # noqa: E402

# ── Workspace capability provider (test double for stapel-workspaces) ──
#
# authz.authorize() asks the `workspaces.check_capability` comm Function
# (fail-closed). Tests register this in-process provider backed by an
# explicit grant table: nothing is allowed until a test grants it, which
# keeps deny-by-default honestly exercised.

_CAPABILITY_GRANTS: dict[tuple[str, str], set] = {}


def _check_capability(payload):
    granted = _CAPABILITY_GRANTS.get((payload["workspace_id"], payload["user_id"]))
    if not granted:
        return {"allowed": False, "role": None}
    cap = payload["capability"]
    allowed = (
        "*" in granted
        or cap in granted
        or any(g.endswith(".*") and cap.startswith(g[:-1]) for g in granted)
    )
    return {"allowed": allowed, "role": "owner" if allowed else None}


def pytest_sessionstart(session):
    from stapel_core.comm import register_function

    register_function("workspaces.check_capability", _check_capability)


@pytest.fixture
def grant_capabilities():
    """``grant(workspace_id, user_id, *caps)`` — no caps means all ("*")."""

    def _grant(workspace_id, user_id, *caps):
        _CAPABILITY_GRANTS[(str(workspace_id), str(user_id))] = set(caps) or {"*"}

    yield _grant
    _CAPABILITY_GRANTS.clear()


@pytest.fixture(autouse=True)
def _isolate_capability_cache():
    """Capability verdicts are cached 30 s in the Django cache — flush per
    test so one test's allow/deny never leaks into the next."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()
