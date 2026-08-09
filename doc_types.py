"""Document type registry — the per-type editor/codec seam (DECIDED).

A document is ONE entity with a ``type`` slug from this open merge
registry (design §3.1). The registry carries everything the library needs
to stay body-blind (storage-verdict §6): the storage substrate never
parses a body — only a type's own ``text_extractor`` may.

Versioning discipline is a property of the TYPE, fixed here (storage
verdict §7): ``collab="crdt"`` types accumulate an update journal between
snapshots; ``collab="snapshot"`` types save whole states under optimistic
lock. Exactly two disciplines exist; a third must pass the I1–I4 contract
before it may be born.

Resolution order (library-standard §3.3 merge registry):
builtins -> ``STAPEL_DOCS["DOC_TYPES"]`` overlay ({slug: dotted-path |
None to remove}) -> runtime :func:`register_doc_type` calls.

A type whose spec vanishes from the registry degrades to ``file``
behavior — read-only, never unreadable (verdict §7.3): revisions still
list, snapshots still download, trash/purge/export still work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

COLLAB_CRDT = "crdt"
COLLAB_SNAPSHOT = "snapshot"
COLLAB_CHOICES = (COLLAB_CRDT, COLLAB_SNAPSHOT)


class DocTypeNotRegistered(Exception):
    """No spec registered for the requested type slug."""


@dataclass(frozen=True)
class DocTypeSpec:
    """Everything the library knows about a document type.

    ``editor_hint`` is the frontend dispatch key ("" = download-only);
    ``collab`` selects the legal write path (see module docstring);
    ``diffable`` tells a UI whether line-diff rendering is meaningful;
    ``text_extractor`` (bytes -> str) feeds knowledge-chunk extraction and
    may be None (nothing to index, e.g. opaque files).
    """

    slug: str
    label: str
    collab: str = COLLAB_SNAPSHOT
    diffable: bool = False
    editor_hint: str = ""
    mime_type: str = "application/octet-stream"
    extension: str = ""
    empty_body: bytes = b""
    text_extractor: Optional[Callable[[bytes], str]] = field(default=None)

    def __post_init__(self):
        if self.collab not in COLLAB_CHOICES:
            raise ValueError(
                f"DocTypeSpec {self.slug!r}: collab must be one of {COLLAB_CHOICES}"
            )


def _utf8_text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


#: v1 builtins, per the owner's scope: the simplest editable types plus
#: opaque files. sheet/slides/office are later registry entries, not
#: schema changes. ``file`` bodies are byte-preserved originals — never
#: rewritten, repacked or normalized (verdict §9.4).
BUILTIN_DOC_TYPES = {
    "txt": DocTypeSpec(
        slug="txt", label="Plain text", collab=COLLAB_SNAPSHOT, diffable=True,
        editor_hint="text", mime_type="text/plain", extension=".txt",
        text_extractor=_utf8_text,
    ),
    "md": DocTypeSpec(
        slug="md", label="Markdown", collab=COLLAB_SNAPSHOT, diffable=True,
        editor_hint="markdown", mime_type="text/markdown", extension=".md",
        text_extractor=_utf8_text,
    ),
    "csv": DocTypeSpec(
        slug="csv", label="CSV", collab=COLLAB_SNAPSHOT, diffable=True,
        editor_hint="csv", mime_type="text/csv", extension=".csv",
        text_extractor=_utf8_text,
    ),
    "file": DocTypeSpec(
        slug="file", label="File", collab=COLLAB_SNAPSHOT, diffable=False,
        editor_hint="", mime_type="application/octet-stream", extension="",
        text_extractor=None,
    ),
}

#: Runtime registrations (``register_doc_type``) — applied last.
_runtime_doc_types: dict[str, DocTypeSpec] = {}


def register_doc_type(spec: DocTypeSpec) -> None:
    """Register (or replace) a document type at runtime.

    The settings-overlay path (``STAPEL_DOCS["DOC_TYPES"]``) is the
    canonical host seam; this is the programmatic equivalent for apps that
    register from ``AppConfig.ready()``.
    """
    if not isinstance(spec, DocTypeSpec):
        raise TypeError(f"expected DocTypeSpec, got {type(spec)!r}")
    _runtime_doc_types[spec.slug] = spec


def unregister_doc_type(slug: str) -> None:
    """Remove a runtime registration (tests)."""
    _runtime_doc_types.pop(slug, None)


def _resolve_overlay_entry(slug: str, dotted: str) -> DocTypeSpec:
    from django.utils.module_loading import import_string

    value = import_string(dotted)
    if callable(value) and not isinstance(value, DocTypeSpec):
        value = value()
    if not isinstance(value, DocTypeSpec):
        raise TypeError(
            f"STAPEL_DOCS['DOC_TYPES'][{slug!r}] -> {dotted!r} is not a DocTypeSpec"
        )
    return value


def get_doc_types() -> dict[str, DocTypeSpec]:
    """The effective registry: builtins <- settings overlay <- runtime."""
    from .conf import docs_settings

    registry = dict(BUILTIN_DOC_TYPES)
    overlay = docs_settings.DOC_TYPES or {}
    for slug, dotted in overlay.items():
        if dotted is None:
            registry.pop(slug, None)
        else:
            registry[slug] = _resolve_overlay_entry(slug, dotted)
    registry.update(_runtime_doc_types)
    return registry


def get_doc_type(slug: str) -> DocTypeSpec:
    """Spec for *slug*, or :class:`DocTypeNotRegistered`."""
    try:
        return get_doc_types()[slug]
    except KeyError:
        raise DocTypeNotRegistered(slug) from None


__all__ = [
    "COLLAB_CRDT",
    "COLLAB_SNAPSHOT",
    "COLLAB_CHOICES",
    "DocTypeSpec",
    "DocTypeNotRegistered",
    "BUILTIN_DOC_TYPES",
    "register_doc_type",
    "unregister_doc_type",
    "get_doc_types",
    "get_doc_type",
]
