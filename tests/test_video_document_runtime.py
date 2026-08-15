"""NEXT-024B video-container admission and unified source-time coverage."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from local_steward.document_discovery import normalize_document_extensions
from local_steward.file_agent.runtime.scope_binding import ScopeBinding, ScopeBindings
from local_steward.file_agent.runtime.structured_documents import (
    CURRENT_FILESYSTEM_VIDEO,
    ProjectOwnedBoundedDocumentIngress,
    StructuredDocumentParserAdapter,
    identify_document_format,
)
from local_steward.file_agent.runtime.video_documents import (
    VIDEO_SCENE_POLICY_ID,
    VideoProbeWorker,
    VideoSceneWorker,
    VideoTimelineWorker,
    detect_video_scenes,
    extract_embedded_subtitles,
    probe_video,
    video_request_digest,
    video_runtime_capabilities,
)
from local_steward.file_agent.runtime import video_documents
from local_steward.file_agent.runtime.visual_documents import (
    DocumentVisualRequest,
    VisualDocumentAdapter,
)
from local_steward.native_mcp_server.protocol import DOCUMENT_INPUT_SCHEMA


def _ffmpeg_fixture(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=2",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(path),
        ],
        check=True,
    )


def _three_scene_fixture(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x180:r=10:d=1",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "mpeg4",
            "-y",
            str(path),
        ],
        check=True,
    )


def _black_transition_fixture(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=10:d=1",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "mpeg4",
            "-y",
            str(path),
        ],
        check=True,
    )


def _multimodal_fixture(path: Path) -> None:
    video = path.with_name("visual-source.mp4")
    subtitle = path.with_name("cues.srt")
    _three_scene_fixture(video)
    subtitle.write_text(
        "1\n00:00:00,400 --> 00:00:01,200\nembedded alpha\n\n"
        "2\n00:00:02,100 --> 00:00:02,800\nembedded omega\n",
        encoding="utf-8",
    )
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(video),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=3",
            "-i",
            str(subtitle),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:s:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-c:s",
            "srt",
            "-metadata:s:s:0",
            "language=eng",
            "-shortest",
            "-y",
            str(path),
        ],
        check=True,
    )
    video.unlink()
    subtitle.unlink()


def _late_query_fixture(path: Path) -> None:
    video = path.with_name("late-visual.mp4")
    subtitle = path.with_name("late-cue.srt")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=5:d=20",
            "-c:v",
            "mpeg4",
            "-y",
            str(video),
        ],
        check=True,
    )
    subtitle.write_text(
        "1\n00:00:15,000 --> 00:00:16,000\nlate needle\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(video),
            "-i",
            str(subtitle),
            "-map",
            "0:v:0",
            "-map",
            "1:s:0",
            "-c:v",
            "copy",
            "-c:s",
            "srt",
            "-y",
            str(path),
        ],
        check=True,
    )
    video.unlink()
    subtitle.unlink()


def _adapter(root: Path) -> StructuredDocumentParserAdapter:
    bindings = ScopeBindings(
        (ScopeBinding("scope", root),),
        (str(root),),
        ("scope",),
    )
    return StructuredDocumentParserAdapter(ProjectOwnedBoundedDocumentIngress(bindings))


def test_ffprobe_projects_video_audio_streams_on_one_source_timeline(tmp_path: Path) -> None:
    source = tmp_path / "timeline.mp4"
    _ffmpeg_fixture(source)

    probe = probe_video(str(source), "MP4")
    capabilities = video_runtime_capabilities()

    assert 1_900 <= probe["duration_ms"] <= 2_100
    assert probe["primary_video_stream_index"] == 0
    assert [stream["codec_type"] for stream in probe["streams"]] == ["video", "audio"]
    assert probe["streams"][0]["width"] == 320
    assert probe["streams"][0]["height"] == 180
    assert probe["streams"][1]["sample_rate_hz"] == 16_000
    assert probe["streams"][0]["time_base"] is not None
    assert probe["streams"][0]["start_pts"] is not None
    assert probe["streams"][0]["frame_rate_mode"] == "CONSTANT"
    assert probe["primary_audio_stream_index"] == 1
    assert probe["primary_subtitle_stream_index"] is None
    assert probe["track_selection_policy_id"] == "DEFAULT_DISPOSITION_THEN_LOWEST_INDEX_V1"
    assert capabilities["probe_ready"] is True
    assert capabilities["timeline_authority"] == "SOURCE_PRESENTATION_TIMESTAMPS"
    assert capabilities["persistence_effect"] == "NONE"
    assert "/Users/" not in json.dumps(capabilities, sort_keys=True)


def test_stream_precision_projection_and_primary_selection_are_explicit() -> None:
    projected = video_documents._stream_projection(
        {
            "index": 7,
            "codec_type": "video",
            "codec_name": "h264",
            "start_time": "1.250",
            "duration": "2.500",
            "time_base": "1/90000",
            "start_pts": "112500",
            "duration_ts": "225000",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "24000/1001",
            "r_frame_rate": "30000/1001",
            "sample_aspect_ratio": "1/1",
            "display_aspect_ratio": "16/9",
            "color_primaries": "bt709",
            "tags": {"rotate": "90"},
            "disposition": {"default": 1},
        },
        duration_ms=10_000,
    )
    assert projected["time_base"] == "1/90000"
    assert projected["start_pts"] == 112_500
    assert projected["duration_ts"] == 225_000
    assert projected["start_ms"] == 1_250
    assert projected["end_ms"] == 3_750
    assert projected["frame_rate_mode"] == "VARIABLE_SUSPECTED"
    assert projected["rotation_degrees"] == 90
    assert projected["display_aspect_ratio"] == "16/9"

    streams = [
        {"index": 4, "codec_type": "video", "default": False, "attached_picture": False},
        {"index": 8, "codec_type": "video", "default": True, "attached_picture": False},
        {"index": 2, "codec_type": "video", "default": True, "attached_picture": True},
        {"index": 3, "codec_type": "audio", "default": False},
        {"index": 6, "codec_type": "audio", "default": True},
    ]
    assert video_documents._select_primary(streams, "video") == 8
    assert video_documents._select_primary(streams, "audio") == 6


def test_video_probe_worker_publishes_structure_without_decoding_frames(tmp_path: Path) -> None:
    source = tmp_path / "timeline.mp4"
    _ffmpeg_fixture(source)

    result = VideoProbeWorker("MP4")(str(source))

    assert result["backend_name"] == "FFprobe"
    assert [item["kind"] for item in result["items"]] == [
        "video_document",
        "video_video_stream",
        "video_audio_stream",
    ]
    timeline = result["items"][0]
    assert timeline["extension"]["timing_authority"] == "SOURCE_PRESENTATION_TIMESTAMPS"
    assert result["resource_extension"]["decoded_frame_count"] == 0
    assert result["resource_extension"]["decoded_audio_bytes"] == 0
    assert result["resource_extension"]["persistence_effect"] == "NONE"


def test_shared_adapter_routes_suffix_admission_through_ffprobe_identity(tmp_path: Path) -> None:
    source = tmp_path / "timeline.mp4"
    _ffmpeg_fixture(source)

    observation = _adapter(tmp_path).observe(
        {
            "scope_id": "scope",
            "relative_path": source.name,
            "parser_profile": "FAST",
            "view": "STRUCTURE",
            "intent": "STRUCTURE",
        }
    )

    assert observation.status == "COMPLETE"
    assert observation.source_format == "MP4"
    assert observation.backend_name == "FFprobe"
    assert observation.provenance.source_kind == CURRENT_FILESYSTEM_VIDEO
    assert observation.execution is not None
    assert observation.execution.initial_profile == "VIDEO_PROBE"
    assert observation.resources.media is not None
    assert observation.resources.media["timeline_authority"] == "SOURCE_PRESENTATION_TIMESTAMPS"


def test_video_suffix_is_only_admission_and_audio_only_mp4_fails_closed(tmp_path: Path) -> None:
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"not a container")
    assert identify_document_format(fake, fake.name).source_format == "MP4"

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    audio_only = tmp_path / "audio-only.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "aac",
            "-y",
            str(audio_only),
        ],
        check=True,
    )
    malformed = _adapter(tmp_path).observe(
        {
            "scope_id": "scope",
            "relative_path": audio_only.name,
            "parser_profile": "FAST",
            "view": "STRUCTURE",
            "intent": "STRUCTURE",
        }
    )
    assert malformed.status == "MALFORMED"
    assert malformed.items == ()


def test_video_discovery_and_flat_schema_are_additive() -> None:
    assert normalize_document_extensions(["MP4", ".mkv", "webm"]) == (
        ".mkv",
        ".mp4",
        ".webm",
    )
    extension_enum = DOCUMENT_INPUT_SCHEMA["properties"]["extensions"]["items"]["enum"]
    assert {"MP4", "MOV", "MKV", "WEBM", ".mp4", ".m4v", ".mov", ".mkv", ".webm"} <= set(
        extension_enum
    )


def test_scene_detection_and_representative_frames_preserve_source_time(tmp_path: Path) -> None:
    source = tmp_path / "three-scenes.mp4"
    _three_scene_fixture(source)
    scenes = detect_video_scenes(
        str(source),
        stream_index=0,
        start_ms=0,
        end_ms=3_000,
    )

    assert len(scenes) == 3
    assert [scene["start_ms"] for scene in scenes] == [0, 1_000, 2_000]
    assert [scene["end_ms"] for scene in scenes] == [1_000, 2_000, 3_000]
    assert [scene["representative_timestamp_ms"] for scene in scenes] == [500, 1_500, 2_500]

    digest = video_request_digest(
        source_sha256="a" * 64,
        scope_id="scope",
        relative_path=source.name,
        intent="READ",
        content_query=None,
        analysis="SCENES",
    )
    result = VideoSceneWorker("MP4", 0, False, digest, "a" * 64)(str(source))
    scene_items = [item for item in result["items"] if item["kind"] == "video_scene"]
    frame_items = [
        item for item in result["items"] if item["kind"] == "video_representative_frame"
    ]
    assert len(scene_items) == len(frame_items) == 3
    assert all(item["extension"]["scene_policy_id"] == VIDEO_SCENE_POLICY_ID for item in scene_items)
    assert [item["location"]["timestamp_ms"] for item in frame_items] == [500, 1_500, 2_500]
    assert all(item["extension"]["persistence_effect"] == "NONE" for item in frame_items)
    assert result["resource_extension"]["decoded_frame_count"] == 9
    assert result["resource_extension"]["candidate_frame_count"] == 9
    assert result["resource_extension"]["selected_frame_count"] == 3
    assert all(item["extension"]["candidate_count"] == 3 for item in frame_items)
    assert all("selection_score" in item["extension"] for item in frame_items)
    assert result["resource_extension"]["ocr_frame_count"] == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == [source.name]


def test_hybrid_scene_detector_preserves_black_transition_reason(tmp_path: Path) -> None:
    source = tmp_path / "black-transition.mp4"
    _black_transition_fixture(source)
    scenes = detect_video_scenes(str(source), stream_index=0, start_ms=0, end_ms=3_000)

    reasons = [str(scene["boundary_reason"]) for scene in scenes[1:]]
    detectors = [str(scene["boundary_detector"]) for scene in scenes[1:]]
    assert any("BLACK_ENTER" in reason for reason in reasons)
    assert any("BLACK_EXIT" in reason for reason in reasons)
    assert any("FFMPEG_BLACKDETECT" in detector for detector in detectors)


def test_scene_worker_projects_explicit_ocr_as_a_distinct_modality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "three-scenes.mp4"
    _three_scene_fixture(source)
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.video_documents._frame_ocr",
        lambda _path: [{"text_or_value": "VISIBLE LABEL"}],
    )
    result = VideoSceneWorker("MP4", 0, True, "b" * 64, "c" * 64)(str(source))
    ocr = [item for item in result["items"] if item["kind"] == "video_frame_ocr_text"]

    assert len(ocr) == 3
    assert all(item["extension"]["source_kind"] == "FRAME_OCR" for item in ocr)
    assert all(item["extension"]["model_derived"] is True for item in ocr)
    assert result["resource_extension"]["ocr_frame_count"] == 3
    tracks = [item for item in result["items"] if item["kind"] == "video_text_track"]
    assert len(tracks) == 1
    assert tracks[0]["text_or_value"] == "VISIBLE LABEL"
    assert tracks[0]["location"]["start_ms"] == 500
    assert tracks[0]["location"]["end_ms"] == 2_500
    assert tracks[0]["extension"]["source_kind"] == "VIDEO_TEXT_TRACK"
    assert tracks[0]["extension"]["continuity"] == "SAMPLED_OBSERVATIONS_ONLY"
    assert tracks[0]["extension"]["embedded_subtitle_conflated"] is False
    assert result["resource_extension"]["ocr_track_count"] == 1


def test_temporal_ocr_preserves_normalized_region_confidence_and_breaks_distant_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "three-scenes.mp4"
    _three_scene_fixture(source)
    calls = 0

    def fake_ocr(_path: str) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        text = "Alpha" if calls < 3 else "Omega"
        return [
            {
                "text_or_value": text,
                "confidence": 0.91,
                "extension": {
                    "visual_region": {
                        "bbox": [32.0, 18.0, 160.0, 90.0],
                        "page_size": [320.0, 180.0],
                    }
                },
            }
        ]

    monkeypatch.setattr(
        "local_steward.file_agent.runtime.video_documents._frame_ocr", fake_ocr
    )
    result = VideoSceneWorker("MP4", 0, True, "b" * 64, "c" * 64)(str(source))
    observations = [
        item for item in result["items"] if item["kind"] == "video_frame_ocr_text"
    ]
    tracks = [item for item in result["items"] if item["kind"] == "video_text_track"]

    assert observations[0]["extension"]["normalized_bbox"] == [0.1, 0.1, 0.5, 0.5]
    assert observations[0]["extension"]["confidence"] == 0.91
    assert [track["text_or_value"] for track in tracks] == ["Alpha", "Omega"]
    assert [track["extension"]["observation_count"] for track in tracks] == [2, 1]


def test_shared_adapter_runs_bounded_scene_projection_for_read(tmp_path: Path) -> None:
    source = tmp_path / "three-scenes.mp4"
    _three_scene_fixture(source)
    observation = _adapter(tmp_path).observe(
        {
            "scope_id": "scope",
            "relative_path": source.name,
            "parser_profile": "FAST",
            "view": "READ",
            "intent": "READ",
            "video_analysis": "SCENES",
        }
    )

    assert observation.status == "COMPLETE"
    assert observation.backend_name == "FFmpeg"
    assert observation.execution is not None
    assert observation.execution.selected_profile == "VIDEO_SCENES"
    assert sum(item.kind == "video_scene" for item in observation.items) == 3
    assert sum(item.kind == "video_representative_frame" for item in observation.items) == 3
    assert observation.resources.media is not None
    assert observation.resources.media["decoded_frame_count"] == 9
    assert observation.resources.media["selected_frame_count"] == 3
    assert observation.resources.media["persistence_effect"] == "NONE"


def test_video_view_returns_one_ephemeral_source_pinned_frame(tmp_path: Path) -> None:
    source = tmp_path / "three-scenes.mp4"
    _three_scene_fixture(source)
    bindings = ScopeBindings(
        (ScopeBinding("scope", tmp_path),),
        (str(tmp_path),),
        ("scope",),
    )
    ingress = ProjectOwnedBoundedDocumentIngress(bindings)
    visual = VisualDocumentAdapter(
        ingress,
        StructuredDocumentParserAdapter(ingress),
    ).observe(DocumentVisualRequest("scope", source.name, video_timestamp_ms=1_500))

    assert visual.status == "COMPLETE"
    assert visual.source_format == "MP4"
    assert visual.rendered_timestamp_ms == 1_500
    assert visual.mime_type == "image/png"
    assert visual.image_data.startswith(b"\x89PNG\r\n\x1a\n")
    assert visual.image_sha256 is not None
    assert visual.payload()["source_kind"] == CURRENT_FILESYSTEM_VIDEO
    assert sorted(path.name for path in tmp_path.iterdir()) == [source.name]


def test_representative_candidate_prefers_usable_quality_then_midpoint() -> None:
    candidates = [
        {"timestamp_ms": 250, "selection_score": -0.4},
        {"timestamp_ms": 500, "selection_score": 0.2},
        {"timestamp_ms": 750, "selection_score": 0.8},
    ]
    assert (
        video_documents._select_representative_candidate(candidates, midpoint_ms=500)[
            "timestamp_ms"
        ]
        == 750
    )
    tied = [
        {"timestamp_ms": 250, "selection_score": 0.5},
        {"timestamp_ms": 500, "selection_score": 0.5},
        {"timestamp_ms": 750, "selection_score": 0.5},
    ]
    assert (
        video_documents._select_representative_candidate(tied, midpoint_ms=500)[
            "timestamp_ms"
        ]
        == 500
    )


def test_embedded_subtitles_retain_stream_time_and_non_model_provenance(tmp_path: Path) -> None:
    source = tmp_path / "multimodal.mkv"
    _multimodal_fixture(source)
    probe = probe_video(str(source), "MKV")
    subtitles, warnings = extract_embedded_subtitles(
        str(source),
        streams=probe["streams"],
        start_ms=0,
        end_ms=3_000,
    )

    assert warnings == []
    assert [item["text_or_value"] for item in subtitles] == [
        "embedded alpha",
        "embedded omega",
    ]
    cue_starts = [item["location"]["start_ms"] for item in subtitles]
    assert 400 <= cue_starts[0] <= 500
    assert cue_starts[1] - cue_starts[0] == 1_700
    assert all(item["extension"]["source_kind"] == "EMBEDDED_SUBTITLE" for item in subtitles)
    assert all(item["extension"]["model_derived"] is False for item in subtitles)


def test_multimodal_timeline_joins_but_does_not_conflate_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "multimodal.mkv"
    _multimodal_fixture(source)
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.video_documents.transcribe_media_window",
        lambda *_args, **_kwargs: (
            [
                {
                    "kind": "audio_transcript_segment",
                    "node_id": "video:audio-asr:1",
                    "role": "PARAGRAPH",
                    "text_or_value": "spoken alpha",
                    "parent": "video:timeline",
                    "location": {"start_ms": 600, "end_ms": 1_100, "ordinal": 1},
                    "extension": {"source_kind": "AUDIO_ASR", "model_derived": True},
                }
            ],
            {"base_transcript_digest": "d" * 64},
        ),
    )
    result = VideoTimelineWorker(
        "MKV",
        0,
        False,
        "e" * 64,
        "f" * 64,
        "alpha",
        "en",
    )(str(source))
    subtitle = [
        item for item in result["items"] if item["kind"] == "video_embedded_subtitle_cue"
    ]
    asr = [item for item in result["items"] if item["kind"] == "audio_transcript_segment"]
    windows = [item for item in result["items"] if item["kind"] == "video_query_window"]

    assert subtitle and asr and len(windows) == 2
    assert subtitle[0]["extension"]["source_kind"] == "EMBEDDED_SUBTITLE"
    assert asr[0]["extension"]["source_kind"] == "AUDIO_ASR"
    assert {window["extension"]["matched_source_kind"] for window in windows} == {
        "EMBEDDED_SUBTITLE",
        "AUDIO_ASR",
    }
    assert all(window["extension"]["semantic_agreement_inferred"] is False for window in windows)
    assert result["resource_extension"]["semantic_agreement_inferred"] is False
    assert result["resource_extension"]["modalities"] == {
        "SCENE": True,
        "REPRESENTATIVE_FRAME": True,
            "FRAME_OCR": False,
            "VIDEO_TEXT_TRACK": False,
            "EMBEDDED_SUBTITLE": True,
        "AUDIO_ASR": True,
    }
    assert sorted(path.name for path in tmp_path.iterdir()) == [source.name]


def test_query_anchor_avoids_unrelated_video_decode_and_publishes_plan(tmp_path: Path) -> None:
    source = tmp_path / "late-query.mkv"
    _late_query_fixture(source)
    result = VideoTimelineWorker(
        "MKV",
        0,
        False,
        "e" * 64,
        "f" * 64,
        "needle",
        None,
    )(str(source))

    plan = result["resource_extension"]["decode_plan"]
    assert plan["reason"] == "QUERY_TEXT_ANCHOR"
    assert plan["anchor_source_kinds"] == ["EMBEDDED_SUBTITLE"]
    assert plan["windows"] == [{"start_ms": 10_000, "end_ms": 20_000}]
    assert plan["decoded_ms"] == 10_000
    assert plan["avoided_ms"] == 10_000
    scenes = [item for item in result["items"] if item["kind"] == "video_scene"]
    assert scenes[0]["location"]["start_ms"] == 10_000
    assert all(item["location"]["start_ms"] >= 10_000 for item in scenes)
    assert sorted(path.name for path in tmp_path.iterdir()) == [source.name]
