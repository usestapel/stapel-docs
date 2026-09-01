"""Export registry — render a document body into a download format.

Open merge registry (library-standard §3.3): builtins below <-
``STAPEL_DOCS["EXPORTERS"]`` overlay ({format: dotted-path | None}).

Exporter contract::

    class Exporter:
        formats: tuple[str, ...]          # e.g. ("pdf",)
        def export(self, document, body: bytes, spec: DocTypeSpec) -> tuple[bytes, str]:
            ...returns (rendered bytes, mime type)

Raise :class:`ExportUnsupportedType` when the document's type cannot be
rendered (e.g. an opaque ``file``); raise :class:`ExporterUnavailable`
when a required optional dependency is missing (``stapel-docs[pdf]``) —
the view maps them to 400 / 503 respectively.
"""
from __future__ import annotations

import csv
import io
from html import escape as _escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

_FONT_DIR = Path(__file__).resolve().parent / "assets"
_FONT_FAMILY = "DejaVu"
_MONO_FAMILY = "DejaVuMono"


class ExportFormatUnknown(Exception):
    """No exporter registered for the requested format."""


class ExportUnsupportedType(Exception):
    """The exporter cannot render this document type."""


class ExporterUnavailable(Exception):
    """Optional dependency for the exporter is not installed."""


def _fpdf_class():
    """Lazy import — fpdf2 ships in the ``[pdf]`` extra only."""
    try:
        from fpdf import FPDF
        from fpdf.html import HTML2FPDF
    except ImportError as exc:
        raise ExporterUnavailable("install stapel-docs[pdf]") from exc

    class _Html(HTML2FPDF):
        """`HTML2FPDF_CLASS`, fpdf2's documented hook, for one fix.

        fpdf2 draws a list marker with the PDF's *live* font instead of the
        one the paragraph is about to use, and a heading leaves that font
        bold at the heading's size. So every list under an `##` — most
        lists in a document — got a bold 15pt `1.` beside 11pt text. The
        marker is pinned to the body face here. `**kwargs` on purpose: a
        future fpdf2 that passes the bullet differently loses the fix rather
        than raising mid-export.
        """

        def _new_paragraph(self, *args, **kwargs):
            if kwargs.get("bullet") is not None:
                self.pdf.set_font(self.pdf.font_family, "", self.font_size_pt)
            return super()._new_paragraph(*args, **kwargs)

    class _Pdf(FPDF):
        HTML2FPDF_CLASS = _Html

        def footer(self):
            self.set_y(-12)
            self.set_font(_FONT_FAMILY, size=8)
            self.cell(0, 6, str(self.page_no()), align="C")

    return _Pdf


def _markdown_module():
    """Lazy import — Python-Markdown ships in the ``[pdf]`` extra only.

    Loud, not degraded: an md export on an install missing this dependency
    raises ``ExporterUnavailable`` (-> 503) exactly like a missing fpdf2,
    rather than falling back to the pre-0.4.1 verbatim rendering. A silent
    fallback is the shape of failure this module refuses everywhere else —
    the caller would receive a 200 with a PDF full of literal ``#`` and
    ``**``, indistinguishable at the point of use from a rendered one, and
    nobody would learn the install is incomplete. txt and csv exports are
    unaffected: only the md path imports this.
    """
    try:
        import markdown
    except ImportError as exc:
        raise ExporterUnavailable("install stapel-docs[pdf]") from exc
    return markdown


# --------------------------------------------------------------------------
# Markdown -> sanitized HTML
# --------------------------------------------------------------------------

# Tags fpdf2's write_html understands and this module is willing to hand it.
_ALLOWED_TAGS = frozenset(
    {
        "h1", "h2", "h3", "h4", "h5", "h6",
        "p", "br", "hr", "blockquote", "pre", "code",
        "ul", "ol", "li",
        "table", "thead", "tbody", "tr", "th", "td",
        "a", "b", "strong", "i", "em", "u", "s", "del",
        "sup", "sub",
    }
)

