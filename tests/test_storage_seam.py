"""Seam lint (storage-verdict §9.2): the storage ABC is the SINGLE
read/write path for content bytes. No default_storage, boto3 or direct
filesystem-storage access outside storage.py — otherwise the deferred
DatabaseBackend profile stops being a config value and becomes a rewrite.
Same genre as realtime's "no Channels imports outside the substrate" lint.

AST-based: comments and docstrings may NAME the rule; code may not break it.
"""
import ast
import pathlib

PKG_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Files allowed to touch storage primitives directly.
ALLOWED = {"storage.py"}

FORBIDDEN_MODULES = ("boto3", "botocore", "django.core.files.storage")
FORBIDDEN_NAMES = ("default_storage",)


def _module_files():
    for path in PKG_ROOT.glob("*.py"):
        if path.name not in ALLOWED:
            yield path
    yield from (PKG_ROOT / "migrations").glob("*.py")


def _offences(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in ("boto3", "botocore") or any(
                    alias.name.startswith(m) for m in FORBIDDEN_MODULES
                ):
                    yield f"{path.name}:{node.lineno} import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] in ("boto3", "botocore") or any(
                mod.startswith(m) for m in FORBIDDEN_MODULES
            ):
                yield f"{path.name}:{node.lineno} from {mod} import ..."
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    yield f"{path.name}:{node.lineno} from {mod} import {alias.name}"
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            yield f"{path.name}:{node.lineno} {node.id}"
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            yield f"{path.name}:{node.lineno} .{node.attr}"


class TestStorageSeamClosure:
    def test_no_storage_primitives_outside_the_seam(self):
        offenders = [o for path in _module_files() for o in _offences(path)]
        assert not offenders, (
            "content I/O must go through stapel_docs.storage.get_storage(); "
            f"direct storage access found in: {offenders}"
        )
