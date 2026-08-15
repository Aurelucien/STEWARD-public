"""Focused coverage for natural Context and document entrypoint routing."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.agent_session import create_steward_session
from local_steward.native_mcp_server import NativeStewardDispatcher, create_codex_host_policy
from local_steward.native_mcp_server.protocol import DOCUMENT_TOOL, HISTORY_TOOL
from local_steward.models import ScanBudget
from local_steward.snapshot_acquisition import SnapshotAcquisitionRequest, acquire_snapshot

from .test_document_inspection_product import _write_pdf
from .test_protocol_completion import prepared_config


@pytest.fixture(autouse=True)
def _allow_task_owned_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())


def _session(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = prepared_config(tmp_path)
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "a.txt").write_text("a", encoding="utf-8")
    config = replace(
        config,
        scopes=(replace(config.scopes[0], raw_path=str(scope), normalized_path=scope),),
    )
    return config, scope, create_steward_session(config)


@pytest.mark.anyio
async def test_auto_context_routing_selects_structure_and_change_base(tmp_path: Path) -> None:
    config, scope, session = _session(tmp_path)
    first = acquire_snapshot(
        config, SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True)
    )
    (scope / "b.txt").write_text("b", encoding="utf-8")
    second = acquire_snapshot(
        config, SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True)
    )
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    structure = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "ANALYZE_SNAPSHOT",
            "selector": {"policy": "EXACT_ID", "snapshot_id": second.snapshot_id},
            "question": "请告诉我目录结构",
            "analysis_profile": "AUTO",
            "limit": 10,
        },
    )
    assert structure.isError is False
    structure_projection = structure.structuredContent["result"]["context_projection"]
    assert structure_projection["projection_kind"] == "STRUCTURE_OVERVIEW"
    assert structure.structuredContent["result"]["routing"] == {
        "requested_profile": "AUTO",
        "selected_profile": "STRUCTURE_OVERVIEW",
        "reason_code": "QUESTION_STRUCTURE",
        "matched_terms": ["目录", "结构"],
    }

    change = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "ANALYZE_SNAPSHOT",
            "selector": {"policy": "EXACT_ID", "snapshot_id": second.snapshot_id},
            "question": "what changed since the previous Snapshot?",
            "analysis_profile": "AUTO",
            "limit": 10,
        },
    )
    assert change.isError is False
    change_projection = change.structuredContent["result"]["context_projection"]
    assert change_projection["projection_kind"] == "CHANGE_TRIAGE"
    assert change.structuredContent["result"]["routing"]["reason_code"] == "QUESTION_CHANGE"
    assert [item["snapshot_id"] for item in change.structuredContent["selection"]] == [
        first.snapshot_id,
        second.snapshot_id,
    ]


@pytest.mark.anyio
async def test_document_query_parses_unique_and_returns_candidates_when_ambiguous(
    tmp_path: Path,
) -> None:
    _config, scope, session = _session(tmp_path)
    _write_pdf(scope / "named-guide.pdf")
    _write_pdf(scope / "other-guide.pdf")
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    unique = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            "query": "named-guide.pdf",
            "extensions": ["PDF"],
            "limit": 10,
            "content_query": "SEARCHABLE pdf",
        },
    )
    assert unique.isError is False
    assert unique.structuredContent["selection"][0]["input_kind"] == "DOCUMENT_QUERY"
    assert unique.structuredContent["selection"][0]["relative_path"] == "named-guide.pdf"
    assert unique.structuredContent["result"]["document"]["source_format"] == "PDF"
    assert unique.structuredContent["result"]["document_search"]["matched_count"] == 1
    content_search = unique.structuredContent["result"]["document"]["content_search"]
    assert content_search["query"] == "SEARCHABLE pdf"
    assert content_search["match_mode"] == "SUBSTRING_CASEFOLD_NFKC"
    assert content_search["status"] == "COMPLETE"
    assert content_search["matched_item_count"] == 1
    assert content_search["matched_occurrence_count"] == 1
    assert content_search["returned_count"] == 1
    assert content_search["has_more"] is False
    assert content_search["matches"][0]["kind"] == "pdf_page_block"
    assert content_search["matches"][0]["location"] == {"block": 1, "page": 1}
    assert "searchable PDF" in content_search["matches"][0]["excerpt"]
    assert str(scope) not in str(unique.structuredContent)

    ambiguous = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            "query": "guide",
            "extensions": [".pdf"],
            "limit": 10,
            "content_query": "SEARCHABLE pdf",
        },
    )
    assert ambiguous.isError is False
    result = ambiguous.structuredContent["result"]
    assert "document" not in result
    assert {item["relative_path"] for item in result["document_search"]["candidates"]} == {
        "named-guide.pdf",
        "other-guide.pdf",
    }
    assert result["document_search"]["content_search"] == {
        "status": "NOT_RUN_AMBIGUOUS",
        "reason_code": "DOCUMENT_SELECTION_NOT_UNIQUE",
    }
