"""Regression coverage for resilient EPUB routing and safe parser diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import zipfile

import pytest

from local_steward.file_agent.runtime import (
    ProjectOwnedBoundedDocumentIngress,
    ScopeBinding,
    ScopeBindings,
    StructuredDocumentParserAdapter,
)
from local_steward.file_agent.runtime.structured_documents import _WorkerExecution


def _bindings(tmp_path: Path) -> tuple[Path, ScopeBindings]:
    root = tmp_path / "epub"
    root.mkdir()
    return root, ScopeBindings((ScopeBinding("managed", root),), (str(root),), ("managed",))


def _write_tolerant_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "OPS/book.opf",
            '<package xmlns="http://www.idpf.org/2007/opf">'
            '<manifest><item id="two" href="two.html" media-type="text/html"/>'
            '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>',
        )
        archive.writestr(
            "OPS/one.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<h1>First chapter</h1><p>ordinary XML content</p></body></html>',
        )
        archive.writestr(
            "OPS/two.html",
            "<html><body><h2>Second chapter</h2>"
            "<p>RESILIENT_EPUB_MARKER & retained<br>after break</p></body></html>",
        )


@dataclass
class _NeverWorker:
    def run(self, _source_path: Path) -> _WorkerExecution:
        raise AssertionError("Docling must not run for an EPUB query map")


@dataclass
class _FailedDoclingWorker:
    def run(self, _source_path: Path) -> _WorkerExecution:
        return _WorkerExecution(
            "PARSER_FAILED",
            None,
            1,
            1_024,
            "PARSER_BACKEND_EXCEPTION",
            "RuntimeError",
        )


def _unsafe_backend_exception(_source_path: str) -> dict[str, object]:
    raise RuntimeError("sensitive /Users/example/private/book.epub parser detail")


@pytest.mark.parametrize("intent", ["EVIDENCE", "LOCATE"])
def test_small_epub_queries_route_directly_to_tolerant_map(
    tmp_path: Path, intent: str
) -> None:
    root, bindings = _bindings(tmp_path)
    source = root / "book.epub"
    _write_tolerant_epub(source)
    adapter = StructuredDocumentParserAdapter(ProjectOwnedBoundedDocumentIngress(bindings))
    adapter.docling_worker = _NeverWorker()  # type: ignore[assignment]

    observation = adapter.observe(
        {
            "scope_id": "managed",
            "relative_path": source.name,
            "parser_profile": "AUTO",
            "view": "READ",
            "intent": intent,
            "content_query": "RESILIENT_EPUB_MARKER",
        }
    )

    assert observation.status == "COMPLETE"
    assert observation.backend_name == "STEWARDStreamingMap"
    assert observation.execution is not None
    assert observation.execution.initial_profile == "MAP"
    assert [attempt.profile for attempt in observation.execution.attempts] == ["MAP"]
    assert any("RESILIENT_EPUB_MARKER" in (item.text_or_value or "") for item in observation.items)
    assert "EPUB_HTML_TOLERANT_FALLBACK_COUNT:1" in observation.warnings


@pytest.mark.parametrize(
    ("requested_profile", "view"),
    [("AUTO", "READ"), ("AUTO", "STRUCTURE"), ("DEEP", "READ"), ("DEEP", "STRUCTURE")],
)
def test_epub_read_and_structure_fall_back_to_native_container_projection(
    tmp_path: Path,
    requested_profile: str,
    view: str,
) -> None:
    root, bindings = _bindings(tmp_path)
    source = root / "book.epub"
    _write_tolerant_epub(source)
    adapter = StructuredDocumentParserAdapter(ProjectOwnedBoundedDocumentIngress(bindings))
    adapter.docling_worker = _FailedDoclingWorker()  # type: ignore[assignment]

    observation = adapter.observe(
        {
            "scope_id": "managed",
            "relative_path": source.name,
            "parser_profile": requested_profile,
            "view": view,
            "intent": view,
        }
    )

    assert observation.status == "COMPLETE"
    assert observation.backend_name == "STEWARDNativeEpub"
    assert observation.execution is not None
    assert [attempt.profile for attempt in observation.execution.attempts] == ["DEEP", "FAST"]
    failed = observation.execution.attempts[0]
    assert failed.failure_reason_code == "PARSER_BACKEND_EXCEPTION"
    assert failed.failure_exception_type == "RuntimeError"
    assert observation.execution.selected_profile == "FAST"
    assert any(item.role == "DOCUMENT" for item in observation.items)
    if view == "READ":
        assert any(
            "RESILIENT_EPUB_MARKER" in (item.text_or_value or "")
            for item in observation.items
        )
    else:
        assert any(item.role == "SECTION" for item in observation.items)
        assert any(item.role == "HEADING" for item in observation.items)
    assert "EPUB_HTML_TOLERANT_FALLBACK_COUNT:1" in observation.warnings


def test_isolated_parser_reports_only_safe_exception_identity(tmp_path: Path) -> None:
    root, bindings = _bindings(tmp_path)
    source = root / "book.epub"
    _write_tolerant_epub(source)
    adapter = StructuredDocumentParserAdapter(ProjectOwnedBoundedDocumentIngress(bindings))
    adapter.docling_worker.worker_target = _unsafe_backend_exception

    observation = adapter.observe(
        {
            "scope_id": "managed",
            "relative_path": source.name,
            "parser_profile": "DEEP",
            "view": "READ",
            "intent": "READ",
        }
    )

    assert observation.status == "COMPLETE"
    assert observation.execution is not None
    failed = observation.execution.attempts[0]
    assert failed.status == "PARSER_FAILED"
    assert failed.failure_reason_code == "PARSER_BACKEND_EXCEPTION"
    assert failed.failure_exception_type == "RuntimeError"
    serialized = json.dumps(observation.payload(), sort_keys=True)
    assert "sensitive" not in serialized
    assert "/Users/" not in serialized
    assert "traceback" not in serialized.casefold()
