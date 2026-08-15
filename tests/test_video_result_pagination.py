"""Regression coverage for cached, modality-fair long-video result pagination."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from local_steward.file_agent.runtime import video_documents
from local_steward.file_agent.runtime.structured_documents import (
    IsolatedParserWorker,
    _WorkerExecution,
)
from local_steward.file_agent.runtime.video_documents import VideoTimelineWorker
from local_steward.native_mcp_server import NativeStewardDispatcher, create_codex_host_policy
from local_steward.native_mcp_server.host_paths import admit_host_absolute_file
from local_steward.native_mcp_server.protocol import DOCUMENT_TOOL

from .test_steward_native_agent_surface import _session


def _item(
    kind: str,
    ordinal: int,
    *,
    source_kind: str,
) -> dict[str, object]:
    start_ms = ordinal * 1_000
    return {
        "kind": kind,
        "node_id": f"{kind}:{ordinal:04d}",
        "role": "PARAGRAPH",
        "text_or_value": f"{kind} {ordinal}",
        "parent": "video:timeline",
        "location": {"start_ms": start_ms, "end_ms": start_ms + 900},
        "extension": {
            "source_kind": source_kind,
            "model_derived": source_kind != "EMBEDDED_SUBTITLE",
            "timestamp_accuracy": "MODEL_APPROXIMATE",
        },
    }


def _video_payload(target: VideoTimelineWorker) -> dict[str, Any]:
    items: list[dict[str, object]] = [
        {
            "kind": "video_document",
            "node_id": "video:timeline",
            "role": "DOCUMENT",
            "text_or_value": None,
            "parent": None,
            "location": {"start_ms": 0, "end_ms": 180_000},
            "extension": {"timing_authority": "SOURCE_PRESENTATION_TIMESTAMPS"},
        },
        {
            "kind": "video_analysis_summary",
            "node_id": "video:analysis-summary:0-180000",
            "role": "METADATA",
            "text_or_value": "bounded coverage",
            "parent": "video:timeline",
            "location": {"start_ms": 0, "end_ms": 180_000},
            "extension": {
                "source_kind": "VIDEO_ANALYSIS_SUMMARY",
                "coverage_report": {"schema_version": 1},
            },
        },
    ]
    items.extend(
        _item("audio_transcript_segment", ordinal, source_kind="AUDIO_ASR")
        for ordinal in range(1, 121)
    )
    items.extend(
        _item(
            "video_scene" if ordinal % 2 else "video_representative_frame",
            ordinal,
            source_kind="VIDEO_REPRESENTATIVE_FRAME",
        )
        for ordinal in range(1, 21)
    )
    items.extend(
        _item("video_frame_ocr_text", ordinal, source_kind="FRAME_OCR")
        for ordinal in range(1, 21)
    )
    items.extend(
        _item("video_embedded_subtitle_cue", ordinal, source_kind="EMBEDDED_SUBTITLE")
        for ordinal in range(1, 11)
    )
    items.extend(
        _item("video_visual_semantic_anchor", ordinal, source_kind="VISUAL_SEMANTIC_RETRIEVAL")
        for ordinal in range(1, 9)
    )
    assert len(items) == 180
    return {
        "backend_name": "FFmpegMultimodal",
        "backend_version": "test",
        "warnings": [],
        "items": items,
        "resource_extension": {
            "media_kind": "VIDEO",
            "duration_ms": 180_000,
            "window_start_ms": target.start_ms,
            "window_end_ms": 180_000,
            "video_request_digest": target.request_digest,
            "video_analysis": "MULTIMODAL_AND_OCR",
            "coverage_report": {"schema_version": 1},
            "persistence_effect": "NONE",
        },
        "continuation": None,
    }


@pytest.mark.anyio
async def test_video_read_pages_all_modalities_without_rerunning_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())
    _config, scope, session = _session(tmp_path)
    source = scope / "long.mp4"
    source.write_bytes(b"not decoded by this isolated regression")
    before = (
        source.stat().st_size,
        source.stat().st_mtime_ns,
        sha256(source.read_bytes()).hexdigest(),
    )
    calls: list[int] = []

    monkeypatch.setattr(
        "local_steward.file_agent.runtime.structured_documents.video_runtime_capabilities",
        lambda: {"probe_ready": True, "decode_ready": True},
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.structured_documents.resolve_local_audio_model",
        lambda: (tmp_path, "revision", "a" * 64),
    )

    def fake_run(worker: IsolatedParserWorker, _source_path: Path) -> _WorkerExecution:
        assert isinstance(worker.worker_target, VideoTimelineWorker)
        calls.append(worker.worker_target.start_ms)
        return _WorkerExecution("COMPLETE", _video_payload(worker.worker_target), 9_000, 99_000)

    monkeypatch.setattr(IsolatedParserWorker, "run", fake_run)
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    first = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            "action": "READ",
            "absolute_path": str(source),
            "video_analysis": "MULTIMODAL_AND_OCR",
            "limit": 100,
        },
    )
    first_page = first.structuredContent["result"]["document"]
    continuation = first_page["continuation"]

    assert first.isError is False
    assert first_page["full_item_count"] == 180
    assert first_page["returned_count"] == 100
    assert first_page["has_more"] is True
    assert first_page["next_offset"] == 100
    assert continuation["schema_version"] == 2
    assert continuation["kind"] == "RESULT_PAGE"
    assert continuation["next_offset"] == 100
    assert continuation["limit"] == 100
    assert first_page["execution"]["attempts"][0]["cache_status"] == "MISS"
    first_kinds = {item["kind"] for item in first_page["items"]}
    assert {
        "video_analysis_summary",
        "video_scene",
        "video_representative_frame",
        "video_frame_ocr_text",
        "video_embedded_subtitle_cue",
        "video_visual_semantic_anchor",
        "audio_transcript_segment",
    } <= first_kinds
    omissions = first.structuredContent["result"]["evidence_packet"]["omissions"]
    assert any(item["reason_code"] == "VIDEO_RESULT_PAGE_LIMIT" for item in omissions)

    second = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            "action": "READ",
            "absolute_path": str(source),
            "video_analysis": "MULTIMODAL_AND_OCR",
            "video_continuation": continuation,
        },
    )
    second_page = second.structuredContent["result"]["document"]

    assert second.isError is False
    assert second_page["offset"] == 100
    assert second_page["returned_count"] == 80
    assert second_page["has_more"] is False
    assert second_page["continuation"] is None
    assert second_page["execution"]["attempts"][0]["cache_status"] == "HIT"
    assert second_page["resources"]["parser_elapsed_ms"] == 0
    first_ids = {item["node_id"] for item in first_page["items"]}
    second_ids = {item["node_id"] for item in second_page["items"]}
    assert len(first_ids | second_ids) == 180
    assert first_ids.isdisjoint(second_ids)
    assert calls == [0]
    assert (
        source.stat().st_size,
        source.stat().st_mtime_ns,
        sha256(source.read_bytes()).hexdigest(),
    ) == before

    expired = await NativeStewardDispatcher(
        session, create_codex_host_policy()
    ).dispatch(
        DOCUMENT_TOOL,
        {
            "action": "READ",
            "absolute_path": str(source),
            "video_analysis": "MULTIMODAL_AND_OCR",
            "video_continuation": continuation,
        },
    )
    assert expired.isError is True
    assert calls == [0]


def test_video_coverage_report_exposes_sampling_gaps_and_temporal_links() -> None:
    items = [
        {
            "kind": "video_scene",
            "node_id": "scene:1",
            "location": {"start_ms": 0, "end_ms": 10_000},
        },
        {
            "kind": "video_representative_frame",
            "node_id": "frame:1",
            "location": {"timestamp_ms": 5_000},
        },
        {
            "kind": "video_frame_ocr_text",
            "node_id": "ocr:1",
            "location": {"timestamp_ms": 5_000},
        },
        {
            "kind": "audio_transcript_segment",
            "node_id": "asr:1",
            "location": {"start_ms": 4_000, "end_ms": 6_000},
            "extension": {"timestamp_accuracy": "MODEL_APPROXIMATE"},
        },
        {
            "kind": "video_embedded_subtitle_cue",
            "node_id": "subtitle:1",
            "location": {"start_ms": 4_500, "end_ms": 5_500},
        },
    ]

    report = video_documents._video_coverage_report(
        items,
        window_start_ms=0,
        window_end_ms=20_000,
        detected_scene_count=2,
        selected_frame_count=1,
        ocr_frame_count=1,
        include_ocr=True,
    )

    assert report["scene"]["selection_complete"] is False
    assert report["scene"]["unselected_count"] == 1
    assert report["representative_frames"]["timestamps_ms"] == [5_000]
    assert report["representative_frames"]["all_other_source_times_visually_unobserved"] is True
    assert report["ocr"]["coverage"] == "SELECTED_REPRESENTATIVE_FRAMES_ONLY"
    assert report["audio_asr"]["word_exact_alignment_claimed"] is False
    group = report["temporal_browse_groups"][0]
    assert group["representative_frame_node_ids"] == ["frame:1"]
    assert group["ocr_node_ids"] == ["ocr:1"]
    assert group["audio_asr_node_ids"] == ["asr:1"]
    assert group["embedded_subtitle_node_ids"] == ["subtitle:1"]

    capabilities = video_documents.video_runtime_capabilities()
    assert capabilities["result_pagination"]["result_page_schema_version"] == 2
    assert capabilities["result_pagination"]["reruns_analysis"] is False
    assert (
        capabilities["result_presentation"]["policy_id"]
        == "WEIGHTED_MODALITY_ROUND_ROBIN_V1"
    )


def test_macos_tmp_alias_admits_video_before_exact_file_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())
    _config, _scope, session = _session(tmp_path)
    with TemporaryDirectory(prefix="steward-video-alias-", dir="/tmp") as directory:
        canonical_directory = Path(directory).resolve()
        source = canonical_directory / "alias.mp4"
        source.write_bytes(b"video")
        alias = Path("/tmp") / canonical_directory.name / source.name

        binding = admit_host_absolute_file(session, str(alias))

        assert binding.relative_path == source.name
        assert binding.config.scopes[0].normalized_path == canonical_directory
