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
from pathlib import Path

_FONT_DIR = Path(__file__).resolve().parent / "assets"
_FONT_FAMILY = "DejaVu"


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
    except ImportError as exc:
        raise ExporterUnavailable("install stapel-docs[pdf]") from exc

    class _Pdf(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font(_FONT_FAMILY, size=8)
            self.cell(0, 6, str(self.page_no()), align="C")

    return _Pdf


class PdfExporter:
    """txt/csv/md -> PDF via fpdf2 (extra ``[pdf]``).

    md is rendered verbatim as plain text in v1 (no markdown parsing).
    Unicode via the bundled DejaVu fonts (assets/) — fpdf2 core fonts are
    latin-1 only. Only attribute access on *document* (``title``); works
    on unsaved instances.
    """

    formats = ("pdf",)

    _TEXT_SLUGS = ("txt", "md")
    _BODY_SIZE = 11
    _LINE_H = 6
    _ROW_H = 7

    def export(self, document, body: bytes, spec) -> tuple[bytes, str]:
        if spec.slug in self._TEXT_SLUGS:
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
