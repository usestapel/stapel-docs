"""Django system checks for stapel-docs configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with; W-level for entries that degrade lazily.

The sharing-axis guards implement the "configured but not implemented"
canon (sharing-axis-design §11): every deferred capability has a live
config key with a closed default, and opening it before the mechanism
exists is a LOUD error here — never a silent no-op.
"""
from django.core import checks

#: Modes the axis will ever accept (sharing-axis §2.1). v1 implements none.
KNOWN_SHARING_MODES = ("whitelist", "link")
IMPLEMENTED_SHARING_MODES = ()


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
    """E010-E013: closed-by-default sharing axis guards (v1)."""
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
                "version — the axis ships closed (MODES=[]); remove it or upgrade",
                id="stapel_docs.E011",
            ))

    link = {**DEFAULT_SHARING["LINK"], **(sharing.get("LINK") or {})}
    if link.get("ANONYMOUS"):
        errors.append(checks.Error(
            "STAPEL_DOCS['SHARING']['LINK']['ANONYMOUS']=True is configured but "
            "not implemented in this version (sharing-axis §11.1)",
            id="stapel_docs.E012",
        ))
    if link.get("MAX_LEVEL", "view") != "view":
        errors.append(checks.Error(
            "STAPEL_DOCS['SHARING']['LINK']['MAX_LEVEL'] above 'view' is "
            "configured but not implemented in this version (sharing-axis §11.2)",
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
