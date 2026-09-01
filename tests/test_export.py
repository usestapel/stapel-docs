"""PDF export: unicode text/csv/md rendering, registry semantics."""
import sys
from uuid import uuid4

import pytest
from django.test import override_settings

from stapel_docs.doc_types import get_doc_type
from stapel_docs.exporters import (
    ExporterUnavailable,
    ExportFormatUnknown,
    ExportUnsupportedType,
    PdfExporter,
    _sanitize_html,
    get_exporter,
)
from stapel_docs.models import Document

CYRILLIC = "Привет, мир — стапель"

# One document exercising every construct the renderer claims to handle.
MARKDOWN = """# Заголовок первого уровня

Абзац с **жирным**, *курсивом* и `inline_code`, плюс
[ссылка](https://example.com/a).

## Второй уровень

- первый пункт
- второй пункт
    - вложенный пункт

1. раз
2. два

> цитата на русском

```python
def привет(x):
    return x * 2
```

| Колонка | Значение |
| --- | --- |
| Иванов | 42 |
| Smith | 7 |

![Схема потока](https://example.com/pic.png)

---

Последняя строка.
"""

# Every character sequence that must NOT survive into the rendered text
# layer: seeing one means the body was printed instead of parsed.
MARKER_SUBSTRINGS = ("# ", "**", "```", "](", "| ---")


def _doc(type_: str, title: str = "Экспорт — проверка") -> Document:
    # Unsaved on purpose: the exporter must only touch attributes.
    return Document(workspace_id=uuid4(), type=type_, title=title)


def _export(slug: str, body: bytes) -> bytes:
    out, mime = get_exporter("pdf").export(_doc(slug), body, get_doc_type(slug))
    assert mime == "application/pdf"
    return out


def _assert_pdf(out: bytes) -> None:
    assert out.startswith(b"%PDF")
    assert len(out) > 1024
    # Unicode font actually embedded (subset keeps the DejaVuSans name).
    assert b"DejaVuSans" in out


def _pdf_text(out: bytes) -> str:
    """Text layer of every page, or skip when pypdf is not installed.

    CI installs pypdf alongside pytest; a bare local checkout may not have
    it, and the claims that do not need it are asserted separately (see
    the `_sanitize_html` tests, which are pure and always run).
    """
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(__import__("io").BytesIO(out))
    return "\n".join(page.extract_text() for page in reader.pages)


def test_txt_cyrillic_renders_pdf():
    _assert_pdf(_export("txt", CYRILLIC.encode("utf-8")))


def test_txt_stays_verbatim():
    """txt is not markdown: its markers are content and must survive."""
    out = _export("txt", b"# not a heading\n**not bold**\n")
    _assert_pdf(out)
    text = _pdf_text(out)
    assert "# not a heading" in text
    assert "**not bold**" in text


def test_md_renders_pdf():
    _assert_pdf(_export("md", MARKDOWN.encode("utf-8")))


def test_md_html_carries_structure_not_markers():
    """The parse step, asserted without a PDF reader in the way."""
    html = _sanitize_html(
        __import__("markdown").markdown(
            MARKDOWN, extensions=["tables", "fenced_code", "sane_lists"]
        )
    )
    for tag in ("<h1>", "<h2>", "<strong>", "<em>", "<code>", "<ul>", "<ol>",
                "<blockquote>", "<pre>", "<table ", "<th>", "<td>", "<hr />"):
        assert tag in html, tag
    for marker in MARKER_SUBSTRINGS:
        assert marker not in html, marker


def test_md_text_layer_has_no_markdown_markers():
    text = _pdf_text(_export("md", MARKDOWN.encode("utf-8")))
    for marker in MARKER_SUBSTRINGS:
        assert marker not in text, marker
    # ...while the content itself is all there.
    for content in (
        "Заголовок первого уровня",
        "жирным",
        "курсивом",
        "inline_code",
        "ссылка",
        "первый пункт",
        "вложенный пункт",
        "цитата на русском",
        "def привет(x):",
        "Колонка",
        "Иванов",
        "Последняя строка.",
    ):
        assert content in text, content


def test_md_uses_bold_italic_and_monospace_faces():
    """Emphasis and code are different faces, not the body font twice."""
    out = _export("md", MARKDOWN.encode("utf-8"))
    assert b"DejaVuSansBook" in out  # body
    assert b"DejaVuSansBold" in out  # headings + **bold**
    assert b"DejaVuSansOblique" in out  # *italic*
    assert b"DejaVuSansMonoBook" in out  # `code` and fenced blocks


def test_md_link_becomes_a_pdf_annotation():
    pypdf = pytest.importorskip("pypdf")
    out = _export("md", MARKDOWN.encode("utf-8"))
    reader = pypdf.PdfReader(__import__("io").BytesIO(out))
    uris = [
        annot.get_object().get("/A", {}).get("/URI")
        for page in reader.pages
        for annot in (page.get("/Annots") or [])
    ]
    assert "https://example.com/a" in uris


def test_md_cyrillic_code_block_survives():
    """Fenced code is monospace AND unicode — fpdf2's Courier is neither."""
    body = f"```\n{CYRILLIC}\n```\n".encode("utf-8")
    out = _export("md", body)
    _assert_pdf(out)
    assert b"DejaVuSansMonoBook" in out
    assert CYRILLIC in _pdf_text(out)


