"""NEXT-027 adaptive coverage and optional visual-semantic retrieval contracts."""

from __future__ import annotations

from pathlib import Path

from local_steward.file_agent.runtime import video_documents, video_semantics
from local_steward.file_agent.runtime.video_documents import VideoTimelineWorker
from local_steward.file_agent.runtime.video_semantics import VideoSemanticModel


def _probe(*, duration_ms: int) -> dict[str, object]:
    return {
        "container": "mp4",
        "duration_ms": duration_ms,
        "container_start_ms": 0,
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "start_ms": 0,
                "end_ms": duration_ms,
                "language": None,
                "title": None,
                "default": True,
                "forced": False,
                "attached_picture": False,
                "time_base": "1/1000",
                "start_pts": 0,
                "duration_ts": duration_ms,
                "width": 320,
                "height": 180,
                "pixel_format": "yuv420p",
                "average_frame_rate": "1/1",
                "real_frame_rate": "1/1",
                "frame_rate_mode": "CONSTANT",
                "sample_aspect_ratio": "1/1",
                "display_aspect_ratio": "16/9",
                "field_order": "progressive",
                "color_range": None,
                "color_space": None,
                "color_transfer": None,
                "color_primaries": None,
                "rotation_degrees": None,
            }
        ],
        "chapters": [],
        "primary_video_stream_index": 0,
        "primary_audio_stream_index": None,
        "primary_subtitle_stream_index": None,
        "track_selection_policy_id": "DEFAULT_DISPOSITION_THEN_LOWEST_INDEX_V1",
    }


def test_scene_coverage_spans_the_detected_timeline() -> None:
    scenes = [
        {"ordinal": ordinal, "start_ms": ordinal * 1_000, "end_ms": (ordinal + 1) * 1_000}
        for ordinal in range(30)
    ]

    selected = video_documents._select_scene_coverage(scenes, limit=6)

    assert [scene["ordinal"] for scene in selected] == [0, 6, 12, 17, 23, 29]


def test_semantic_runtime_never_downloads_a_missing_model(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setenv("STEWARD_VIDEO_MODEL_HOME", str(tmp_path))  # type: ignore[attr-defined]
    video_semantics.resolve_local_video_semantic_model.cache_clear()

    capabilities = video_semantics.video_semantic_runtime_capabilities()

    assert capabilities["ready"] is False
    assert capabilities["runtime_downloads_allowed"] is False
    assert list(tmp_path.iterdir()) == []
    video_semantics.resolve_local_video_semantic_model.cache_clear()


def test_whole_source_visual_scan_refines_and_selects_late_candidates(
    tmp_path: Path, monkeypatch: object
) -> None:
    model = VideoSemanticModel(tmp_path, "revision", "a" * 64)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents,
        "video_semantic_runtime_capabilities",
        lambda: {
            "ready": True,
            "model_id": "test/clip",
            "model_revision": "revision",
            "model_identity_sha256": "a" * 64,
        },
    )

    def extract(
        _source: str,
        target: str,
        *,
        stream_index: int,
        timestamp_ms: int,
    ) -> None:
        del stream_index, timestamp_ms
        Path(target).write_bytes(b"frame")

    monkeypatch.setattr(video_documents, "_extract_visual_scan_frame", extract)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents,
        "_frame_average_hash",
        lambda path: int(Path(path).stem.rsplit("-", 1)[1]),
    )
    monkeypatch.setattr(video_documents, "_hamming_distance", lambda _left, _right: 64)  # type: ignore[attr-defined]

    def rank(
        *, query: str, frames: list[tuple[int, str]]
    ) -> tuple[list[dict[str, object]], VideoSemanticModel]:
        del query
        return (
            sorted(
                (
                    {
                        "timestamp_ms": timestamp_ms,
                        "similarity": 1 - abs(timestamp_ms - 900_000) / 1_200_000,
                    }
                    for timestamp_ms, _path in frames
                ),
                key=lambda item: (-float(item["similarity"]), int(item["timestamp_ms"])),
            ),
            model,
        )

    monkeypatch.setattr(video_documents, "rank_video_frames", rank)  # type: ignore[attr-defined]

    anchors, resources, warnings = video_documents._visual_semantic_anchors(
        "unused.mp4",
        stream_index=0,
        duration_ms=1_200_000,
        query="a red vehicle",
    )

    assert warnings == []
    assert resources["status"] == "COMPLETE"
    assert resources["anchor_count"] == 4
    first_location = anchors[0]["location"]
    assert isinstance(first_location, dict)
    assert 880_000 <= int(first_location["timestamp_ms"]) <= 920_000
    assert anchors[0]["extension"]["retrieval_candidate_not_truth"] is True  # type: ignore[index]


