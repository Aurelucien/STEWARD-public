"""Acceptance for bounded hierarchy-aware current-document evidence."""

from __future__ import annotations

from pathlib import Path

import fitz  # type: ignore[import-untyped]
import pytest

from local_steward.document_evidence import build_document_evidence_selection
from local_steward.document_execution import BoundedDocumentParseCache
from local_steward.document_observation import (
    DOCUMENT_INSPECTION_PROTOCOL_VERSION,
    DocumentInspectionPage,
    DocumentInspectionRequest,
    inspect_document,
)
from local_steward.file_agent.runtime.structured_documents import (
    CURRENT_FILESYSTEM_DOCUMENT,
    DocumentResourceUsage,
    NormalizedDocumentItem,
    StructuredDocumentParserAdapter,
    _WorkerExecution,
)
from local_steward.grounded_evidence import (
    DOCUMENT_EVIDENCE_PACKET_SCHEMA_NAME,
    build_document_evidence_packet,
)
from local_steward.native_mcp_server import NativeStewardDispatcher, create_codex_host_policy
from local_steward.native_mcp_server.protocol import DOCUMENT_INPUT_SCHEMA, DOCUMENT_TOOL

from .test_document_inspection_product import _config
from .test_steward_native_agent_surface import _session


def _item(
    index: int,
    role: str,
    text: str | None,
    *,
    depth: int = 1,
    parent: str = "document:root",
) -> NormalizedDocumentItem:
    return NormalizedDocumentItem(
        f"test_{role.lower()}",
        text,
        parent,
        {"ordinal": index, "depth": depth, "page": 1},
        None,
        f"node:{index}",
        role,
    )


def _hierarchical_items() -> tuple[NormalizedDocumentItem, ...]:
    return (
        NormalizedDocumentItem(
            "document", None, None, {"ordinal": 0, "depth": 0}, None, "document:root", "DOCUMENT"
        ),
        _item(1, "HEADING", "Part One", depth=1),
        _item(2, "PARAGRAPH", "Introduction", depth=2),
        _item(3, "HEADING", "Target Section", depth=2),
        _item(4, "PARAGRAPH", "The target fact is forty two.", depth=3),
        _item(5, "PARAGRAPH", "Supporting context.", depth=3),
        _item(6, "HEADING", "Next Section", depth=2),
        _item(7, "PARAGRAPH", "Unrelated material.", depth=3),
    )


def test_auto_selection_retains_heading_trail_and_stops_at_section_boundary() -> None:
    items = _hierarchical_items()
    first = build_document_evidence_selection(
        items,
        source_sha256="a" * 64,
        query="target fact",
        mode="AUTO",
        context_items=2,
        max_characters=12_000,
        limit=8,
        offset=0,
        searchable=True,
    )
    second = build_document_evidence_selection(
        items,
        source_sha256="a" * 64,
        query="target fact",
        mode="AUTO",
        context_items=2,
        max_characters=12_000,
        limit=8,
        offset=0,
        searchable=True,
    )

    assert first == second
    assert first.status == "COMPLETE"
    assert first.matched_item_count == 1
    evidence_slice = first.slices[0]
    assert evidence_slice.selection_mode == "SECTION"
    assert evidence_slice.heading_trail == (1, 3)
    assert [item.item_index for item in evidence_slice.items] == [1, 3, 4, 5]
    assert evidence_slice.items[2].relation == "ANCHOR"
    assert 6 not in {item.item_index for item in evidence_slice.items}
    assert first.selection_digest


def test_selection_budget_and_continuation_are_explicit() -> None:
    items = tuple(
        _item(index, "PARAGRAPH", f"marker {index} " + "x" * 400)
        for index in range(5)
    )
    result = build_document_evidence_selection(
        items,
        source_sha256="b" * 64,
        query="marker",
        mode="MATCH",
        context_items=0,
        max_characters=512,
        limit=5,
        offset=0,
        searchable=True,
    )

    assert result.selected_character_count <= 512
    assert result.returned_slice_count == 2
    assert result.has_more is True
    assert result.next_offset == 2
    assert result.slices[-1].truncated is True


