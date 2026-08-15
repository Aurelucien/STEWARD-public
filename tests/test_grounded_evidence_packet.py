"""Isolated acceptance for the unified Grounded Evidence Pack wrapper."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.agent_context import ContextProjectionRequest, build_context_projection
from local_steward.agent_session import create_steward_session
from local_steward.document_observation import DocumentInspectionRequest, inspect_document
from local_steward.document_evidence import build_document_evidence_selection
from local_steward.grounded_evidence import (
    GROUNDED_EVIDENCE_PACKET_SCHEMA_NAME,
    build_document_evidence_packet,
    build_historical_evidence_packet,
)
from local_steward.models import ScanBudget
from local_steward.snapshot_acquisition import SnapshotAcquisitionRequest, acquire_snapshot
from local_steward.native_mcp_server import NativeStewardDispatcher, create_codex_host_policy
from local_steward.native_mcp_server.protocol import DOCUMENT_TOOL, HISTORY_TOOL

from .test_document_inspection_product import _config, _write_pdf
from .test_protocol_completion import prepared_config


@pytest.fixture(autouse=True)
def _allow_task_owned_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())


def test_current_document_packet_is_source_pinned_and_location_grounded(
    tmp_path: Path,
) -> None:
    _config_path, config = _config(tmp_path, pytest.MonkeyPatch())
    source = tmp_path / "documents" / "guide.pdf"
    _write_pdf(source, pages=2)
    page = inspect_document(
        config,
        DocumentInspectionRequest(
            "managed",
            "guide.pdf",
            True,
            content_query="SEARCHABLE pdf",
        ),
    )

    first = build_document_evidence_packet(page)
    second = build_document_evidence_packet(page)
    assert first == second
    assert first["schema_name"] == GROUNDED_EVIDENCE_PACKET_SCHEMA_NAME
    assert first["packet_kind"] == "CURRENT_DOCUMENT"
    assert first["packet_status"] == "READY"
    assert first["source"]["source_kind"] == "CURRENT_FILESYSTEM_DOCUMENT"  # type: ignore[index]
    assert first["verification"]["status"] == "OBSERVATION_COMPLETE"  # type: ignore[index]
    facts = first["facts"]
    assert isinstance(facts, list) and len(facts) == 2
    assert all(item["citation_id"].startswith("citation:document:") for item in facts)
    assert {item["location"]["page"] for item in facts} == {1, 2}  # type: ignore[index]
    assert first["delivery"] == {
        "response_contract": "GROUNDED_EVIDENCE_V1",
        "citation_required": True,
        "required_answer_fields": [
            "source",
            "verification",
            "citation_id",
            "unknowns",
            "omissions",
        ],
        "historical_current_distinction_required": True,
        "interpretation_is_not_evidence": True,
        "unknowns_and_omissions_must_be_preserved": True,
    }
    assert first["packet_digest"]
    assert str(tmp_path) not in json.dumps(first, ensure_ascii=False)

    no_match_page = inspect_document(
        config,
        DocumentInspectionRequest(
            "managed",
            "guide.pdf",
            True,
            content_query="__not_present__",
        ),
    )
    no_match = build_document_evidence_packet(no_match_page)
    assert no_match["packet_status"] == "NO_EVIDENCE"
    assert no_match["facts"] == []
    assert no_match["unknowns"][0]["reason_code"] == "CONTENT_NO_MATCH"  # type: ignore[index]
    assert no_match["delivery"]["citation_required"] is False  # type: ignore[index]


def test_document_packet_can_locate_matches_outside_the_document_item_page(
    tmp_path: Path,
) -> None:
    _config_path, config = _config(tmp_path, pytest.MonkeyPatch())
    _write_pdf(tmp_path / "documents" / "guide.pdf", pages=3)
    page = inspect_document(
        config,
        DocumentInspectionRequest(
            "managed",
            "guide.pdf",
            True,
            limit=1,
            content_query="SEARCHABLE pdf",
            content_limit=3,
        ),
    )
    packet = build_document_evidence_packet(page)
    facts = packet["facts"]
    assert isinstance(facts, list)
    assert [item["location"]["page"] for item in facts] == [1, 2, 3]  # type: ignore[index]
    assert packet["omissions"] == []


def test_pdf_ocr_evidence_is_explicitly_model_derived(tmp_path: Path) -> None:
    _config_path, config = _config(tmp_path, pytest.MonkeyPatch())
    source = tmp_path / "documents" / "guide.pdf"
    _write_pdf(source, pages=1)
    page = inspect_document(
        config,
        DocumentInspectionRequest(
            "managed",
            "guide.pdf",
            True,
            content_query="SEARCHABLE pdf",
            parser_profile="FAST",
            intent="EVIDENCE",
        ),
    )
    source_item = next(
        item
        for item in page.items
        if item.text_or_value is not None
        and "searchable pdf" in item.text_or_value.casefold()
    )
    ocr_item = replace(
        source_item,
        kind="pdf_ocr_page_block",
        extension={"text_source": "LOCAL_OCR", "ocr_engine": "RapidOCR"},
    )
    assert page.source_sha256 is not None
    selection = build_document_evidence_selection(
        (ocr_item,),
        source_sha256=page.source_sha256,
        source_format="PDF",
        query="SEARCHABLE pdf",
        mode="AUTO",
        context_items=2,
        max_characters=12_000,
        limit=20,
        offset=0,
        searchable=True,
    )
    ocr_page = replace(
        page,
        backend_name="STEWARDPageOCR",
        backend_version="3.6.0",
        items=(ocr_item,),
        full_item_count=1,
        returned_count=1,
        evidence_selection=selection,
    )

    packet = build_document_evidence_packet(ocr_page)
    broad_packet = build_document_evidence_packet(
        replace(ocr_page, evidence_selection=None, content_search=None)
    )

    assert packet["facts"][0]["authority"] == "MODEL_DERIVED"  # type: ignore[index]
    assert packet["facts"][0]["text_accuracy"] == "OCR_MODEL_APPROXIMATE"  # type: ignore[index]
    assert packet["delivery"]["model_output_must_not_be_described_as_verbatim"] is True  # type: ignore[index]
    assert broad_packet["facts"][0]["authority"] == "MODEL_DERIVED"  # type: ignore[index]
    assert broad_packet["verification"]["status"] == "MODEL_OBSERVATION_COMPLETE"  # type: ignore[index]
    assert broad_packet["delivery"]["model_output_must_not_be_described_as_verbatim"] is True  # type: ignore[index]


def test_historical_packet_preserves_projection_uncertainty_and_digest(
    tmp_path: Path,
) -> None:
    config = prepared_config(tmp_path)
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "a.txt").write_text("a", encoding="utf-8")
    config = replace(
        config,
        scopes=(replace(config.scopes[0], raw_path=str(scope), normalized_path=scope),),
    )
    acquired = acquire_snapshot(
        config,
        SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True),
    )
    assert acquired.snapshot_id is not None
    projection = build_context_projection(
        config,
        ContextProjectionRequest(
            "GENERAL",
            acquired.snapshot_id,
            limit=1,
        ),
    )
    first = build_historical_evidence_packet(projection, routing={"selected_profile": "GENERAL"})
    second = build_historical_evidence_packet(projection, routing={"selected_profile": "GENERAL"})
    assert first == second
    assert first["packet_kind"] == "HISTORICAL_SNAPSHOT"
    assert first["source"]["source_kind"] == "HISTORICAL_SNAPSHOT"  # type: ignore[index]
    assert first["verification"]["status"] == "VALID"  # type: ignore[index]
    assert first["facts"]
    assert first["routing"] == {"selected_profile": "GENERAL"}
    assert first["packet_digest"]
    assert str(tmp_path) not in json.dumps(first, ensure_ascii=False)


@pytest.mark.anyio
async def test_native_surface_publishes_packets_without_replacing_legacy_results(
    tmp_path: Path,
) -> None:
    config = prepared_config(tmp_path)
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "a.txt").write_text("a", encoding="utf-8")
    config = replace(
        config,
        scopes=(replace(config.scopes[0], raw_path=str(scope), normalized_path=scope),),
    )
    acquired = acquire_snapshot(
        config,
        SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True),
    )
    dispatcher = NativeStewardDispatcher(create_steward_session(config), create_codex_host_policy())
    history = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "ANALYZE_SNAPSHOT",
            "selector": {"policy": "EXACT_ID", "snapshot_id": acquired.snapshot_id},
            "analysis_profile": "GENERAL",
            "question": "Describe the verified history.",
            "limit": 10,
        },
    )
    assert history.isError is False
    result = history.structuredContent["result"]
    assert "context_projection" in result
    assert result["evidence_packet"]["packet_kind"] == "HISTORICAL_SNAPSHOT"

    _write_pdf(scope / "guide.pdf")
    document = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            "query": "guide.pdf",
            "extensions": ["PDF"],
            "content_query": "SEARCHABLE pdf",
        },
    )
    assert document.isError is False
    document_result = document.structuredContent["result"]
    assert "document" in document_result
    assert document_result["evidence_packet"]["packet_kind"] == "CURRENT_DOCUMENT"
