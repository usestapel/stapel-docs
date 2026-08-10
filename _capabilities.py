"""stapel-docs capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli


def main(argv=None):
    from stapel_docs._codegen import _configure

    _configure()
    from stapel_docs.conf import DEFAULTS
    from stapel_docs.urls_v1 import GATE_REGISTRY

    # SHARING is the one CTO-facing axis: does the product grant access
    # beyond the immutable workspace baseline (MODES / LINK.* — closed by
    # default, v1 system checks E010-E013 refuse opening it before the
    # mechanism exists). The emitter expresses axes as top-level DEFAULTS
    # keys only, so the nested MODES/LINK.* knobs surface as ONE composite
    # SHARING axis (derived kind "enum", default = the closed dict) rather
    # than per-key axes — honest within the mechanism, detailed in the
    # curated summary. STORAGE/DOC_TYPES/EXPORTERS/INGEST/SHARING RESOLVERS
    # are extension seams (curated in docs/capabilities.meta.json);
    # timeouts, URL lifetimes, REPLAY_WINDOW, AUTO_REVISION_INTERVAL,
    # FOLDER_MAX_DEPTH, TRASH_RETENTION_DAYS and the S3_* block are tuning
    # — neither axes nor extension points.
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/docs/api/v1",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda k: k == "SHARING",
        axis_group=axis_group_rules(exact={"SHARING": "docs.sharing"}),
        prog="stapel-docs-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
