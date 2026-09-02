"""The pycrdt codec — the ONE place this library parses a crdt body.

The substrate stays body-blind (storage-verdict §6): journal payloads and
snapshot bodies are opaque bytes everywhere else. This module is the codec
the builtin ``codec="yjs"`` types own — ``pycrdt`` is the y-crdt Rust
binding, so everything here is Yjs-compatible on the wire and a browser
running Yjs (y-codemirror.next et al.) converges with what the server
folds.

Canonical document shape: ONE ``Text`` shared type named ``"content"`` —
the shape y-codemirror.next's ``Y.Text`` binding expects. A future type
with a different shape is a different codec, not a parameter here.

pycrdt ships in the optional ``[crdt]`` extra. Importing THIS module costs
nothing without it; every function that needs the runtime imports lazily
and raises :class:`CrdtUnavailable` with the install hint. Nothing in the
package calls these functions unless a yjs-codec type is registered, and
the builtin yjs types register only when :func:`available` says so — a
deployment without the extra never reaches this error.
"""
from __future__ import annotations

#: The shared type every yjs-codec document carries its text in.
CONTENT_KEY = "content"

#: The canonical EMPTY Y update: ``Doc().get_update()`` — a state that
#: contains no clients and no operations. Pinned by a test against the
#: installed pycrdt so a codec upgrade that changes the wire form of
#: "nothing" fails loudly instead of corrupting stored empty bodies.
EMPTY_STATE = b"\x00\x00"


class CrdtUnavailable(ImportError):
    """pycrdt is not installed — the ``[crdt]`` extra is missing."""

    def __init__(self):
        super().__init__(
            "stapel_docs.crdt requires the optional 'pycrdt' dependency. "
            "Install it with:\n    pip install 'stapel-docs[crdt]'"
        )


def available() -> bool:
    """Whether the pycrdt runtime is importable.

    The registry gate for the builtin yjs types: cheap after the first call
    (a re-import of a loaded module is a ``sys.modules`` lookup)."""
    try:
        import pycrdt  # noqa: F401
    except ImportError:
        return False
    return True


def _pycrdt():
    try:
        import pycrdt
    except ImportError as exc:  # pragma: no cover - exercised via available()
        raise CrdtUnavailable() from exc
    return pycrdt


def fold(base_state: bytes, updates: list[bytes]) -> bytes:
    """Fold *updates* onto *base_state* and return the full state update.

    Order-independent for concurrent updates (CRDT commutativity) — the
    property the whole journal discipline rests on, pinned by tests. An
    empty/absent base means "start from nothing"; the result of folding
    nothing at all is :data:`EMPTY_STATE`.
    """
    pycrdt = _pycrdt()
    doc = pycrdt.Doc()
    if base_state:
        doc.apply_update(bytes(base_state))
    for payload in updates:
        doc.apply_update(bytes(payload))
    return doc.get_update()


def extract_text(state: bytes) -> str:
    """The ``"content"`` text of a stored Y state — what feeds search and
    knowledge extraction (``DocTypeSpec.text_extractor`` of the yjs types).
    An empty state extracts to ``""``."""
    pycrdt = _pycrdt()
    doc = pycrdt.Doc()
    if state:
        doc.apply_update(bytes(state))
    return str(doc.get(CONTENT_KEY, type=pycrdt.Text))


def is_valid_update(payload: bytes) -> bool:
    """Does *payload* parse as a Y update?

    Apply-validate against a throwaway doc: the same rust decoder that
    would later fold it is the only honest judge of the format. Guards the
    two write doors of yjs-codec types (journal append, body PUT) so a
    corrupt payload is a 400 at the boundary instead of an assembly that
    can never complete.
    """
    pycrdt = _pycrdt()
    if not payload:
        return False
    try:
        pycrdt.Doc().apply_update(bytes(payload))
    except (ValueError, TypeError):
        return False
    return True


__all__ = [
    "CONTENT_KEY",
    "EMPTY_STATE",
    "CrdtUnavailable",
    "available",
    "extract_text",
    "fold",
    "is_valid_update",
]
