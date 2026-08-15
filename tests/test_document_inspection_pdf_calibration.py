"""Practical searchable-PDF calibration for the frozen v1 product route."""

from __future__ import annotations

from pathlib import Path

import fitz  # type: ignore[import-untyped]

from local_steward.document_observation import DocumentInspectionRequest, inspect_document
from local_steward.models import PathConfig, ScopeConfig, ScopeRole, StewardConfig


def _config(tmp_path: Path) -> StewardConfig:
    root = tmp_path / "documents"
    root.mkdir()
    project = tmp_path / "project"
    paths = PathConfig(
        project / "data",
        project / "data/cache",
        project / "data/evidence",
        project / "data/quarantine",
    )
    scope = ScopeConfig("managed", ScopeRole.MANAGED_ROOT, str(root), root, True, False, False)
    return StewardConfig(1, "PDF calibration", paths, (scope,), project, project / "config.toml")


def _inspect(config: StewardConfig, name: str):
    return inspect_document(config, DocumentInspectionRequest("managed", name, True))


def test_searchable_chinese_and_representative_page_sizes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "documents" / "paper-sizes.pdf"
    document = fitz.open()
    a4 = document.new_page(width=fitz.paper_rect("a4").width, height=fitz.paper_rect("a4").height)
    a4.insert_text((72, 72), "中文可搜索事实：第一页", fontname="china-s")
    letter = document.new_page(
        width=fitz.paper_rect("letter").width, height=fitz.paper_rect("letter").height
    )
    letter.insert_text((72, 72), "searchable letter-sized page")
    document.save(source)
    document.close()

    page = _inspect(config, "paper-sizes.pdf")
    assert page.status == "COMPLETE"
    assert len(page.items) == 2
    assert "中文可搜索事实" in (page.items[0].text_or_value or "")
    assert "searchable letter-sized page" in (page.items[1].text_or_value or "")
    assert [item.location["page"] for item in page.items] == [1, 2]


def test_multi_column_order_is_deterministic_but_only_markdown_text(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "documents" / "columns.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "LEFT-ONE")
    page.insert_text((72, 110), "LEFT-TWO")
    page.insert_text((330, 72), "RIGHT-ONE")
    page.insert_text((330, 110), "RIGHT-TWO")
    document.save(source)
    document.close()

    first = _inspect(config, "columns.pdf")
    second = _inspect(config, "columns.pdf")
    text = first.items[0].text_or_value or ""
    assert first.status == second.status == "COMPLETE"
    assert first.document_observation_digest == second.document_observation_digest
    assert all(marker in text for marker in ("LEFT-ONE", "LEFT-TWO", "RIGHT-ONE", "RIGHT-TWO"))
    assert text.index("LEFT-ONE") < text.index("RIGHT-ONE") < text.index("LEFT-TWO")
    assert text.index("LEFT-TWO") < text.index("RIGHT-TWO")


def test_simple_ruled_table_is_markdown_projection_not_structured_cells(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "documents" / "table.pdf"
    document = fitz.open()
    page = document.new_page()
    for x in (72, 220, 360):
        page.draw_line((x, 72), (x, 156))
    for y in (72, 100, 128, 156):
        page.draw_line((72, y), (360, y))
    for x, y, value in (
        (82, 92, "Label"),
        (230, 92, "Value"),
        (82, 120, "Alpha"),
        (230, 120, "42"),
        (82, 148, "Beta"),
        (230, 148, "84"),
    ):
        page.insert_text((x, y), value)
    document.save(source)
    document.close()

    result = _inspect(config, "table.pdf")
    text = result.items[0].text_or_value or ""
    assert result.status == "COMPLETE"
    assert all(value in text for value in ("Label", "Value", "Alpha", "42", "Beta", "84"))
    assert "|" in text
    assert all(item.kind == "pdf_page_block" for item in result.items)


def test_password_protected_pdf_fails_closed_without_items(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "documents" / "encrypted.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "secret synthetic text")
    document.save(
        source,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="reader-secret",
    )
    document.close()

    result = _inspect(config, "encrypted.pdf")
    assert result.status == "PARSER_FAILED"
    assert result.items == ()
    assert result.returned_count == result.full_item_count == 0
    assert result.document_observation_digest is None
