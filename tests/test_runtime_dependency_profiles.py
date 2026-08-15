"""Acceptance for NEXT-010 dependency tiers and lazy runtime loading."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.file_agent.runtime.structured_documents import IsolatedParserWorker
from local_steward.native_mcp_server import NativeStewardDispatcher, create_codex_host_policy
from local_steward.native_mcp_server.protocol import DOCUMENT_TOOL
from local_steward.runtime_capabilities import inspect_runtime_capabilities

from .test_steward_native_agent_surface import _session


ROOT = Path(__file__).resolve().parents[1]
HEAVY_IMPORT_ROOTS = {
    "docling",
    "markitdown",
    "mcp",
    "onnxruntime",
    "openpyxl",
    "PIL",
    "pptx",
    "pymupdf",
    "pymupdf4llm",
    "rapidocr",
    "torch",
    "transformers",
}


def _missing_dependency_worker(_source_path: str) -> dict[str, object]:
    __import__("steward_dependency_that_does_not_exist")
    return {}


def test_core_cli_import_does_not_load_agent_or_document_backends() -> None:
    script = (
        "import json, sys; import local_steward.cli; "
        f"blocked={sorted(HEAVY_IMPORT_ROOTS)!r}; "
        "loaded=sorted(name for name in blocked if name in sys.modules); "
        "print(json.dumps(loaded)); raise SystemExit(bool(loaded))"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == []


def test_runtime_capabilities_are_path_free_deterministic_and_complete_here() -> None:
    first = inspect_runtime_capabilities()
    second = inspect_runtime_capabilities()

    assert first == second
    assert first["report_digest"] == second["report_digest"]
    assert first["profiles"]["core"]["status"] == "AVAILABLE"
    assert first["profiles"]["agent"]["status"] == "AVAILABLE"
    assert first["profiles"]["document-fast"]["status"] == "AVAILABLE"
    assert first["profiles"]["document-deep"]["status"] == "AVAILABLE"
    assert first["profiles"]["full"]["status"] == "AVAILABLE"
    assert first["operations"]["DOCUMENT_DEEP"]["status"] == "AVAILABLE"
    assert first["operations"]["DOCUMENT_ADAPTIVE"]["status"] == "AVAILABLE"
    assert first["schema_version"] == 12
    assert first["formats"]["PDF"]["STRUCTURE_NATIVE"]["status"] == "AVAILABLE"
    assert first["operations"]["DOCUMENT_EVIDENCE"] == {
        "status": "AVAILABLE",
        "selection": "DETERMINISTIC_NATIVE_CONTAINER_HIERARCHY_AWARE",
        "match_repairs": [
            "NFKC_CASEFOLD",
            "SOFT_HYPHEN_REMOVAL",
            "LINEBREAK_DEHYPHENATION",
            "WHITESPACE_NORMALIZATION",
        ],
        "persistence": "NONE",
        "max_targeted_pdf_pages": 8,
        "max_packet_characters": 32768,
    }
    assert first["operations"]["DOCUMENT_NATIVE_FIDELITY"] == {
        "status": "AVAILABLE",
        "projection": "BOUNDED_NATIVE_AUXILIARY_NODES",
        "features": {
            "PDF": ["METADATA", "OUTLINE", "ANNOTATIONS", "FORM_FIELDS", "REPAIR_FACTS"],
            "DOCX": ["COMMENTS", "FOOTNOTES", "ENDNOTES", "REVISIONS"],
            "XLSX": ["COMMENTS", "CHART_SOURCE_REFERENCES", "MERGED_CELL_FACTS"],
            "PPTX": [
                "SPEAKER_NOTES",
                "CHART_CACHED_DATA",
                "ACCESSIBILITY_TEXT",
                "MERGED_CELL_FACTS",
            ],
        },
        "formula_evaluation": False,
        "optional_component_recovery": "FAIL_COMPONENT_PRESERVE_DOCUMENT",
        "persistence": "NONE",
    }
    assert first["document_execution"]["cache"] == {
        "scope": "PROCESS_MEMORY",
        "max_entries": 8,
        "max_bytes": 16 * 1024 * 1024,
        "ttl_seconds": 600.0,
        "single_flight": True,
        "stores_source_bytes": False,
        "persistence_effect": "NONE",
    }
    assert first["operations"]["VIDEO_LOCAL"] == {
        "status": "AVAILABLE",
        "timeline_authority": "SOURCE_PRESENTATION_TIMESTAMPS",
        "source_distinction": [
            "SCENE",
            "REPRESENTATIVE_FRAME",
            "FRAME_OCR",
            "VIDEO_TEXT_TRACK",
            "EMBEDDED_SUBTITLE",
            "AUDIO_ASR",
            "VISUAL_SEMANTIC_RETRIEVAL",
        ],
        "focused_decode_planning": ("WHOLE_SOURCE_TEXT_VISUAL_ANCHOR_OR_EXPLICIT_FALLBACK"),
        "visual_semantic_candidate_authority": ("MODEL_DERIVED_RETRIEVAL_NOT_TRUTH"),
        "semantic_agreement_inferred": False,
        "runtime_downloads_allowed": False,
        "persistence": "NONE",
    }
    assert set(first["formats"]["MKV"]) == {
        "STRUCTURE",
        "READ",
        "LOCATE",
        "EVIDENCE",
        "VIEW",
    }
    resources = first["document_resources"]
    assert resources["source_admission"] == "STREAM_HASH_STAGE"
    assert resources["stores_source_bytes_in_memory"] is False
    assert resources["source_limits"]["PDF"] >= 1024 * 1024 * 1024
    assert resources["source_limits"]["XLSX"] >= 512 * 1024 * 1024
    assert resources["streaming_query_map"]["formats"] == [
        "PDF",
        "EPUB",
        "DOCX",
        "XLSX",
        "PPTX",
    ]
    assert resources["streaming_query_map"]["pdf_page_threshold"] == 128
    assert resources["streaming_query_map"]["pdf_query_intents"] == [
        "LOCATE",
        "EVIDENCE",
    ]
    assert resources["streaming_query_map"]["always_map_formats"] == ["EPUB"]
    assert resources["streaming_query_map"]["query_intents"] == [
        "LOCATE",
        "EVIDENCE",
    ]
    assert resources["epub_native_fallback"] == {
        "backend": "STEWARDNativeEpub",
        "views": ["READ", "STRUCTURE"],
        "tolerant_html": True,
        "spine_order": True,
        "read_text_limit_bytes": 524288,
        "item_limit": 3000,
    }
    assert resources["pdf_native_structure"] == {
        "backend": "PyMuPDFNativeStructure",
        "projection": "OUTLINE_AND_METADATA",
        "page_body_parsed": False,
        "page_auxiliary_scanned": False,
        "item_limit": 512,
    }
    assert resources["pdf_native_auxiliary"] == {
        "page_limit": 512,
        "item_limit": 512,
    }
    assert resources["pdf_page_ocr"] == {
        "backend": "STEWARDPageOCR",
        "engine": "RapidOCR",
        "execution": "PAGE_LOCAL_DISCARD_AFTER_PROJECTION",
        "render_scale": 1.0,
        "render_pixel_limit": 4_000_000,
        "read_trigger": "MAJORITY_LOW_NATIVE_TEXT",
        "query_trigger": "ANY_LOW_NATIVE_TEXT_PAGE",
        "text_authority": "MODEL_DERIVED",
        "persistence": "NONE",
    }
    serialized = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert "/Users/" not in serialized
    assert "site-packages" not in serialized


def test_runtime_capabilities_publish_missing_profiles_without_importing_them() -> None:
    available_imports = {"psutil", "typer", "pymupdf", "pymupdf4llm", "PIL"}
    report = inspect_runtime_capabilities(
        import_probe=lambda name: name in available_imports,
        version_probe=lambda _distribution: "synthetic",
        executable_probe=lambda _name: None,
        platform_name="Linux",
    )

    assert report["profiles"]["core"]["status"] == "AVAILABLE"
    assert report["profiles"]["agent"]["status"] == "UNAVAILABLE"
    assert report["profiles"]["document-fast"]["status"] == "UNAVAILABLE"
    assert report["profiles"]["document-deep"]["status"] == "UNAVAILABLE"
    assert report["formats"]["PDF"]["READ_FAST"]["status"] == "AVAILABLE"
    assert report["formats"]["PDF"]["READ_DEEP"]["status"] == "UNAVAILABLE"
    assert report["formats"]["PDF"]["VIEW"]["status"] == "AVAILABLE"
    assert report["formats"]["DOCX"]["VIEW"]["status"] == "UNAVAILABLE"
    assert report["formats"]["PNG"]["OCR"]["status"] == "UNAVAILABLE"


def test_core_cli_publishes_runtime_capabilities_without_configuration() -> None:
    result = CliRunner().invoke(app, ["--format", "json", "runtime", "status"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "runtime.status"
    assert payload["status"] == "OK"
    report = payload["result"]["runtime_capabilities"]
    assert report["schema_name"] == "local_steward.runtime_capabilities"
    assert report["persistence_effect"] == "NONE"


def test_missing_worker_dependency_is_typed_unavailable(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"bounded")
    result = IsolatedParserWorker(_missing_dependency_worker).run(source)

    assert result.status == "UNAVAILABLE"
    assert result.payload is None


@pytest.mark.anyio
async def test_native_document_capability_action_requires_no_document_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())
    _config, scope, session = _session(tmp_path)
    before = sorted(item.name for item in scope.iterdir())
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    result = await dispatcher.dispatch(DOCUMENT_TOOL, {"action": "CAPABILITIES"})
    invalid = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {"action": "CAPABILITIES", "absolute_path": str(scope / "not-opened.pdf")},
    )

    report = result.structuredContent["result"]["runtime_capabilities"]
    assert result.isError is False
    assert report["schema_name"] == "local_steward.runtime_capabilities"
    assert report["persistence_effect"] == "NONE"
    assert result.structuredContent["selection"] == [
        {"object_kind": "RUNTIME_PROFILE", "policy": "OBSERVED_LOCAL_RUNTIME"}
    ]
    assert invalid.isError is True
    assert invalid.structuredContent["error"]["code"] == "STEWARD_NATIVE_ARGUMENT_INVALID"
    assert sorted(item.name for item in scope.iterdir()) == before