def test_packet_v3_deduplicates_overlapping_slice_nodes_without_losing_citations() -> None:
    items = tuple(
        _item(index, "PARAGRAPH", f"marker evidence {index}")
        for index in range(5)
    )
    selection = build_document_evidence_selection(
        items,
        source_sha256="c" * 64,
        query="marker",
        mode="WINDOW",
        context_items=1,
        max_characters=12_000,
        limit=5,
        offset=0,
        searchable=True,
    )
    page = DocumentInspectionPage(
        protocol_version=DOCUMENT_INSPECTION_PROTOCOL_VERSION,
        status="COMPLETE",
        source_format="DOCX",
        backend_name="synthetic",
        backend_version="1",
        source_kind=CURRENT_FILESYSTEM_DOCUMENT,
        scope_id="managed",
        relative_path="evidence.docx",
        source_sha256="c" * 64,
        identification_reason=None,
        warnings=(),
        resources=DocumentResourceUsage(None, 0, 0, None, len(items), 0),
        items=items,
        full_item_count=len(items),
        returned_count=len(items),
        limit=100,
        offset=0,
        has_more=False,
        next_offset=None,
        document_observation_digest="d" * 64,
        execution=None,
        evidence_selection=selection,
    )

    packet = build_document_evidence_packet(page)

    citations = {fact["citation_id"] for fact in packet["facts"]}
    assert packet["schema_version"] == 3
    assert packet["delivery"]["response_contract"] == "DOCUMENT_EVIDENCE_V3"
    assert packet["bounds"]["deduplicated_reference_count"] > 0
    assert packet["bounds"]["published_fact_count"] == len(packet["facts"])
    assert len({fact["node_id"] for fact in packet["facts"]}) == len(packet["facts"])
    assert all(
        all(0 <= index < len(packet["facts"]) for index in evidence_slice["fact_indexes"])
        for evidence_slice in packet["slices"]
    )
    assert all(
        packet["facts"][evidence_slice["anchor_fact_index"]]["citation_id"]
        == evidence_slice["anchor_citation_id"]
        for evidence_slice in packet["slices"]
    )
    assert all(evidence_slice["anchor_citation_id"] in citations for evidence_slice in packet["slices"])
    assert all(
        set(fact)
        <= {
            "citation_id",
            "role",
            "node_id",
            "native_location",
            "location",
            "value",
            "value_truncated",
        }
        for fact in packet["facts"]
    )


def _write_evidence_pdf(path: Path) -> None:
    document = fitz.open()
    for page_number, text in enumerate(
        ("Opening material", "Target Section\nThe evidence marker is 8675309", "Appendix"),
        start=1,
    ):
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 90), f"Page {page_number}\n{text}", fontsize=16)
    document.save(path)
    document.close()


def test_pdf_evidence_uses_fast_map_then_targeted_deep_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_path, config = _config(tmp_path, monkeypatch)
    source = tmp_path / "documents" / "evidence.pdf"
    _write_evidence_pdf(source)
    cache = BoundedDocumentParseCache[_WorkerExecution]()
    request = DocumentInspectionRequest(
        "managed",
        "evidence.pdf",
        True,
        content_query="8675309",
        parser_profile="AUTO",
        view="READ",
        intent="EVIDENCE",
        evidence_mode="AUTO",
        evidence_context_items=2,
        evidence_max_characters=4_096,
    )

    page = inspect_document(config, request, parse_cache=cache)

    assert page.status == "COMPLETE"
    assert page.backend_name == "Docling"
    assert page.execution is not None
    assert page.execution.requested_intent == "EVIDENCE"
    assert [attempt.profile for attempt in page.execution.attempts] == ["FAST", "DEEP"]
    assert page.execution.escalation_reason == "EVIDENCE_TARGETED_PAGE_RANGE"
    assert page.execution.selection is not None
    assert page.execution.selection.selected_page_start == 2
    assert page.execution.selection.selected_page_end == 2
    assert page.evidence_selection is not None
    assert page.evidence_selection.status == "COMPLETE"
    packet = build_document_evidence_packet(page)
    assert packet["schema_name"] == DOCUMENT_EVIDENCE_PACKET_SCHEMA_NAME
    assert packet["packet_status"] == "READY"
    assert packet["facts"]
    assert packet["execution"]["selection"]["matched_page_numbers"] == [2]
    assert str(tmp_path) not in str(packet)


