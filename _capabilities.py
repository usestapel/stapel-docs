"""stapel-docs capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli

#: The nested sharing knob projected as its own axis (see ``axis_config``).
LINK_MAX_LEVEL_AXIS = "SHARING.LINK.MAX_LEVEL"


def axis_config():
    """``(defaults, is_axis, axis_group)`` the emitter and its tests share.

    SHARING is the one CTO-facing axis: does the product grant access beyond
    the immutable workspace baseline (MODES / LINK.* — closed by default). The
    emitter expresses axes as top-level DEFAULTS keys, so the nested knobs
    surface as ONE composite SHARING axis. The one nested value a client must
    branch on — the ceiling of a minted link, which decides which levels a
    share dialog may offer — is projected as its own axis,
    ``SHARING.LINK.MAX_LEVEL``, read from the same DEFAULTS literal (the
    composite stays). STORAGE/DOC_TYPES/EXPORTERS/INGEST/SHARING RESOLVERS
    are extension seams (curated in docs/capabilities.meta.json); timeouts,
    URL lifetimes, REPLAY_WINDOW, AUTO_REVISION_INTERVAL, FOLDER_MAX_DEPTH,
    TRASH_RETENTION_DAYS and the S3_* block are tuning — neither axes nor
    extension points.
    """
    from stapel_docs.conf import DEFAULTS

    defaults = {
        **DEFAULTS,
        LINK_MAX_LEVEL_AXIS: DEFAULTS["SHARING"]["LINK"]["MAX_LEVEL"],
    }
    keys = ("SHARING", LINK_MAX_LEVEL_AXIS)
    return (
        defaults,
        lambda k: k in keys,
        axis_group_rules(exact={k: "docs.sharing" for k in keys}),
    )


def main(argv=None):
    from stapel_docs._codegen import _configure

    _configure()
    from stapel_docs.urls_v1 import GATE_REGISTRY

    defaults, is_axis, axis_group = axis_config()
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/docs/api/v1",
        defaults=defaults,
        registry=GATE_REGISTRY,
        is_axis=is_axis,
        axis_group=axis_group,
        prog="stapel-docs-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