# Tags whose *content* is markup for a machine, not prose for a reader.
_DROP_CONTENT_TAGS = frozenset({"script", "style", "head", "title"})

# Per-tag attribute allowlist. Everything unlisted is dropped, including
# `style`, `width` and `attr_list`-injected ids: fpdf2 acts on some of them
# (a `<font face=...>` naming a font this document never registered is an
# exception, i.e. a 500 written by whoever typed the document body).
_ALLOWED_ATTRS = {
    "a": ("href",),
    "ol": ("start", "type"),
    "ul": ("type",),
    "td": ("align", "colspan", "rowspan"),
    "th": ("align", "colspan", "rowspan"),
}

_SAFE_SCHEMES = ("http", "https", "mailto")

# Void elements — emitted self-closed so the parser sees a balanced tree.
_VOID_TAGS = frozenset({"br", "hr"})


class _HtmlSanitizer(HTMLParser):
    """Rewrite Python-Markdown's HTML into the subset fpdf2 may be handed.

    Markdown passes raw HTML through untouched, so the document body — user
    input — reaches the renderer as markup. Two things follow. ``<img>`` is
    the sharp one: fpdf2 resolves an image ``src`` by *fetching it*, which
    turns "export this document" into "make this server issue a request of
    the author's choosing". Images are dropped here, before fpdf2 sees them
    (alt text survives as italics, so a figure's caption is not lost).
    Everything else is an allowlist: unknown tags lose their markup but keep
    their text, ``<script>``/``<style>`` lose both.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        # (tag, emitted) for every open non-void element, dropped ones
        # included — so a `</a>` whose `<a>` was dropped is dropped too, and
        # fpdf2 is never handed a close without its open.
        self._stack: list[tuple[str, bool]] = []
        self._muted = 0

    # -- collected result ---------------------------------------------------
    @property
    def html(self) -> str:
        return "".join(self._out)

    def close(self) -> None:
        super().close()
        # Unbalanced input (raw HTML in the body) closes here, not in fpdf2.
        for tag, emitted in reversed(self._stack):
            if emitted:
                self._out.append(f"</{tag}>")
        self._stack.clear()

    # -- HTMLParser hooks ---------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT_TAGS:
            self._muted += 1
            return
        if self._muted:
            return
        if tag == "img":
            alt = dict(attrs).get("alt") or ""
            if alt.strip():
                self._out.append(f"<i>{_escape(alt)}</i>")
            return
        if tag in _VOID_TAGS:
            self._out.append(f"<{tag} />")
            return
        if tag == "table":
            # fpdf2 draws a full grid only for a table that asks for borders,
            # and markdown has no syntax for asking. A markdown table is a
            # grid here, like the csv exporter's — so the attribute is set,
            # not passed through (any author-supplied `border` was already
            # dropped by the allowlist).
            self._out.append('<table border="1">')
            self._stack.append((tag, True))
            return
        # fpdf2 reads `<a>`'s href unconditionally; an anchor whose href this
        # module will not pass on is dropped whole, keeping its text.
        emitted = tag in _ALLOWED_TAGS and (tag != "a" or self._safe_href(attrs))
        if emitted:
            self._out.append(f"<{tag}{self._attrs(tag, attrs)}>")
        self._stack.append((tag, emitted))

    def handle_startendtag(self, tag, attrs):
        if tag in _DROP_CONTENT_TAGS or self._muted:
            return
        if tag == "img" or tag in _VOID_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT_TAGS:
            self._muted = max(0, self._muted - 1)
            return
        if self._muted or tag in _VOID_TAGS:
            return
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] != tag:
                continue
            for open_tag, emitted in reversed(self._stack[i:]):
                if emitted:
                    self._out.append(f"</{open_tag}>")
            del self._stack[i:]
            return
        # Stray close with no open: dropped.

    def handle_data(self, data):
        if not self._muted:
            self._out.append(_escape(data))

    @staticmethod
    def _safe_href(attrs) -> bool:
        href = dict(attrs).get("href") or ""
        return urlsplit(href).scheme.lower() in _SAFE_SCHEMES

    @staticmethod
    def _attrs(tag, attrs) -> str:
        allowed = _ALLOWED_ATTRS.get(tag, ())
        out = []
        for name, value in attrs:
            if name not in allowed or value is None:
                continue
            out.append(f' {name}="{_escape(value, quote=True)}"')
        return "".join(out)


def _sanitize_html(html: str) -> str:
    parser = _HtmlSanitizer()
    parser.feed(html)
    parser.close()
    return parser.html


class PdfExporter:
    """txt/csv/md -> PDF via fpdf2 (extra ``[pdf]``).

    md is parsed (Python-Markdown) and rendered with real structure since
    0.4.1: headings sized, bold/italic honored, lists bulleted/numbered,
    fenced code in a monospace face, tables as bordered grids, links
    clickable. Unicode via the bundled DejaVu fonts (assets/) — fpdf2 core
    fonts are latin-1 only, and that includes the Courier fpdf2 would
    otherwise reach for on ``<code>``, which is why the monospace face is
    bundled too. Only attribute access on *document* (``title``); works on
    unsaved instances.
    """

    formats = ("pdf",)

    _BODY_SIZE = 11
    _LINE_H = 6
    _ROW_H = 7

    def export(self, document, body: bytes, spec) -> tuple[bytes, str]:
        if spec.slug == "md":
            render = self._render_md
        elif spec.slug == "txt":
            render = self._render_text
        elif spec.slug == "csv":
            render = self._render_csv
        else:
            raise ExportUnsupportedType(spec.slug)
        pdf = self._new_pdf(getattr(document, "title", "") or "")
        render(pdf, body.decode("utf-8", errors="replace"))
        return bytes(pdf.output()), "application/pdf"

    def _new_pdf(self, title: str):
        pdf = _fpdf_class()(format="A4")
        pdf.add_font(_FONT_FAMILY, "", str(_FONT_DIR / "DejaVuSans.ttf"))
        pdf.add_font(_FONT_FAMILY, "B", str(_FONT_DIR / "DejaVuSans-Bold.ttf"))
        pdf.set_auto_page_break(True, margin=18)
        pdf.add_page()
        if title:
            pdf.set_font(_FONT_FAMILY, "B", 14)
            pdf.multi_cell(0, 8, text=title)
            pdf.ln(2)
        pdf.set_font(_FONT_FAMILY, "", self._BODY_SIZE)
        return pdf

    @staticmethod
    def _add_markdown_fonts(pdf) -> None:
        """The three faces only a rendered markdown body needs.

        Registered on the md path alone: `add_font` re-parses the TTF for
        every document exported, and a txt or csv export has nothing to do
        with italics or code.
        """
        pdf.add_font(_FONT_FAMILY, "I", str(_FONT_DIR / "DejaVuSans-Oblique.ttf"))
        pdf.add_font(_FONT_FAMILY, "BI", str(_FONT_DIR / "DejaVuSans-BoldOblique.ttf"))
        pdf.add_font(_MONO_FAMILY, "", str(_FONT_DIR / "DejaVuSansMono.ttf"))

    def _md_tag_styles(self):
        """Override fpdf2's defaults that do not survive this font set.

        `code`/`pre` default to Courier — a latin-1 core font, so a cyrillic
        identifier inside a fenced block renders as nothing recognizable.
        Headings default to a dark red at sizes tuned for a 12pt Times body;
        this is a document export, so they are black and scaled off the 11pt
        DejaVu body instead.
        """
        from fpdf.fonts import FontFace, TextStyle

        heading = {
            "h1": 18,
            "h2": 15,
            "h3": 13,
            "h4": 12,
            "h5": 11,
            "h6": 10,
        }
        styles = {
            "code": FontFace(family=_MONO_FAMILY),
            "pre": TextStyle(
                font_family=_MONO_FAMILY, font_size_pt=9.5, t_margin=3, b_margin=3
            ),
            "blockquote": TextStyle(
                color="#444444", font_style="I", l_margin=8, t_margin=3, b_margin=3
            ),
            # The list marker inherits whatever size the previous block left
            # behind unless the style pins it — a `1.` in 18pt next to 11pt
            # text otherwise.
            "li": TextStyle(font_size_pt=self._BODY_SIZE, l_margin=5, t_margin=1),
            "ul": TextStyle(font_size_pt=self._BODY_SIZE, t_margin=2),
            "ol": TextStyle(font_size_pt=self._BODY_SIZE, t_margin=2),
            "p": TextStyle(font_size_pt=self._BODY_SIZE, b_margin=1),
        }
        for tag, size in heading.items():
            styles[tag] = TextStyle(
                font_style="B",
                font_size_pt=size,
                color="#000000",
                t_margin=4,
                b_margin=1,
            )
        return styles

    def _render_md(self, pdf, text: str) -> None:
        markdown = _markdown_module()
        html = _sanitize_html(
            markdown.markdown(
                text,
                extensions=["tables", "fenced_code", "sane_lists"],
                output_format="html",
            )
        )
        if not html.strip():
            return
        self._add_markdown_fonts(pdf)
        pdf.write_html(
            html,
            font_family=_FONT_FAMILY,
            tag_styles=self._md_tag_styles(),
            # Grid, not just a rule under the header row (with border="1"
            # from the sanitizer this is fpdf2's ALL layout).
            table_line_separators=True,
            # Bullets and numbers are text, not decoration: fpdf2 paints
            # them dark red by default.
            li_prefix_color=(60, 60, 60),
            # The tree comes from the sanitizer above, which closes what it
            # opens; a warning here would only ever be about fpdf2's own
            # expectations, on a stream no operator can act on.
            warn_on_tags_not_matching=False,
        )

    def _render_text(self, pdf, text: str) -> None:
        # Verbatim body; multi_cell wraps lines and page-breaks itself.
        if text:
            pdf.multi_cell(0, self._LINE_H, text=text)

    def _render_csv(self, pdf, text: str) -> None:
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            return
        ncols = max(len(row) for row in rows)
        if not ncols:
            return
        col_w = pdf.epw / ncols
        for row in rows:
            if pdf.will_page_break(self._ROW_H):
                pdf.add_page()
            for i in range(ncols):
                value = row[i] if i < len(row) else ""
                pdf.cell(col_w, self._ROW_H, self._fit(pdf, value, col_w - 2), border=1)
            pdf.ln(self._ROW_H)

    @staticmethod
    def _fit(pdf, text: str, max_w: float) -> str:
        """One-line cell text; too-long values truncate with an ellipsis."""
        text = " ".join(text.split())
        if pdf.get_string_width(text) <= max_w:
            return text
        while text and pdf.get_string_width(text + "…") > max_w:
            text = text[:-1]
        return text + "…"


BUILTIN_EXPORTERS = {
    "pdf": PdfExporter,
}


def get_exporter(fmt: str):
    """Instantiate the exporter for *fmt* or raise ExportFormatUnknown."""
    from django.utils.module_loading import import_string

    from .conf import docs_settings

    registry = dict(BUILTIN_EXPORTERS)
    for name, dotted in (docs_settings.EXPORTERS or {}).items():
        if dotted is None:
            registry.pop(name, None)
        else:
            registry[name] = import_string(dotted) if isinstance(dotted, str) else dotted
    try:
        cls = registry[fmt]
    except KeyError:
        raise ExportFormatUnknown(fmt) from None
    return cls()


__all__ = [
    "ExportFormatUnknown",
    "ExportUnsupportedType",
    "ExporterUnavailable",
    "PdfExporter",
    "BUILTIN_EXPORTERS",
    "get_exporter",
]