def test_md_empty_body_still_renders():
    _assert_pdf(_export("md", b""))
    _assert_pdf(_export("md", b"   \n\n  \n"))


def test_long_md_page_breaks():
    body = "\n\n".join(f"## Раздел {i}\n\nтекст раздела {i}" for i in range(120))
    out = _export("md", body.encode("utf-8"))
    _assert_pdf(out)
    assert out.count(b"/Type /Page") > 1


def test_md_without_markdown_installed_is_loud():
    """Missing optional dep -> 503, never a silent verbatim fallback."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "markdown", None)  # makes `import markdown` raise
        with pytest.raises(ExporterUnavailable):
            _export("md", b"# heading\n")
        # ...and the formats that never needed it keep working.
        _assert_pdf(_export("txt", CYRILLIC.encode("utf-8")))
        _assert_pdf(_export("csv", b"a,b\n1,2\n"))


# --------------------------------------------------------------------------
# Sanitizer — the body is user input, and fpdf2 acts on some of these tags
# --------------------------------------------------------------------------

def test_sanitizer_drops_images_before_fpdf_can_fetch_them():
    """fpdf2 resolves an <img src> by fetching it; nothing reaches it."""
    html = _sanitize_html(
        '<p><img alt="Схема" src="http://169.254.169.254/latest/meta-data/" />'
        '<img src="https://example.com/pic.png" /></p>'
    )
    assert "img" not in html
    assert "169.254.169.254" not in html
    assert "<i>Схема</i>" in html  # alt text is content, and it survives


def test_md_body_with_remote_image_exports_without_fetching():
    body = "![Схема потока](http://169.254.169.254/latest/meta-data/)\n".encode("utf-8")
    out = _export("md", body)
    _assert_pdf(out)
    assert "Схема потока" in _pdf_text(out)


def test_sanitizer_drops_script_and_style_content():
    html = _sanitize_html("<p>a</p><script>alert(1)</script><style>p{}</style><p>b</p>")
    assert "alert" not in html and "p{}" not in html
    assert "<p>a</p>" in html and "<p>b</p>" in html


def test_sanitizer_keeps_only_safe_link_schemes():
    kept = _sanitize_html('<a href="https://example.com/x">ok</a>')
    assert 'href="https://example.com/x"' in kept
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "/relative"):
        html = _sanitize_html(f'<a href="{bad}">текст</a>')
        assert "<a" not in html  # the anchor goes...
        assert "текст" in html  # ...its text stays


def test_sanitizer_strips_unlisted_attributes():
    html = _sanitize_html('<p id="x" style="color:red" onclick="x()">hi</p>')
    assert html == "<p>hi</p>"
    assert '<font face="Nonexistent">' not in _sanitize_html(
        '<font face="Nonexistent">hi</font>'
    )


def test_sanitizer_asks_for_table_borders():
    """Markdown cannot request borders; fpdf2 draws a grid only if asked."""
    assert '<table border="1">' in _sanitize_html("<table><tr><td>x</td></tr></table>")
    # ...and an author-supplied value does not get to turn them off.
    assert '<table border="1">' in _sanitize_html('<table border="0"><tr></tr></table>')


def test_sanitizer_balances_unclosed_and_stray_tags():
    assert _sanitize_html("<b>bold") == "<b>bold</b>"
    assert _sanitize_html("bold</b>") == "bold"
    assert _sanitize_html("<p><b>x</p>") == "<p><b>x</b></p>"


def test_sanitizer_keeps_text_of_unknown_tags():
    assert _sanitize_html("<marquee>текст</marquee>") == "текст"


def test_sanitizer_escapes_text():
    assert _sanitize_html("<p>a &lt; b &amp; c</p>") == "<p>a &lt; b &amp; c</p>"


# --------------------------------------------------------------------------
# csv + registry (unchanged behavior)
# --------------------------------------------------------------------------

def test_csv_table_renders_pdf():
    body = (
        "name,city,note\n"
        '"Иванов, Иван",Москва,"со ""кавычками"" внутри"\n'
        f"Smith,London,{CYRILLIC}\n"
        "a,b,c\n"
    ).encode("utf-8")
    _assert_pdf(_export("csv", body))


def test_long_csv_page_breaks():
    body = ("col1,col2,col3\n" + "\n".join(f"row{i},данные,{i}" for i in range(200))).encode("utf-8")
    out = _export("csv", body)
    _assert_pdf(out)
    assert out.count(b"/Type /Page") > 1  # long tables span pages


def test_file_type_unsupported():
    exporter = PdfExporter()
    with pytest.raises(ExportUnsupportedType):
        exporter.export(_doc("file"), b"\x00\x01", get_doc_type("file"))


def test_unknown_format_raises():
    with pytest.raises(ExportFormatUnknown):
        get_exporter("docx")


def test_overlay_removes_builtin():
    with override_settings(STAPEL_DOCS={"EXPORTERS": {"pdf": None}}):
        with pytest.raises(ExportFormatUnknown):
            get_exporter("pdf")
