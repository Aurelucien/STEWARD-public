"""Acceptance for the stateless DocumentGraphV2 parsing foundation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import zipfile

import fitz  # type: ignore[import-untyped]
import pytest

from local_steward.document_observation import DocumentInspectionRequest, inspect_document
from local_steward.file_agent.runtime import identify_document_format

from .test_document_inspection_product import (
    _config,
    _write_docx,
    _write_epub,
    _write_pdf,
    _write_pptx,
    _write_xlsx,
)


@pytest.mark.parametrize(
    ("name", "writer", "marker"),
    [
        pytest.param(
            "sample.pdf",
            _write_pdf,
            "searchable PDF page 1",
            marks=pytest.mark.host_assets,
        ),
        ("sample.epub", _write_epub, "EPUB fact marker"),
        ("sample.docx", _write_docx, "DOCX fact marker"),
        ("sample.xlsx", _write_xlsx, "answer"),
        ("sample.pptx", _write_pptx, "PPTX fact marker"),
    ],
)
def test_deep_profile_projects_supported_formats_into_document_graph_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    writer: Callable[[Path], None],
    marker: str,
) -> None:
    _path, config = _config(tmp_path, monkeypatch)
    source = tmp_path / "documents" / name
    writer(source)

    page = inspect_document(
        config,
        DocumentInspectionRequest(
            "managed",
            name,
            True,
            parser_profile="DEEP",
            view="READ",
            content_query=marker,
        ),
    )

    assert page.status == "COMPLETE"
    assert page.backend_name == "Docling"
    assert page.protocol_version == 4
    assert page.view == "READ"
    assert page.items
    assert page.items[0].node_id == "document:root"
    assert page.items[0].role == "DOCUMENT"
    assert any(item.node_id and item.role for item in page.items)
    assert page.content_search is not None
    assert page.content_search.matched_item_count >= 1


def test_structure_and_read_views_share_one_complete_semantic_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _path, config = _config(tmp_path, monkeypatch)
    source = tmp_path / "documents" / "book.epub"
    _write_epub(source)

    read = inspect_document(
        config,
        DocumentInspectionRequest(
            "managed", "book.epub", True, parser_profile="DEEP", view="READ"
        ),
    )
    structure = inspect_document(
        config,
        DocumentInspectionRequest(
            "managed", "book.epub", True, parser_profile="DEEP", view="STRUCTURE"
        ),
    )

    assert read.document_observation_digest == structure.document_observation_digest
    assert len(structure.items) < len(read.items)
    assert {item.role for item in structure.items} == {"DOCUMENT", "HEADING"}


def test_epub_admission_rejects_encryption_and_unsafe_package_paths(tmp_path: Path) -> None:
    encrypted = tmp_path / "encrypted.epub"
    _write_epub(encrypted)
    with zipfile.ZipFile(encrypted, "a") as archive:
        archive.writestr("META-INF/encryption.xml", "<encryption/>")
    encryption_result = identify_document_format(encrypted.read_bytes(), encrypted.name)
    assert encryption_result.status == "UNSUPPORTED_FORMAT"
    assert encryption_result.reason == "ENCRYPTED_EPUB"

    unsafe = tmp_path / "unsafe.epub"
    _write_epub(unsafe)
    with zipfile.ZipFile(unsafe, "a") as archive:
        archive.writestr("../escape.xhtml", "unsafe")
    unsafe_result = identify_document_format(unsafe.read_bytes(), unsafe.name)
    assert unsafe_result.status == "RESOURCE_LIMIT"
    assert unsafe_result.reason == "UNSAFE_CONTAINER_PATH"


def test_auto_profile_uses_existing_local_ocr_for_an_image_only_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _path, config = _config(tmp_path, monkeypatch)
    source = tmp_path / "documents" / "scan.pdf"
    text_document = fitz.open()
    page = text_document.new_page(width=800, height=300)
    page.insert_text((60, 150), "STEWARD OCR 8675309", fontsize=42)
    pixels = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    image_bytes = pixels.tobytes("png")
    text_document.close()
    image_document = fitz.open()
    image_page = image_document.new_page(width=800, height=300)
    image_page.insert_image(image_page.rect, stream=image_bytes)
    image_document.save(source)
    image_document.close()

    result = inspect_document(
        config,
        DocumentInspectionRequest(
            "managed",
            "scan.pdf",
            True,
            parser_profile="AUTO",
            view="READ",
            content_query="8675309",
        ),
    )

    assert result.status == "COMPLETE", result
    assert result.backend_name == "STEWARDPageOCR"
    assert "OCR_ENGINE:RAPIDOCR_ONNXRUNTIME" in result.warnings
    assert result.content_search is not None
    assert result.content_search.matched_item_count >= 1