def test_visual_scan_discards_candidates_far_below_the_best_score(
    tmp_path: Path, monkeypatch: object
) -> None:
    model = VideoSemanticModel(tmp_path, "revision", "a" * 64)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents,
        "video_semantic_runtime_capabilities",
        lambda: {
            "ready": True,
            "model_id": "test/clip",
            "model_revision": "revision",
            "model_identity_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents,
        "_visual_scan_timestamps",
        lambda _duration, *, limit: [100_000, 900_000],
    )

    def extract(
        _source: str,
        target: str,
        *,
        stream_index: int,
        timestamp_ms: int,
    ) -> None:
        del stream_index
        Path(target).write_text(str(timestamp_ms), encoding="utf-8")

    monkeypatch.setattr(video_documents, "_extract_visual_scan_frame", extract)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents,
        "_frame_average_hash",
        lambda path: int(Path(path).read_text(encoding="utf-8")),
    )
    monkeypatch.setattr(video_documents, "_hamming_distance", lambda _left, _right: 64)  # type: ignore[attr-defined]

    def rank(
        *, query: str, frames: list[tuple[int, str]]
    ) -> tuple[list[dict[str, object]], VideoSemanticModel]:
        del query
        scores = {100_000: 0.21, 900_000: 0.34}
        ranked = [
            {"timestamp_ms": timestamp_ms, "similarity": scores.get(timestamp_ms, 0.20)}
            for timestamp_ms, _path in frames
        ]
        ranked.sort(key=lambda item: -float(item["similarity"]))
        return ranked, model

    monkeypatch.setattr(video_documents, "rank_video_frames", rank)  # type: ignore[attr-defined]

    anchors, resources, warnings = video_documents._visual_semantic_anchors(
        "unused.mp4",
        stream_index=0,
        duration_ms=1_000_000,
        query="late target",
    )

    assert warnings == []
    assert [anchor["location"]["timestamp_ms"] for anchor in anchors] == [900_000]  # type: ignore[index]
    assert resources["relative_score_margin"] == 0.08


def test_timeline_uses_whole_source_visual_anchor_without_claiming_agreement(
    monkeypatch: object,
) -> None:
    duration_ms = 1_200_000
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents, "probe_video", lambda _path, _format: _probe(duration_ms=duration_ms)
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents, "extract_embedded_subtitles", lambda *args, **kwargs: ([], [])
    )
    anchor = {
        "kind": "video_visual_semantic_anchor",
        "node_id": "video:visual-anchor:1",
        "role": "FIGURE",
        "text_or_value": "a red vehicle",
        "parent": "video:timeline",
        "location": {"timestamp_ms": 900_000, "stream_index": 0, "ordinal": 1},
        "extension": {
            "source_kind": "VISUAL_SEMANTIC_RETRIEVAL",
            "retrieval_candidate_not_truth": True,
        },
    }
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents,
        "_visual_semantic_anchors",
        lambda *args, **kwargs: ([anchor], {"status": "COMPLETE"}, []),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents,
        "ffmpeg_runtime_version",
        lambda: "test",
    )

    def scene_result(_worker: object, _path: str) -> dict[str, object]:
        return {
            "warnings": [],
            "items": [],
            "resource_extension": {
                "scene_count": 0,
                "decoded_frame_count": 0,
                "candidate_frame_count": 0,
                "selected_frame_count": 0,
                "ocr_frame_count": 0,
                "ocr_item_count": 0,
                "ocr_track_count": 0,
            },
        }

    monkeypatch.setattr(video_documents.VideoSceneWorker, "__call__", scene_result)  # type: ignore[attr-defined]

    result = VideoTimelineWorker(
        "MP4",
        0,
        False,
        "request",
        "b" * 64,
        "a red vehicle",
    )("unused.mp4")

    resources = result["resource_extension"]
    assert resources["decode_plan"]["reason"] == "QUERY_MULTIMODAL_ANCHOR"
    assert resources["decode_plan"]["windows"] == [
        {"start_ms": 895_000, "end_ms": 905_000}
    ]
    assert resources["decode_plan"]["avoided_ms"] == 1_190_000
    assert resources["modalities"]["VISUAL_SEMANTIC_RETRIEVAL"] is True
    assert resources["semantic_agreement_inferred"] is False


