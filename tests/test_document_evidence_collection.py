"""Acceptance for bounded multi-document evidence over one existing native tool."""

from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from local_steward.models import ScanBudget
from local_steward.native_mcp_server import NativeStewardDispatcher, create_codex_host_policy
from local_steward.native_mcp_server.protocol import (
    DOCUMENT_INPUT_SCHEMA,
    DOCUMENT_TOOL,
    TOOL_NAMES,
)
from local_steward.snapshot_acquisition import SnapshotAcquisitionRequest, acquire_snapshot

from .test_steward_native_agent_surface import _session


@pytest.fixture(autouse=True)
def _allow_task_owned_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())


def _write_docx(path: Path, marker: str, revision: str = "initial") -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>Collection {revision} Evidence {marker}</w:t>"
            "</w:r></w:p><w:sectPr/></w:body></w:document>",
        )


def _collection(result):  # type: ignore[no-untyped-def]
    assert result.isError is False
    return result.structuredContent["result"]["document_evidence_collection"]


@pytest.mark.anyio
async def test_current_collection_returns_two_distinct_grounded_packets(
    tmp_path: Path,
) -> None:
    _config, scope, session = _session(tmp_path)
    _write_docx(scope / "collection-report-a.docx", "shared-marker")
    _write_docx(scope / "collection-report-b.docx", "shared-marker")
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    collection = _collection(
        await dispatcher.dispatch(
            DOCUMENT_TOOL,
            {
                "action": "EVIDENCE_SET",
                "query": "collection-report",
                "content_query": "shared-marker",
                "max_documents": 2,
                "batch_size": 2,
                "per_document_timeout_seconds": 5,
                "evidence_max_characters": 4096,
                "diagnostic_detail": "FULL",
            },
        )
    )

    assert collection["counts"] == {
        "planned": 2,
        "processed": 2,
        "complete": 2,
        "no_evidence": 0,
        "failed": 0,
    }
    assert collection["continuation"] is None
    packets = [item["evidence_packet"] for item in collection["items"]]
    assert all(packet["packet_status"] == "READY" for packet in packets)
    assert packets[0]["packet_digest"] != packets[1]["packet_digest"]
    assert all(
        packet["execution"]["projection"] == "COLLECTION_SUMMARY" for packet in packets
    )
    assert all(
        "container_qualities" not in packet["execution"]["selection"] for packet in packets
    )
    assert all(item["current"]["source_sha256"] for item in collection["items"])
    assert all(
        0 < item["diagnostics"]["resources"]["parser_timeout_limit_ms"] <= 5_000
        for item in collection["items"]
    )
    assert str(tmp_path) not in str(collection)
    assert "EVIDENCE_SET" in DOCUMENT_INPUT_SCHEMA["properties"]["action"]["enum"]
    assert len(TOOL_NAMES) == 5


@pytest.mark.anyio
async def test_collection_continuation_is_stateless_and_request_bound(tmp_path: Path) -> None:
    _config, scope, session = _session(tmp_path)
    _write_docx(scope / "continued-report-a.docx", "continuation-marker")
    _write_docx(scope / "continued-report-b.docx", "continuation-marker")
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())
    arguments = {
        "action": "EVIDENCE_SET",
        "query": "continued-report",
        "content_query": "continuation-marker",
        "max_documents": 2,
        "batch_size": 1,
        "per_document_timeout_seconds": 5,
        "evidence_max_characters": 2048,
    }

    first = _collection(await dispatcher.dispatch(DOCUMENT_TOOL, arguments))
    assert first["schema_version"] == 2
    assert first["diagnostic_detail"] == "COMPACT"
    assert "diagnostics" not in first["items"][0]
    assert "execution" not in first["items"][0]
    assert "resources" not in first["items"][0]
    continuation = first["continuation"]
    assert first["counts"]["processed"] == 1
    assert continuation["next_index"] == 1

    second = _collection(
        await dispatcher.dispatch(
            DOCUMENT_TOOL,
            {**arguments, "collection_continuation": continuation},
        )
    )
    assert second["items"][0]["index"] == 1
    assert second["continuation"] is None
    assert {
        first["items"][0]["current"]["relative_path"],
        second["items"][0]["current"]["relative_path"],
    } == {"continued-report-a.docx", "continued-report-b.docx"}

    rejected = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            **arguments,
            "collection_continuation": {
                **continuation,
                "request_digest": "0" * 64,
            },
        },
    )
    assert rejected.isError is True
    assert rejected.structuredContent["error"]["code"] == "STEWARD_NATIVE_ARGUMENT_INVALID"

    changed_request = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            **arguments,
            "content_limit": 1,
            "collection_continuation": continuation,
        },
    )
    assert changed_request.isError is True
    assert changed_request.structuredContent["error"]["code"] == (
        "STEWARD_NATIVE_ARGUMENT_INVALID"
    )

    changed_diagnostics = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            **arguments,
            "diagnostic_detail": "FULL",
            "collection_continuation": continuation,
        },
    )
    assert changed_diagnostics.isError is True
    assert changed_diagnostics.structuredContent["error"]["code"] == (
        "STEWARD_NATIVE_ARGUMENT_INVALID"
    )


