"""Regression coverage for bounded native PDF structure projection."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from local_steward.document_execution import BoundedDocumentParseCache
from local_steward.file_agent.runtime import native_fidelity
from local_steward.file_agent.runtime.scope_binding import ScopeBinding, ScopeBindings
from local_steward.file_agent.runtime.structured_documents import (
    DocumentResourceUsage,
    NormalizedDocumentObservation,
    ProjectOwnedBoundedDocumentIngress,
    StructuredDocumentParserAdapter,
    _WorkerExecution,
    _pdf_page_count_for_query_routing,
)


def _outlined_pdf(path: Path, *, pages: int = 180) -> None:
    document = pymupdf.open()
    for page_number in range(1, pages + 1):
        page = document.new_page()
        page.insert_text((72, 72), f"Page body {page_number}")
    document.set_toc(
        [
            [1, "Introduction", 1],
            [1, "Cell materials", 60],
            [2, "Cathode", 75],
            [1, "Safety", 140],
        ]
    )
    document.save(path)
    document.close()


def test_pdf_structure_uses_native_hierarchy_without_whole_body_parse(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.pdf"
    _outlined_pdf(source)
    bindings = ScopeBindings(
        (ScopeBinding("scope", tmp_path),),
        (str(tmp_path),),
        ("scope",),
    )
    adapter = StructuredDocumentParserAdapter(
        ProjectOwnedBoundedDocumentIngress(bindings),
        parse_cache=BoundedDocumentParseCache[_WorkerExecution](),
    )

    observation = adapter.observe(
        {
            "scope_id": "scope",
            "relative_path": source.name,
            "parser_profile": "AUTO",
            "view": "STRUCTURE",
            "intent": "STRUCTURE",
        }
    )

    assert observation.status == "COMPLETE"
    assert observation.backend_name == "PyMuPDFNativeStructure"
    assert observation.execution is not None
    assert observation.execution.selected_profile == "STRUCTURE_NATIVE"
    assert [attempt.profile for attempt in observation.execution.attempts] == ["STRUCTURE_NATIVE"]
    assert observation.execution.attempts[0].cache_status == "MISS"
    assert observation.resources.parser_timeout_limit_ms == 6_000
    assert "PDF_NATIVE_STRUCTURE_BODY_NOT_PARSED" in observation.warnings
    root = next(item for item in observation.items if item.kind == "pdf_document")
    assert root.extension is not None
    assert root.extension["page_count"] == 180
    assert root.extension["page_body_parsed"] is False
    assert root.extension["page_auxiliary_scanned"] is False
    assert root.extension["native_outline_available"] is True
    assert root.extension["structure_completeness"] == "NATIVE_OUTLINE_COMPLETE"
    outlines = [item for item in observation.items if item.kind == "pdf_outline"]
    assert [item.text_or_value for item in outlines] == [
        "Introduction",
        "Cell materials",
        "Cathode",
        "Safety",
    ]
    assert root.node_id == "document:current"
    assert outlines[0].parent == root.node_id
    assert outlines[1].parent == root.node_id
    assert outlines[2].parent == outlines[1].node_id
    assert outlines[3].parent == root.node_id
    node_ids = {item.node_id for item in observation.items}
    assert all(item.parent is None or item.parent in node_ids for item in observation.items)
    assert "PDF_NATIVE_PAGE_AUXILIARY_NOT_SCANNED" in observation.warnings
    assert not any(item.kind == "pdf_page_block" for item in observation.items)


def test_pdf_structure_without_outline_reports_root_metadata_only(tmp_path: Path) -> None:
    source = tmp_path / "flat.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Body text is deliberately not parsed")
    document.save(source)
    document.close()
    bindings = ScopeBindings(
        (ScopeBinding("scope", tmp_path),),
        (str(tmp_path),),
        ("scope",),
    )
    adapter = StructuredDocumentParserAdapter(ProjectOwnedBoundedDocumentIngress(bindings))

    observation = adapter.observe(
        {
            "scope_id": "scope",
            "relative_path": source.name,
            "parser_profile": "AUTO",
            "view": "STRUCTURE",
            "intent": "STRUCTURE",
        }
    )

    root = next(item for item in observation.items if item.kind == "pdf_document")
    assert observation.status == "COMPLETE"
    assert root.extension is not None
    assert root.extension["native_outline_available"] is False
    assert root.extension["native_outline_entry_count"] == 0
    assert root.extension["structure_completeness"] == "ROOT_METADATA_ONLY"
    assert root.extension["inferred_headings_attempted"] is False
    assert "PDF_NATIVE_STRUCTURE_OUTLINE_ABSENT" in observation.warnings


def test_pdf_native_structure_failure_never_falls_back_to_body_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "timeout.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Body parse must not run")
    document.save(source)
    document.close()
    bindings = ScopeBindings(
        (ScopeBinding("scope", tmp_path),),
        (str(tmp_path),),
        ("scope",),
    )
    adapter = StructuredDocumentParserAdapter(ProjectOwnedBoundedDocumentIngress(bindings))
    profiles: list[str] = []

    def fail_native_structure(
        self: StructuredDocumentParserAdapter,
        admitted: Any,
        source_format: str,
        expanded_bytes: int,
        _suffix: str,
        profile: str,
        **_kwargs: object,
    ) -> tuple[NormalizedDocumentObservation, str]:
        profiles.append(profile)
        if profile != "STRUCTURE_NATIVE":
            raise AssertionError("native structure failure must not parse the PDF body")
        return (
            self._failure(
                "TIMEOUT",
                admitted.scope_id,
                admitted.relative_path,
                admitted.source_sha256,
                source_format,
                "PyMuPDFNativeStructure",
                DocumentResourceUsage(
                    admitted.source_bytes,
                    expanded_bytes,
                    6_000,
                    None,
                    0,
                    0,
                    deadline_stage="PARSER",
                ),
                "PARSER_TIMEOUT",
                failure_reason_code="PARSER_TIMEOUT",
            ),
            "MISS",
        )

    monkeypatch.setattr(
        StructuredDocumentParserAdapter,
        "_parse_profile",
        fail_native_structure,
    )

    observation = adapter.observe(
        {
            "scope_id": "scope",
            "relative_path": source.name,
            "parser_profile": "AUTO",
            "view": "STRUCTURE",
            "intent": "STRUCTURE",
        }
    )

    assert observation.status == "TIMEOUT"
    assert profiles == ["STRUCTURE_NATIVE"]
    assert observation.execution is not None
    assert [attempt.profile for attempt in observation.execution.attempts] == ["STRUCTURE_NATIVE"]
    assert observation.execution.selected_profile is None


def test_pdf_native_auxiliary_scan_and_warning_volume_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingPage:
        def annots(self) -> object:
            raise RuntimeError("synthetic annotation failure")

        def widgets(self) -> object:
            raise RuntimeError("synthetic form failure")

    class LargeDocument:
        is_repaired = False
        is_encrypted = False
        metadata: dict[str, str] = {}
        page_count = 600

        def __init__(self) -> None:
            self.loaded_pages: list[int] = []

        def get_toc(self, *, simple: bool) -> list[object]:
            assert simple is True
            return []

        def load_page(self, page_index: int) -> FailingPage:
            self.loaded_pages.append(page_index)
            return FailingPage()

        def close(self) -> None:
            return None

    document = LargeDocument()

    class FakePyMuPDF:
        @staticmethod
        def open(_source_path: str) -> LargeDocument:
            return document

    monkeypatch.setattr(native_fidelity, "import_module", lambda _name: FakePyMuPDF)

    items, warnings = native_fidelity.project_pdf_native("synthetic.pdf")

    assert items == []
    assert len(document.loaded_pages) == native_fidelity.MAX_PDF_NATIVE_AUXILIARY_PAGES
    assert warnings == [
        "PDF_ANNOTATIONS_UNAVAILABLE:count:512:sample_pages:1,2,3,4,5,6,7,8",
        "PDF_FORM_FIELDS_UNAVAILABLE:count:512:sample_pages:1,2,3,4,5,6,7,8",
        "PDF_NATIVE_AUXILIARY_PAGES_OMITTED:88",
    ]


def test_pdf_query_routing_uses_quiet_canonical_pymupdf_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "query.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "quiet query routing")
    document.save(source)
    document.close()
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "fitz":
            raise AssertionError("legacy fitz import may corrupt MCP stdout")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert _pdf_page_count_for_query_routing(source) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
