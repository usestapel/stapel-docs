# stapel-docs — contract emission + drift gate (contract-pipeline.md §2-3).
#
# This module emits its OWN contract triad (schema.json + flows.json + errors.json)
# per-module from a single-module {docs + core} Django instance mounted at the
# canonical /docs/api/v1 prefix (see _codegen.py / _codegen_settings.py /
# codegen_urls.py).
#
# Like stapel-recordings, stapel-docs is NOT mounted in stapel-example-monolith,
# so there is no monolith aggregate slice to diff this artifact against for
# byte-identity — validation is standalone (determinism + closure + canonical
# prefix + security presence; see tests/test_contract.py).
#
# PYTHON must have the module + its deps importable (the workspace venv, or a CI
# venv). The authoritative CI gate is tests/test_contract.py (run under pytest);
# these targets are the dev-loop convenience.
PYTHON ?= python3

.PHONY: contract contract-check

# Emit the contract triad + capabilities.json + llms.txt (the fifth contract
# artifact, stapel_tools.llms_txt) into docs/.
#
# The llms.txt budget is raised from the generator's default 4000 to 9000,
# same exception stapel-auth (8000), stapel-recordings (5000) and
# stapel-workspaces (4500) already take. This module carries the fleet's
# largest usage surface (67 entries across services, storage, doc_types,
# exporters, thumbnails and authz — the whole point of a library whose seams
# exist to be called) plus a full-CRUD 35-operation HTTP surface, which
# together measure ~8.7k tokens. Raise the ceiling, do NOT shorten `intent` lines in
# docs/capabilities.meta.json to fit — a trimmed context file is
# indistinguishable from a complete one at the point of use, which is the
# failure mode the budget gate exists to prevent. contract-check below and
# tests/test_contract.py enforce the same 9000 ceiling. The ceiling moved
# 5500 -> 6000 when the resource-invariant surface landed (limits, quota and
# upload-session gates plus their error keys), and 6000 -> 7000 when the
# drive wave landed (starred/recents/search/usage/thumbnails: 13 more called
# symbols, 8 more operations, 3 more error keys): the honest move per the
# generator's own advice is to raise the ceiling deliberately, because
# shortening the intent lines of security gates is how a gate becomes
# something nobody can explain and therefore nobody adopts. 7000 -> 9000 when
# the sharing mechanism landed (0.6.0): 22 more called symbols across authz and
# services, 7 more operations and 7 more error keys, and this is the surface
# where an unexplained gate is most expensive — every line of it describes who
# may reach somebody else's document.
#
# README.md is the SIXTH artifact (tracker #257): assembled by
# stapel_tools.readme from docs/readme.md (the human half — what this module
# is, how to think about it) plus everything emitted above. Badges, version,
# surface counts and doc links are generated, so a release cannot leave them
# behind. Edit docs/readme.md; never README.md.
contract:
	$(PYTHON) -m stapel_docs._codegen --out docs
	$(PYTHON) -m stapel_docs._capabilities --out docs
	$(PYTHON) -m stapel_tools.llms_txt . --out docs --budget 9000
	$(PYTHON) -m stapel_tools.readme .

# Drift gate: regenerate into a temp dir and diff against the committed docs/*.json
# (mirrors the monolith's `make codegen-check` and the frontend's `gen:*:check`).
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_docs._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_docs._capabilities --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_tools.llms_txt . --out "$$tmp" --budget 9000 || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json capabilities.json llms.txt; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.readme . --check || rc=1; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors,capabilities,llms.txt} + README.md up to date"; fi; \
	exit $$rc


.PHONY: migration-lint

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict
