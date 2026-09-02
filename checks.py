"""Django system checks for stapel-docs configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with; W-level for entries that degrade lazily.

The sharing-axis guards implement the "configured but not implemented"
canon (sharing-axis-design §11): every deferred capability has a live
config key with a closed default, and opening it before the mechanism
exists is a LOUD error here — never a silent no-op. 0.6.0 implements both
grant sources, so E011 no longer fires for them; E012/E013 keep firing,
because the owner's §10 verdict (no anonymous links, links view-only)
governs what a DEPLOYMENT may switch on, not what the rule can express.
"""
from django.core import checks

from .authz import IMPLEMENTED_SHARING_MODES, KNOWN_SHARING_MODES  # noqa: F401

#: Re-exported from ``authz`` so the rule and the check read ONE list.
#: ``KNOWN_SHARING_MODES`` is what the axis will ever accept (§2.1);
#: ``IMPLEMENTED_SHARING_MODES`` is what this version can actually enforce.
#: They are equal since 0.6.0 (both grant sources ship) — E011 exists for
#: the next mode somebody configures before it is built.


@checks.register(checks.Tags.compatibility)
def check_storage_backend(app_configs, **kwargs):
    """E001: the STORAGE dotted path must import and subclass DocsStorage."""
    from .conf import docs_settings
    from .storage import DocsStorage

    try:
        cls = docs_settings.STORAGE
    except Exception as exc:
        return [checks.Error(
            f"STAPEL_DOCS['STORAGE'] cannot be imported: {exc}",
            id="stapel_docs.E001",
        )]
    if not (isinstance(cls, type) and issubclass(cls, DocsStorage)):
        return [checks.Error(
            f"STAPEL_DOCS['STORAGE'] ({cls!r}) is not a DocsStorage subclass",
            id="stapel_docs.E001",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_doc_types(app_configs, **kwargs):
    """E002: every DOC_TYPES overlay entry must resolve to a valid spec."""
    from .conf import docs_settings
    from .doc_types import _resolve_overlay_entry

    errors = []
    overlay = docs_settings.DOC_TYPES or {}
    for slug, dotted in overlay.items():
        if dotted is None:
            continue
        try:
            _resolve_overlay_entry(slug, dotted)
        except Exception as exc:
            errors.append(checks.Error(
                f"STAPEL_DOCS['DOC_TYPES'][{slug!r}] is configured but broken: {exc}",
                id="stapel_docs.E002",
            ))
    return errors


@checks.register(checks.Tags.compatibility)
def check_sharing_axis(app_configs, **kwargs):
    """E010-E014: sharing-axis configuration guards.

    E010 unknown mode, E011 known-but-unimplemented mode, E012 anonymous
    link redemption, E013 a link ceiling above ``view``, E014 a broken
    resolver path. The axis still ships closed (``MODES: []``) — these
    guard what opening it would mean, and a broken configuration fails the
    deploy rather than degrading into "open".
    """
    from .conf import DEFAULT_SHARING, docs_settings

    errors = []
    sharing = {**DEFAULT_SHARING, **(docs_settings.SHARING or {})}
    modes = sharing.get("MODES") or []
    for mode in modes:
        if mode not in KNOWN_SHARING_MODES:
            errors.append(checks.Error(
                f"STAPEL_DOCS['SHARING']['MODES'] contains unknown mode {mode!r} "
                f"(known: {KNOWN_SHARING_MODES}); unknown modes never enable anything",
                id="stapel_docs.E010",
            ))
        elif mode not in IMPLEMENTED_SHARING_MODES:
            errors.append(checks.Error(
                f"Sharing mode {mode!r} is configured but not implemented in this "
                "version; remove it or upgrade — a mode nothing enforces must "
                "not read as an enabled one",
                id="stapel_docs.E011",
            ))

    link = {**DEFAULT_SHARING["LINK"], **(sharing.get("LINK") or {})}
    if link.get("ANONYMOUS"):
        errors.append(checks.Error(
            "STAPEL_DOCS['SHARING']['LINK']['ANONYMOUS']=True is not sanctioned "
            "in this version (sharing-axis §10 verdict 1, §11.1). The rule "
            "carries the branch and it is covered by tests, but no deployment "
            "opens the fleet's leakiest door on a config key alone: shipping it "
            "needs an owner decision, not an override.",
            id="stapel_docs.E012",
        ))
    if link.get("MAX_LEVEL", "view") != "view":
        errors.append(checks.Error(
            "STAPEL_DOCS['SHARING']['LINK']['MAX_LEVEL'] above 'view' is not "
            "sanctioned in this version (sharing-axis §10 verdict 2, §11.2). "
            "The ceiling is enforced generically and edit-level grants work "
            "through the whitelist; edit-BY-LINK needs an owner decision.",
            id="stapel_docs.E013",
        ))

    from django.utils.module_loading import import_string

    for ref_kind, dotted in (sharing.get("RESOLVERS") or {}).items():
        try:
            import_string(dotted)
        except Exception as exc:
            errors.append(checks.Error(
                f"STAPEL_DOCS['SHARING']['RESOLVERS'][{ref_kind!r}] is configured "
                f"but broken: {exc} (fail-closed: broken resolvers deny, but a "
                "broken CONFIGURATION must fail deploys loudly)",
                id="stapel_docs.E014",
            ))
    return errors


@checks.register(checks.Tags.compatibility)
def check_retention_is_scheduled(app_configs, **kwargs):
    """W030: trash retention is configured but nothing runs it.

    The purge is destructive and irreversible, so this is a warning, not an
    error — but a retention policy no scheduler invokes keeps soft-deleted
    documents forever while the config claims otherwise (audit DOCS-02).

    Only hosts that drive a beat schedule are checked: a host with no
    ``CELERY_BEAT_SCHEDULE`` runs the ``docs_purge_expired`` command from
    its own cron, which this check cannot see and must not second-guess.
    """
    from django.conf import settings

    from .tasks import PURGE_TASK_NAME

    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", None)
    if schedule is None:
        return []
    scheduled = any(
        (entry or {}).get("task") == PURGE_TASK_NAME for entry in schedule.values()
    )
    if scheduled:
        return []
    return [checks.Warning(
        "Docs trash retention (TRASH_RETENTION_DAYS) is not scheduled: no "
        f"CELERY_BEAT_SCHEDULE entry runs {PURGE_TASK_NAME}. Add "
        "stapel_docs.tasks.get_docs_beat_schedule() to the beat schedule, or "
        "run the docs_purge_expired command from your own scheduler.",
        id="stapel_docs.W030",
    )]


@checks.register(checks.Tags.compatibility)
def check_download_url_expiry(app_configs, **kwargs):
    """W031/W032: can the download path honour DOWNLOAD_URL_EXPIRES_SECONDS?

    A download URL is a bearer capability. When the configured backend
    cannot sign one, the only thing it can return is a permanent public
    link — readable by anyone who ever saw it, long after the membership
    that minted it ended, without passing authorize() again. The service
    refuses that by default (503 on the download endpoints; the authorized
    content endpoint still serves the bytes), and this check tells the
    operator at deploy time rather than at the first 503 — or, when the
    host has opted in, states plainly what it opted into.
    """
    from .conf import docs_settings

    try:
        backend = docs_settings.STORAGE
    except Exception:  # E001 already reports an unimportable backend
        return []
    expiring = bool(getattr(backend, "mints_expiring_urls", False))
    opted_in = bool(docs_settings.ALLOW_UNEXPIRING_DOWNLOAD_URLS)
    if expiring:
        if opted_in:
            return [checks.Warning(
                f"STAPEL_DOCS['ALLOW_UNEXPIRING_DOWNLOAD_URLS'] is on while "
                f"{backend.__name__} signs expiring URLs — the opt-out is not "
                "needed here and only widens what a leaked URL is worth.",
                id="stapel_docs.W032",
            )]
        return []
    if opted_in:
        return [checks.Warning(
            f"{backend.__name__} cannot honour "
            "STAPEL_DOCS['DOWNLOAD_URL_EXPIRES_SECONDS'] and "
            "ALLOW_UNEXPIRING_DOWNLOAD_URLS is on: every download URL this "
            "deployment mints is a permanent public link that outlives the "
            "membership it was issued for.",
            id="stapel_docs.W032",
        )]
    return [checks.Warning(
        f"{backend.__name__} cannot honour "
        "STAPEL_DOCS['DOWNLOAD_URL_EXPIRES_SECONDS'], so the document and "
        "revision download-URL endpoints refuse with 503 "
        "(error.503.docs_download_url_unavailable). Configure a signing "
        "backend (stapel_docs.storage.S3Backend) for expiring links, or set "
        "ALLOW_UNEXPIRING_DOWNLOAD_URLS=True to accept permanent public "
        "media URLs. The authorized content endpoint is unaffected.",
        id="stapel_docs.W031",
    )]


@checks.register(checks.Tags.compatibility)
def check_exporters(app_configs, **kwargs):
    """E020: every EXPORTERS overlay entry must import."""
    from django.utils.module_loading import import_string

    from .conf import docs_settings

    errors = []
    for fmt, dotted in (docs_settings.EXPORTERS or {}).items():
        if dotted is None:
            continue
        try:
            import_string(dotted)
        except Exception as exc:
            errors.append(checks.Error(
                f"STAPEL_DOCS['EXPORTERS'][{fmt!r}] is configured but broken: {exc}",
                id="stapel_docs.E020",
            ))
    return errors
