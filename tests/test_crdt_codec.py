"""The pycrdt codec (`crdt.py`): the empty-state pin, folding, extraction.

The codec is the ONE place this library parses a crdt body (the body-blind
rule holds: it is reached only through the yjs-codec types' own
``text_extractor`` and the assembly path). Everything here is pure — no
database, no Django settings.
"""
import pytest

pycrdt = pytest.importorskip("pycrdt")

from stapel_docs import crdt  # noqa: E402


def _state_of(text: str) -> bytes:
    """A full Y state whose "content" Text holds *text* (a client's doc)."""
    doc = pycrdt.Doc()
    doc["content"] = pycrdt.Text()
    doc.get("content", type=pycrdt.Text).insert(0, text)
    return doc.get_update()


def test_empty_state_pin():
    """b"\\x00\\x00" is the canonical empty Y update — pinned against the
    installed pycrdt, so a codec upgrade that changes the wire form of
    "nothing" fails here instead of corrupting stored EMPTY_STATE bodies."""
    assert pycrdt.Doc().get_update() == crdt.EMPTY_STATE


def test_available_reports_the_import():
    assert crdt.available() is True


def test_fold_from_empty_base():
    state = crdt.fold(crdt.EMPTY_STATE, [_state_of("hello")])
    assert crdt.extract_text(state) == "hello"


def test_fold_no_base():
    state = crdt.fold(b"", [_state_of("hello")])
    assert crdt.extract_text(state) == "hello"


def test_fold_convergence_is_order_independent():
    """Interleaved updates from two clients fold to the same state in any
    order — the property the whole journal discipline rests on."""
    a = pycrdt.Doc()
    a["content"] = pycrdt.Text()
    at = a.get("content", type=pycrdt.Text)
    at += "AAA"
    base = a.get_update()

    b = pycrdt.Doc()
    b.apply_update(base)
    bt = b.get("content", type=pycrdt.Text)
    bt += "BBB"
    diff_b = b.get_update(a.get_state())

    at += "CCC"
    diff_a = a.get_update()

    one = crdt.fold(crdt.EMPTY_STATE, [diff_a, diff_b])
    other = crdt.fold(crdt.EMPTY_STATE, [diff_b, diff_a])
    # Convergence is SEMANTIC, not byte-level: the update encoding may order
    # its per-client sections differently run to run (client ids are random),
    # and concurrent same-position inserts tie-break BY client id — so the
    # text is one of the two interleavings, but the same one in both orders.
    assert crdt.extract_text(one) == crdt.extract_text(other)
    assert crdt.extract_text(one) in ("AAABBBCCC", "AAACCCBBB")
    # And neither side holds an operation the other lacks.
    doc_one = pycrdt.Doc()
    doc_one.apply_update(one)
    doc_other = pycrdt.Doc()
    doc_other.apply_update(other)
    assert doc_one.get_update(doc_other.get_state()) == doc_other.get_update(
        doc_one.get_state()
    )


def test_fold_onto_stored_base_state():
    base = crdt.fold(crdt.EMPTY_STATE, [_state_of("start")])
    doc = pycrdt.Doc()
    doc.apply_update(base)
    text = doc.get("content", type=pycrdt.Text)
    text += " more"
    folded = crdt.fold(base, [doc.get_update()])
    assert crdt.extract_text(folded) == "start more"


def test_extract_text_of_empty_state():
    assert crdt.extract_text(crdt.EMPTY_STATE) == ""


def test_is_valid_update():
    assert crdt.is_valid_update(_state_of("x")) is True
    assert crdt.is_valid_update(crdt.EMPTY_STATE) is True
    assert crdt.is_valid_update(b"# just markdown text") is False
    assert crdt.is_valid_update(b"") is False