def test_no_match_stays_on_fast_map_and_publishes_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_path, config = _config(tmp_path, monkeypatch)
    source = tmp_path / "documents" / "evidence.pdf"
    _write_evidence_pdf(source)
    page = inspect_document(
        config,
        DocumentInspectionRequest(
            "managed",
            "evidence.pdf",
            True,
            content_query="not present anywhere",
            parser_profile="AUTO",
            view="READ",
            intent="EVIDENCE",
        ),
    )

    assert page.backend_name == "PyMuPDF4LLM"
    assert page.execution is not None
    assert [attempt.profile for attempt in page.execution.attempts] == ["FAST"]
    assert page.evidence_selection is not None
    assert page.evidence_selection.status == "NO_MATCH"
    packet = build_document_evidence_packet(page)
    assert packet["packet_status"] == "NO_EVIDENCE"
    assert packet["unknowns"][0]["reason_code"] == "CONTENT_NO_MATCH"


def test_targeted_page_selection_bounds_widely_separated_matches() -> None:
    items = tuple(
        NormalizedDocumentItem(
            "pdf_page_block",
            "marker" if page in {1, 12} else "ordinary",
            None,
            {"page": page, "block": 1},
            None,
            f"page:{page}",
            "SECTION",
        )
        for page in range(1, 13)
    )

    selection = StructuredDocumentParserAdapter._targeted_evidence_selection(items, "marker")

    assert selection.matched_page_numbers == (1, 12)
    assert selection.selected_page_start == 1
    assert selection.selected_page_end == 8
    assert selection.omitted_matched_page_numbers == (12,)


@pytest.mark.anyio
async def test_native_evidence_action_returns_packet_only_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())
    _config_value, scope, session = _session(tmp_path)
    source = scope / "evidence.pdf"
    _write_evidence_pdf(source)
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    result = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            "action": "EVIDENCE",
            "absolute_path": str(source),
            "content_query": "8675309",
            "evidence_mode": "AUTO",
            "evidence_context_items": 2,
            "evidence_max_characters": 4_096,
        },
    )

    assert result.isError is False
    payload = result.structuredContent["result"]
    assert payload["document"]["projection"] == "EVIDENCE_PACKET_ONLY"
    assert payload["document"]["diagnostic_detail"] == "COMPACT"
    assert "diagnostics" not in payload
    assert "items" not in payload["document"]
    assert "execution" not in payload["document"]
    assert payload["evidence_packet"]["schema_name"] == DOCUMENT_EVIDENCE_PACKET_SCHEMA_NAME
    assert payload["evidence_packet"]["delivery"]["response_contract"] == (
        "DOCUMENT_EVIDENCE_V3"
    )
    assert payload["evidence_packet"]["execution"]["projection"] == "EVIDENCE_SUMMARY"
    assert "EVIDENCE" in DOCUMENT_INPUT_SCHEMA["properties"]["action"]["enum"]

    full = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            "action": "EVIDENCE",
            "absolute_path": str(source),
            "content_query": "8675309",
            "evidence_mode": "AUTO",
            "evidence_context_items": 2,
            "evidence_max_characters": 4_096,
            "diagnostic_detail": "FULL",
        },
    )
    assert full.isError is False
    full_payload = full.structuredContent["result"]
    assert full_payload["document"]["diagnostic_detail"] == "FULL"
    assert full_payload["diagnostics"]["resources"]["source_bytes"] > 0
    assert full_payload["evidence_packet"]["facts"] == payload["evidence_packet"]["facts"]
    assert full_payload["evidence_packet"]["selection"] == (
        payload["evidence_packet"]["selection"]
    )