@pytest.mark.anyio
async def test_snapshot_collection_revalidates_current_payload_and_isolates_missing_item(
    tmp_path: Path,
) -> None:
    config, scope, session = _session(tmp_path)
    first_path = scope / "snapshot-report-a.docx"
    second_path = scope / "snapshot-report-b.docx"
    _write_docx(first_path, "snapshot-marker")
    _write_docx(second_path, "snapshot-marker")
    acquired = acquire_snapshot(
        config,
        SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True),
    )
    first_path.unlink()
    _write_docx(second_path, "snapshot-marker", revision="changed")
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    collection = _collection(
        await dispatcher.dispatch(
            DOCUMENT_TOOL,
            {
                "action": "EVIDENCE_SET",
                "query": "snapshot-report",
                "content_query": "snapshot-marker",
                "snapshot_selector": {
                    "policy": "EXACT_ID",
                    "snapshot_id": acquired.snapshot_id,
                    "scope_id": "managed",
                },
                "max_documents": 2,
                "batch_size": 2,
                "per_document_timeout_seconds": 5,
            },
        )
    )

    assert collection["plan"]["source_kind"] == "VERIFIED_HISTORICAL_SNAPSHOT"
    assert collection["counts"]["failed"] == 1
    assert collection["counts"]["complete"] == 1
    failed = next(item for item in collection["items"] if item["status"] == "FAILED")
    changed = next(item for item in collection["items"] if item["status"] == "COMPLETE")
    assert failed["reason_code"] in {
        "STEWARD_PATH_RESOLUTION_INVALID",
        "CURRENT_DOCUMENT_ADMISSION_FAILED",
    }
    assert failed["evidence_packet"] is None
    assert changed["current"]["historical_metadata_relation"] == "METADATA_CHANGED"
    assert changed["current"]["historical_payload_relation"] == "UNKNOWN"
    assert changed["current"]["source_sha256"]
    assert changed["historical"]["snapshot_id"] == acquired.snapshot_id


@pytest.mark.anyio
async def test_collection_requires_both_discovery_and_content_queries(tmp_path: Path) -> None:
    _config, _scope, session = _session(tmp_path)
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    result = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {"action": "EVIDENCE_SET", "query": "report"},
    )

    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "STEWARD_NATIVE_ARGUMENT_INVALID"


@pytest.mark.anyio
async def test_current_continuation_rejects_candidate_set_drift(tmp_path: Path) -> None:
    _config, scope, session = _session(tmp_path)
    _write_docx(scope / "drift-report-a.docx", "drift-marker")
    second_path = scope / "drift-report-b.docx"
    _write_docx(second_path, "drift-marker")
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())
    arguments = {
        "action": "EVIDENCE_SET",
        "query": "drift-report",
        "content_query": "drift-marker",
        "max_documents": 2,
        "batch_size": 1,
        "per_document_timeout_seconds": 5,
    }
    first = _collection(await dispatcher.dispatch(DOCUMENT_TOOL, arguments))
    _write_docx(second_path, "drift-marker", revision="materially-longer-current-revision")

    rejected = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {**arguments, "collection_continuation": first["continuation"]},
    )

    assert rejected.isError is True
    assert rejected.structuredContent["error"]["code"] == "STEWARD_NATIVE_ARGUMENT_INVALID"


@pytest.mark.anyio
async def test_parser_failure_and_unexpected_item_error_do_not_suppress_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, scope, session = _session(tmp_path)
    (scope / "failure-report-a.docx").write_bytes(b"not-an-office-package")
    _write_docx(scope / "failure-report-b.docx", "failure-marker")
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    malformed = _collection(
        await dispatcher.dispatch(
            DOCUMENT_TOOL,
            {
                "action": "EVIDENCE_SET",
                "query": "failure-report",
                "content_query": "failure-marker",
                "max_documents": 2,
                "batch_size": 2,
                "per_document_timeout_seconds": 5,
            },
        )
    )
    assert malformed["counts"]["failed"] == 1
    assert malformed["counts"]["complete"] == 1

    original = dispatcher._bridge.inspect_document

    def injected_failure(scope_id: str, relative_path: str, **kwargs):  # type: ignore[no-untyped-def]
        if relative_path.endswith("a.docx"):
            raise RuntimeError("injected item-local parser failure")
        return original(scope_id, relative_path, **kwargs)

    monkeypatch.setattr(dispatcher._bridge, "inspect_document", injected_failure)
    unexpected = _collection(
        await dispatcher.dispatch(
            DOCUMENT_TOOL,
            {
                "action": "EVIDENCE_SET",
                "query": "failure-report",
                "content_query": "failure-marker",
                "max_documents": 2,
                "batch_size": 2,
                "per_document_timeout_seconds": 5,
            },
        )
    )
    assert unexpected["counts"]["failed"] == 1
    assert unexpected["counts"]["complete"] == 1
    failed = next(item for item in unexpected["items"] if item["status"] == "FAILED")
    assert failed["reason_code"] == "DOCUMENT_COLLECTION_ITEM_FAILED"
    assert "injected" not in str(unexpected)
