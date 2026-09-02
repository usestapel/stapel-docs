"""Per-module contract quintet + drift gate (contract-pipeline.md §2-3).

stapel-docs emits its **own** contract artifacts — ``docs/schema.json``
(drf-spectacular OpenAPI), ``docs/flows.json`` (generate_flow_docs machine
artifact — empty here, this module has no ``@flow_step`` annotations),
``docs/errors.json`` (generate_error_keys registry), ``docs/capabilities.json``
and ``docs/llms.txt`` — from a single-module ``{docs + core}`` Django instance
mounted at the canonical ``/docs/api/v1/`` prefix. The frontend codegen
consumes these committed artifacts.

Like recordings, **stapel-docs is not mounted in stapel-example-monolith**
(grep-confirmed: no route for it in ``svc-app/core/urls.py`` as of this
writing — docs is a standalone pair-backend pending its own frontend pair).
There is therefore no monolith aggregate slice to assert byte-identity
against. Validation here is **standalone** instead:

  - determinism (two independent emissions are byte-identical — the drift
    gate below is only meaningful if this holds);
  - the schema's ``$ref`` closure is self-contained (every path/component
    reference resolves within this one file);
  - the protected endpoints carry the ``JWTCookieAuth`` security requirement
    (the profiles-finding gap: without an explicit
    ``_register_jwt_auth_extension()`` call, a module with no co-mounted
    sibling silently drops ``security`` from every operation);
  - paths carry the canonical ``/docs/api/v1/`` prefix.

Regenerate after any change to a serializer / view / url / error key / conf:

    make contract        # or: python -m stapel_docs._codegen --out docs

then commit ``docs/*`` + ``README.md``. Without regenerating, the drift gate
below fails — the same byte-stable regenerate-and-diff discipline as every
other pair-backend's contract.

The harness runs in a **subprocess**: this test process already configured
Django (via conftest, on the bare test urlconf), and the harness needs its own
canonical-prefix urlconf + drf-spectacular singleton — a clean interpreter is
the honest way to exercise exactly what ``make contract`` runs.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_PY = sys.version_info[:2]
if _PY != (3, 12):
    _GOT = f"{_PY[0]}.{_PY[1]}"
    _PY312_MSG = (
        "stapel-docs contract tests require Python 3.12 (the CI/monolith "
        f"pin) — running {_GOT}. drf-spectacular renders component "
        "descriptions (Optional[X] vs X | None) differently across Python "
        "minor versions, so drift/identity checks emitted+compared under "
        "any other minor produce false diffs."
    )
    pytest.skip(
        _PY312_MSG + " Skipping on any non-3.12 interpreter (CI or local) — "
        "the contract canon is only defined on Python 3.12.",
        allow_module_level=True,
    )

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
TRIAD = ("schema.json", "flows.json", "errors.json")
CANONICAL_PREFIX = "/docs/api/v1/"
# The fourth artifact (capability-config.md §2): config axes over STAPEL_DOCS,
# emitted from conf.py DEFAULTS + the urls_v1.py gate registry + schema.json +
# the curated docs/capabilities.meta.json. Same emit/drift discipline.
# The fifth artifact (badge-canon §3): docs/llms.txt, rendered from
# docs/capabilities.json (+schema/errors/flows) by stapel_tools.llms_txt.
#
# The usage surface (stapel_tools.surface: services / storage / doc_types /
# exporters / thumbnails / authz — 67 entries, each one a symbol a product
# would otherwise reimplement) plus the full-CRUD 35-operation HTTP surface do
# not fit the generator's default 4000-token budget (~8.7k measured). Same exception
# stapel-auth (8000), stapel-recordings (5000) and stapel-workspaces (4500)
# already take: raise the ceiling for this module, do not shorten intents to
# fit. Must match the Makefile — if they drift, the gate starts measuring the
# wrong number.
ARTIFACTS = TRIAD + ("capabilities.json", "llms.txt")
LLMS_TXT_BUDGET = "9500"


def _emit(out_dir: Path) -> None:
    for module in ("stapel_docs._codegen", "stapel_docs._capabilities"):
        subprocess.run(
            [sys.executable, "-m", module, "--out", str(out_dir)],
            cwd=str(REPO),
            check=True,
            capture_output=True,
        )
    # llms.txt is rendered from the REAL committed docs/capabilities.json (not
    # the just-regenerated tmp one) — same as `make contract-check` — so this
    # step also catches a stale llms.txt independently of the loop above.
    subprocess.run(
        [
            sys.executable, "-m", "stapel_tools.llms_txt", ".",
            "--out", str(out_dir), "--budget", LLMS_TXT_BUDGET,
        ],
        cwd=str(REPO),
        check=True,
        capture_output=True,
    )


def test_contract_artifacts_committed():
    for name in ARTIFACTS:
        assert (DOCS / name).is_file(), f"missing docs/{name} — run `make contract`"
    assert (DOCS / "capabilities.meta.json").is_file(), (
        "missing docs/capabilities.meta.json — the curated layer is "
        "hand-written and committed, not generated"
    )


def test_contract_has_no_drift(tmp_path):
    """Regenerate into a temp dir; committed artifacts must match byte-for-byte."""
    _emit(tmp_path)
    for name in ARTIFACTS:
        committed = (DOCS / name).read_bytes()
        regenerated = (tmp_path / name).read_bytes()
        assert committed == regenerated, (
            f"docs/{name} drifted — run `make contract` and commit docs/{name}"
        )


def test_emission_is_deterministic(tmp_path):
    """Two independent emissions are byte-identical (drift gate is meaningful)."""
    a, b = tmp_path / "a", tmp_path / "b"
    _emit(a)
    _emit(b)
    for name in ARTIFACTS:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_paths_carry_canonical_prefix():
    """The mount-prefix fix: schema paths + flow endpoints are /docs/api/v1/*, not bare."""
    schema = json.loads((DOCS / "schema.json").read_text())
    assert schema["paths"], "schema has no paths"
    assert all(p.startswith(CANONICAL_PREFIX) for p in schema["paths"]), (
        "schema paths are not mounted at the canonical /docs/api/v1/ prefix"
    )
    flows = json.loads((DOCS / "flows.json").read_text())
    for flow in flows:
        for step in flow.get("steps", []):
            for ep in step.get("endpoints", []):
                assert ep["path"].startswith(CANONICAL_PREFIX), (
                    f"flow endpoint {ep['path']} is not canonically prefixed"
                )


def test_flows_is_empty_flowless_module():
    """docs has no @flow_step annotations — [] is the valid, expected artifact."""
    flows = json.loads((DOCS / "flows.json").read_text())
    assert flows == [], (
        "flows.json is non-empty — docs gained @flow_step annotations; "
        "update this test's assumption (it is no longer a flowless module)"
    )


def _refs(obj) -> set[str]:
    return set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(obj)))


def test_schema_ref_closure_is_self_contained():
    """Every $ref reachable from a path resolves inside this one schema.json.

    Standalone analog of the byte-identity-vs-monolith check auth/profiles run
    (contract-pipeline.md §9 Q2): with no monolith slice to diff against here,
    the guarantee that matters is that the ``{module + core}`` harness emitted
    a *closed* component table — no path or component references a schema
    this module never defined.
    """
    schema = json.loads((DOCS / "schema.json").read_text())
    comps = schema.get("components", {}).get("schemas", {})

    seeds: set[str] = set()
    for path_obj in schema["paths"].values():
        seeds |= _refs(path_obj)
    assert seeds, "no component is referenced from any path — unexpected for a DRF API"

    seen: set[str] = set()
    stack = list(seeds)
    dangling: set[str] = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in comps:
            dangling.add(name)
            continue
        stack.extend(_refs(comps[name]))

    assert not dangling, f"dangling $ref(s), not defined in this module's own schema: {dangling}"


#: Operations whose credential rides IN the URL, presigned-style, so they
#: carry no JWT security requirement by design — the ONLY entries allowed
#: here are endpoints whose view sets ``authentication_classes = []``
#: because a header credential is structurally impossible for the caller
#: (the upload intake PUT: the drive queue PUTs raw bytes at put_url with
#: no Authorization header, exactly as it would at an S3 presigned URL).
SIGNATURE_AUTHED_OPERATIONS = {
    ("put", "/docs/api/v1/uploads/{upload_id}/content"),
}


def test_protected_endpoints_carry_jwt_security():
    """The profiles-finding gap: a module with no co-mounted sibling loses
    `security: [{"JWTCookieAuth": []}]` unless _codegen.py explicitly calls
    stapel_core's `_register_jwt_auth_extension()` before emission. Every
    docs view is `permission_classes = [IsNotAnonymousUser]`, so every
    operation here is expected to carry the JWT cookie security requirement
    — except the enumerated signature-authed operations, whose URL is the
    credential (their auth story is tested in tests/test_uploads.py).
    """
    schema = json.loads((DOCS / "schema.json").read_text())
    security_schemes = schema.get("components", {}).get("securitySchemes", {})
    assert "JWTCookieAuth" in security_schemes, (
        "JWTCookieAuth security scheme missing — _register_jwt_auth_extension() "
        "regression (see _codegen.py._configure)"
    )
    for path, path_obj in schema["paths"].items():
        for method, op in path_obj.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            if (method, path) in SIGNATURE_AUTHED_OPERATIONS:
                continue
            security = op.get("security")
            assert security and any("JWTCookieAuth" in s for s in security), (
                f"{method.upper()} {path} is missing the JWTCookieAuth security "
                "requirement — protected endpoint emitted without security"
            )


# --- capabilities.json content sanity (capability-config.md §2) ---------------


def _capabilities() -> dict:
    return json.loads((DOCS / "capabilities.json").read_text())


def test_capabilities_axes_inventory():
    """One composite axis: the closed-by-default sharing axis.

    The emitter expresses axes as top-level DEFAULTS keys only, so the nested
    SHARING knobs (MODES / LINK.*) surface as ONE `SHARING` axis whose default
    is the closed dict — `kind` is the derived fallback "enum" (dict default),
    and the per-key story lives in the curated summary + checks.py E010-E013.
    """
    doc = _capabilities()
    assert {a["key"] for a in doc["axes"]} == {"SHARING"}
    axis = doc["axes"][0]
    assert axis["kind"] == "enum"
    assert axis["group"] == "docs.sharing"
    # The committed default is the CLOSED v1 axis — if this changes, the
    # sharing mechanism shipped and this module's v1 guards story is stale.
    assert axis["default"]["MODES"] == []
    assert axis["default"]["LINK"]["ANONYMOUS"] is False
    assert axis["default"]["LINK"]["MAX_LEVEL"] == "view"


def test_capabilities_sharing_axis_is_behavioral():
    """SHARING gates the authorization algebra, not endpoint mounting."""
    axis = next(a for a in _capabilities()["axes"] if a["key"] == "SHARING")
    assert axis["gates"]["operations"] == []
    assert axis["gates"]["co_gates"] == []
    assert axis["gates"]["behavior"]
    assert axis["curated"]["summary"]
    assert axis["curated"]["business_label"]


def test_capabilities_extension_points_cover_the_seams():
    """The flagship seams (MODULE.md) surface as extension points."""
    names = {e["name"] for e in _capabilities()["extension_points"]}
    assert {
        "STORAGE",
        "DOC_TYPES",
        "EXPORTERS",
        "INGEST",
        "SHARING_RESOLVERS",
        "PRESENTERS",
    } <= names


def test_capabilities_surface_covers_the_choke_points():
    """The two symbols every anti-pattern in MODULE.md points at."""
    surface = {e["name"] for e in _capabilities().get("surface", [])}
    assert {"authorize", "get_storage", "create_document", "save_content"} <= surface


def test_capabilities_operations_total_matches_schema():
    schema = json.loads((DOCS / "schema.json").read_text())
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    total = sum(
        1 for item in schema["paths"].values() for m in item if m in methods
    )
    assert _capabilities()["operations_total"] == total


def test_capabilities_envelope():
    doc = _capabilities()
    import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert doc["module"] == pyproject["project"]["name"]
    assert doc["version"] == pyproject["project"]["version"]
    assert doc["provides"]
    assert doc["extension_points"]
    assert doc["requires"]


def test_capabilities_meta_out_of_sync_fails_loudly():
    """A curated-layer gap must be an emission ERROR, never a silent skip."""
    from stapel_tools.capabilities import axis_group_rules, build_capabilities

    from stapel_docs.conf import DEFAULTS
    from stapel_docs.urls_v1 import GATE_REGISTRY

    schema = json.loads((DOCS / "schema.json").read_text())
    meta = json.loads((DOCS / "capabilities.meta.json").read_text())

    def _build(broken_meta):
        return build_capabilities(
            module="stapel-docs",
            version="0.0.0",
            defaults=DEFAULTS,
            registry=GATE_REGISTRY,
            schema=schema,
            meta=broken_meta,
            is_axis=lambda k: k == "SHARING",
            axis_group=axis_group_rules(exact={"SHARING": "docs.sharing"}),
            canonical_prefix="/docs/api/v1",
        )

    # Baseline: intact meta builds.
    assert _build(json.loads(json.dumps(meta)))["axes"]

    # Missing axis entry → loud failure.
    broken = json.loads(json.dumps(meta))
    del broken["axes"]["SHARING"]
    with pytest.raises(SystemExit, match="SHARING"):
        _build(broken)

    # Stale (unknown) axis entry → loud failure.
    broken = json.loads(json.dumps(meta))
    broken["axes"]["DOCS_NO_SUCH_AXIS"] = {"summary": "x", "business_label": "x"}
    with pytest.raises(SystemExit, match="DOCS_NO_SUCH_AXIS"):
        _build(broken)

    # Empty business_label → loud failure.
    broken = json.loads(json.dumps(meta))
    broken["axes"]["SHARING"]["business_label"] = ""
    with pytest.raises(SystemExit, match="business_label"):
        _build(broken)


# --- README.md — the sixth artifact (tracker #257) ---------------------------
#
# README.md is assembled by ``stapel_tools.readme`` from docs/readme.md (the
# human half: what this module is and how to think about it) plus the contract
# documents above (badges, version, surface counts, doc links). Everything a
# hand-written README used to restate — and therefore used to get wrong one
# release later — is generated and gated here.

def test_readme_is_assembled_and_has_no_drift():
    from stapel_tools.readme import load_inputs, render, static_languages

    inputs = load_inputs(REPO)
    languages = static_languages(REPO)
    assert languages == ["en"], "expected exactly the English static body docs/readme.md"
    committed = (REPO / "README.md").read_text()
    assert committed == render(REPO, inputs, "en", languages), (
        "README.md drifted — run `make contract` and commit README.md "
        "(edit prose in docs/readme.md, never README.md itself)"
    )


def test_readme_version_matches_the_package():
    """The #226 gate, at the point where the number is published."""
    import tomllib

    from stapel_tools.readme import load_inputs, resolve_version

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert resolve_version(load_inputs(REPO)) == pyproject["project"]["version"]
