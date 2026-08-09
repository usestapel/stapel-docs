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


class ExportFormatUnknown(Exception):
    """No exporter registered for the requested format."""


class ExportUnsupportedType(Exception):
    """The exporter cannot render this document type."""


class ExporterUnavailable(Exception):
    """Optional dependency for the exporter is not installed."""


class PdfExporter:
    """txt/csv/md -> PDF via fpdf2 (extra ``[pdf]``). Implementation lands
    with the export workstream; the interface is fixed here."""

    formats = ("pdf",)

    def export(self, document, body: bytes, spec) -> tuple[bytes, str]:
        raise ExporterUnavailable("PDF export not implemented yet")


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
