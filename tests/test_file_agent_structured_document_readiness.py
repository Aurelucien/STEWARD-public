"""Cross-format offline readiness coverage for the shared Parser Adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from typing import Any
import zipfile

from local_steward.file_agent.runtime import (
    CURRENT_FILESYSTEM_DOCUMENT,
    ProjectOwnedBoundedDocumentIngress,
    ScopeBinding,
    ScopeBindings,
    StructuredDocumentParserAdapter,
)
from local_steward.file_agent.runtime.structured_documents import _WorkerExecution


_DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)


def _bindings(tmp_path: Path) -> tuple[Path, ScopeBindings]:
    root = tmp_path / "isolated-cross-format"
    root.mkdir(parents=True)
    return root, ScopeBindings((ScopeBinding("managed", root),), (str(root),), ("managed",))


def _arguments(path: str) -> dict[str, object]:
    return {"scope_id": "managed", "relative_path": path}


def _container(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)


def _docx_content_types() -> bytes:
    return (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/word/document.xml" ContentType="{_DOCX_CONTENT_TYPE}"/>'
        "</Types>"
    ).encode()


def _worker_payload(backend_name: str) -> dict[str, Any]:
    return {
        "backend_name": backend_name,
        "backend_version": "test-version",
        "warnings": [],
        "items": [
            {
                "kind": "test_document_block",
                "text_or_value": "synthetic observation",
                "parent": None,
                "location": {"block": 1},
                "extension": None,
            }
        ],
    }


@dataclass
class _RecordingWorker:
    backend_name: str
    paths: list[Path] = field(default_factory=list)

    def run(self, source_path: Path) -> _WorkerExecution:
        self.paths.append(source_path)
        return _WorkerExecution("COMPLETE", _worker_payload(self.backend_name), 1, 1_024)


@dataclass
class _UnavailableWorker:
    paths: list[Path] = field(default_factory=list)

    def run(self, source_path: Path) -> _WorkerExecution:
        self.paths.append(source_path)
        return _WorkerExecution("UNAVAILABLE", None, 1, 1_024)


def _adapter(
    tmp_path: Path,
) -> tuple[Path, StructuredDocumentParserAdapter, dict[str, _RecordingWorker]]:
    root, bindings = _bindings(tmp_path)
    workers = {
        "PDF": _RecordingWorker("PyMuPDF4LLM"),
        "DOCX": _RecordingWorker("MarkItDown"),
        "XLSX": _RecordingWorker("openpyxl"),
        "PPTX": _RecordingWorker("python-pptx"),
    }
    adapter = StructuredDocumentParserAdapter(ProjectOwnedBoundedDocumentIngress(bindings))
    adapter.worker = workers["PDF"]  # type: ignore[assignment]
    adapter.docx_worker = workers["DOCX"]  # type: ignore[assignment]
    adapter.xlsx_worker = workers["XLSX"]  # type: ignore[assignment]
    adapter.pptx_worker = workers["PPTX"]  # type: ignore[assignment]
    return root, adapter, workers


def test_shared_adapter_routes_all_supported_formats_by_evidence_not_suffix(tmp_path: Path) -> None:
    root, adapter, workers = _adapter(tmp_path)
    (root / "pdf.data").write_bytes(b"%PDF-1.7\nsynthetic")
    _container(
        root / "docx.data",
        {"[Content_Types].xml": _docx_content_types(), "word/document.xml": b"<w:document />"},
    )
    _container(
        root / "xlsx.data",
        {"[Content_Types].xml": b"<Types />", "xl/workbook.xml": b"<workbook />"},
    )
    _container(
        root / "pptx.data",
        {"[Content_Types].xml": b"<Types />", "ppt/presentation.xml": b"<p:presentation />"},
    )

    observations = {
        source_format: adapter.observe(_arguments(f"{source_format.lower()}.data"))
        for source_format in ("PDF", "DOCX", "XLSX", "PPTX")
    }

    assert {name: result.backend_name for name, result in observations.items()} == {
        "PDF": "PyMuPDF4LLM",
        "DOCX": "MarkItDown",
        "XLSX": "openpyxl",
        "PPTX": "python-pptx",
    }
    for source_format, observation in observations.items():
        assert observation.status == "COMPLETE"
        assert observation.source_format == source_format
        assert observation.provenance.payload()["source_kind"] == CURRENT_FILESYSTEM_DOCUMENT
        assert observation.items and observation.items[0].location == {"block": 1}
        assert workers[source_format].paths[0].suffix == f".{source_format.lower()}"


def test_unknown_inputs_do_not_reach_any_worker_and_backend_identity_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    root, adapter, workers = _adapter(tmp_path)
    (root / "unknown.docx").write_bytes(b"not an OOXML package")
    _container(root / "unknown.pptx", {"ordinary.txt": b"not a supported package"})

    unknown_bytes = adapter.observe(_arguments("unknown.docx"))
    unknown_zip = adapter.observe(_arguments("unknown.pptx"))

    assert unknown_bytes.status == "UNSUPPORTED_FORMAT"
    assert unknown_zip.status == "UNSUPPORTED_FORMAT"
    assert all(not worker.paths for worker in workers.values())

    (root / "wrong.pdf").write_bytes(b"%PDF-1.7\nsynthetic")
    workers["PDF"].backend_name = "unexpected-backend"
    mismatch = adapter.observe(_arguments("wrong.pdf"))

    assert mismatch.status == "PARSER_FAILED"
    assert mismatch.items == ()


def test_production_dependency_profiles_separate_core_fast_deep_and_dev() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = set(project["project"]["dependencies"])
    extras = {
        name: set(values) for name, values in project["project"]["optional-dependencies"].items()
    }
    fast = {
        "pymupdf4llm==1.28.2",
        "openpyxl==3.1.5",
        "python-pptx==1.0.2",
        "markitdown[docx]==0.1.7",
        "pillow>=11,<13",
    }

    assert dependencies == {"psutil>=6,<8", "typer>=0.16,<0.27"}
    assert fast == extras["document-fast"]
    assert extras["document-deep"] == {"docling==2.119.0"}
    assert {"jsonschema>=4.23,<5", "mcp==1.28.1"} == extras["agent"]
    assert fast | extras["document-deep"] | extras["agent"] <= extras["full"]
    assert (
        extras["full"]
        | {
            "pytest>=8,<10",
            "ruff>=0.9,<1",
            "mypy>=1.14,<2",
            "types-psutil>=7,<8",
        }
        == extras["dev"]
    )


def test_deep_profile_uses_explicit_fast_fallback_when_optional_stack_is_missing(
    tmp_path: Path,
) -> None:
    root, adapter, workers = _adapter(tmp_path)
    source = root / "fallback.pdf"
    source.write_bytes(b"%PDF-1.7\nsynthetic")
    unavailable = _UnavailableWorker()
    adapter.docling_worker = unavailable  # type: ignore[assignment]

    result = adapter.observe({**_arguments(source.name), "parser_profile": "DEEP"})

    assert result.status == "COMPLETE"
    assert result.backend_name == "PyMuPDF4LLM"
    assert result.warnings == ("DEEP_PARSER_UNAVAILABLE_FAST_FALLBACK",)
    assert len(unavailable.paths) == 1
    assert len(workers["PDF"].paths) == 1