def test_crossmodal_window_retains_visual_audio_subtitle_and_ocr_authority(
    monkeypatch: object,
) -> None:
    duration_ms = 1_200_000
    probe = _probe(duration_ms=duration_ms)
    streams = probe["streams"]
    assert isinstance(streams, list)
    streams.append(
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "start_ms": 0,
            "end_ms": duration_ms,
        }
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents, "probe_video", lambda _path, _format: probe
    )
    subtitle = {
        "kind": "video_embedded_subtitle_cue",
        "node_id": "video:subtitle:2:1",
        "role": "PARAGRAPH",
        "text_or_value": "unrelated subtitle",
        "parent": "video:timeline",
        "location": {"start_ms": 898_000, "end_ms": 899_000, "stream_index": 2},
        "extension": {"source_kind": "EMBEDDED_SUBTITLE", "model_derived": False},
    }
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents,
        "extract_embedded_subtitles",
        lambda *args, **kwargs: ([subtitle], []),
    )
    visual = {
        "kind": "video_visual_semantic_anchor",
        "node_id": "video:visual-anchor:1",
        "role": "FIGURE",
        "text_or_value": "red vehicle",
        "parent": "video:timeline",
        "location": {"timestamp_ms": 900_000, "stream_index": 0, "ordinal": 1},
        "extension": {
            "source_kind": "VISUAL_SEMANTIC_RETRIEVAL",
            "model_derived": True,
            "retrieval_candidate_not_truth": True,
        },
    }
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents,
        "_visual_semantic_anchors",
        lambda *args, **kwargs: ([visual], {"status": "COMPLETE"}, []),
    )
    asr = {
        "kind": "audio_transcript_segment",
        "node_id": "audio:segment:1",
        "role": "PARAGRAPH",
        "text_or_value": "red vehicle spoken",
        "parent": "video:timeline",
        "location": {"start_ms": 899_000, "end_ms": 901_000, "ordinal": 1},
        "extension": {"source_kind": "AUDIO_ASR", "model_derived": True},
    }
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents,
        "transcribe_media_window",
        lambda *args, **kwargs: ([asr], {"base_transcript_digest": "d" * 64}),
    )
    ocr = {
        "kind": "video_frame_ocr_text",
        "node_id": "video:scene:window-1:1:ocr:1",
        "role": "PARAGRAPH",
        "text_or_value": "RED VEHICLE",
        "parent": "video:scene:window-1:1:frame",
        "location": {"timestamp_ms": 900_500, "stream_index": 0, "ordinal": 1},
        "extension": {"source_kind": "FRAME_OCR", "model_derived": True},
    }

    def scene_result(_worker: object, _path: str) -> dict[str, object]:
        return {
            "warnings": [],
            "items": [ocr],
            "resource_extension": {
                "scene_count": 1,
                "decoded_frame_count": 3,
                "candidate_frame_count": 3,
                "selected_frame_count": 1,
                "ocr_frame_count": 1,
                "ocr_item_count": 1,
                "ocr_track_count": 0,
            },
        }

    monkeypatch.setattr(video_documents.VideoSceneWorker, "__call__", scene_result)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        video_documents, "ffmpeg_runtime_version", lambda: "test"
    )

    result = VideoTimelineWorker(
        "MP4",
        0,
        True,
        "request",
        "b" * 64,
        "red vehicle",
    )("unused.mp4")

    resources = result["resource_extension"]
    assert resources["decode_plan"]["windows"] == [
        {"start_ms": 894_000, "end_ms": 906_000}
    ]
    assert resources["decode_plan"]["reason"] == "QUERY_MULTIMODAL_ANCHOR"
    assert resources["semantic_agreement_inferred"] is False
    kinds = {
        item.get("extension", {}).get("source_kind")
        for item in result["items"]
        if isinstance(item.get("extension"), dict)
    }
    assert {
        "VISUAL_SEMANTIC_RETRIEVAL",
        "AUDIO_ASR",
        "EMBEDDED_SUBTITLE",
        "FRAME_OCR",
    } <= kinds
    query_windows = [
        item for item in result["items"] if item.get("kind") == "video_query_window"
    ]
    assert query_windows
    assert all(
        item["extension"]["semantic_agreement_inferred"] is False
        for item in query_windows
    )
