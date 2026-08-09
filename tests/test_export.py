"""PDF export: unicode text/csv/md rendering, registry semantics."""
from uuid import uuid4

import pytest
from django.test import override_settings

from stapel_docs.doc_types import get_doc_type
from stapel_docs.exporters import (
    ExportFormatUnknown,
    ExportUnsupportedType,
    PdfExporter,
    get_exporter,
)
from stapel_docs.models import Document

CYRILLIC = "Привет, мир — стапель"


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


def test_txt_cyrillic_renders_pdf():
    _assert_pdf(_export("txt", CYRILLIC.encode("utf-8")))


def test_md_verbatim_renders_pdf():
    body = f"# Заголовок\n\n*{CYRILLIC}* — `code` [link](x)\n".encode("utf-8")
    _assert_pdf(_export("md", body))


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
