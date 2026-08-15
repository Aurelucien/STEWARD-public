"""Bounded local video-container probe and source-time projection."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module
import json
import math
from pathlib import Path
import re
import shutil
from statistics import median
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, cast
import unicodedata

from .audio_documents import (
    AUDIO_DECODING_POLICY_SHA256,
    AudioRuntimeUnavailable,
    transcribe_media_window,
)
from .video_semantics import (
    VIDEO_SEMANTIC_RELATIVE_SCORE_MARGIN,
    VIDEO_SEMANTIC_POLICY_ID,
    VideoSemanticUnavailable,
    rank_video_frames,
    video_semantic_runtime_capabilities,
)


VIDEO_SOURCE_FORMATS = frozenset({"MP4", "MOV", "MKV", "WEBM"})
VIDEO_SUFFIX_BY_FORMAT = {
    "MP4": ".mp4",
    "MOV": ".mov",
    "MKV": ".mkv",
    "WEBM": ".webm",
}
VIDEO_FORMAT_BY_SUFFIX = {
    ".mp4": "MP4",
    ".m4v": "MP4",
    ".mov": "MOV",
    ".mkv": "MKV",
    ".webm": "WEBM",
}
MAX_VIDEO_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_VIDEO_STREAMS = 64
MAX_VIDEO_CHAPTERS = 10_000
MAX_VIDEO_PROBE_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_VIDEO_PROCESS_STDERR_BYTES = 64 * 1024
MAX_VIDEO_DURATION_MS = 7 * 24 * 60 * 60 * 1000
MAX_VIDEO_WINDOW_MS = 10 * 60 * 1000
MAX_VIDEO_SCENES = 96
MAX_VIDEO_REPRESENTATIVE_FRAMES = 12
MAX_VIDEO_OCR_FRAMES = 4
MAX_VIDEO_OCR_TRACK_GAP_MS = 3_000
MAX_VIDEO_FRAME_PIXELS = 12_000_000
MAX_VIDEO_FRAME_BYTES = 6 * 1024 * 1024
MAX_VIDEO_SUBTITLE_STREAMS = 8
MAX_VIDEO_SUBTITLE_CUES = 2_000
MAX_VIDEO_SUBTITLE_BYTES = 4 * 1024 * 1024
MAX_VIDEO_QUERY_DECODE_WINDOWS = 4
MAX_VIDEO_VISUAL_SCAN_FRAMES = 48
MAX_VIDEO_VISUAL_REFINEMENT_FRAMES = 8
MAX_VIDEO_VISUAL_ANCHORS = 4
VIDEO_QUERY_PADDING_MS = 5_000
VIDEO_SCENE_CANDIDATE_THRESHOLD = 2.0
VIDEO_SCENE_BASE_THRESHOLD = 8.0
VIDEO_SCENE_ADAPTIVE_RATIO = 2.5
VIDEO_SCENE_POLICY_ID = "FFMPEG_ADAPTIVE_SCDET_BLACKDETECT_CLUSTER_V3"
VIDEO_FRAME_SELECTION_POLICY_ID = "QUALITY_RANK_UNIFORM_SCENE_COVERAGE_V2"
VIDEO_VISUAL_SCAN_POLICY_ID = "WHOLE_SOURCE_UNIFORM_REFINE_DEDUP_V1"
VIDEO_TRACK_SELECTION_POLICY_ID = "DEFAULT_DISPOSITION_THEN_LOWEST_INDEX_V1"
VIDEO_CONTINUATION_DOMAIN = "local_steward.video_continuation.v1"
VIDEO_CONTINUATION_SCHEMA_NAME = "local_steward.video_continuation"
VIDEO_TIME_CONTINUATION_SCHEMA_VERSION = 1
VIDEO_RESULT_PAGE_CONTINUATION_SCHEMA_VERSION = 2
VIDEO_RESULT_PRESENTATION_POLICY_ID = "WEIGHTED_MODALITY_ROUND_ROBIN_V1"
VIDEO_RESULT_MODALITY_QUOTAS = {
    "SCENE_VISUAL": 2,
    "FRAME_OCR": 2,
    "EMBEDDED_SUBTITLE": 2,
    "AUDIO_ASR": 4,
    "VISUAL_SEMANTIC": 1,
    "OTHER": 2,
}
_CONTAINER_RULES: dict[str, set[str]] = {
    "MP4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
    "MOV": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
    "MKV": {"matroska", "webm"},
    "WEBM": {"matroska", "webm"},
}
_MEDIA_TOOL_VERSION = re.compile(r"ff(?:probe|mpeg) version ([^\s]+)")
_SCENE = re.compile(
    r"lavfi\.scd\.score:\s*(?P<score>[0-9.]+),\s*lavfi\.scd\.time:\s*(?P<time>[0-9.]+)"
)
_BLACK = re.compile(
    r"black_start:(?P<start>-?[0-9.]+)\s+black_end:(?P<end>-?[0-9.]+)"
    r"\s+black_duration:(?P<duration>[0-9.]+)"
)
_SRT_CUE = re.compile(
    r"(?:^|\n)(?:\d+\n)?(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})[^\n]*\n(?P<text>.*?)(?=\n\n|\Z)",
    re.DOTALL,
)


class VideoRuntimeUnavailable(RuntimeError):
    """The local FFmpeg probe runtime is unavailable."""


class VideoSourceInvalid(OSError):
    """The admitted source is not a supported, usable video container."""


def _run_bounded(command: list[str], *, timeout: float, stdout_limit: int) -> bytes:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise VideoRuntimeUnavailable("the bounded video subprocess is unavailable") from error
    if (
        len(completed.stdout) > stdout_limit
        or len(completed.stderr) > MAX_VIDEO_PROCESS_STDERR_BYTES
    ):
        raise VideoSourceInvalid("video subprocess output exceeded its bound")
    if completed.returncode != 0:
        raise VideoSourceInvalid("video subprocess rejected the admitted source")
    return completed.stdout


def _run_bounded_completed(
    command: list[str],
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int = MAX_VIDEO_PROCESS_STDERR_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise VideoRuntimeUnavailable("the bounded video subprocess is unavailable") from error
    if len(completed.stdout) > stdout_limit or len(completed.stderr) > stderr_limit:
        raise VideoSourceInvalid("video subprocess output exceeded its bound")
    if completed.returncode != 0:
        raise VideoSourceInvalid("video subprocess rejected the admitted source")
    return completed


def _ffprobe_version(ffprobe: str) -> str:
    raw = _run_bounded([ffprobe, "-version"], timeout=5.0, stdout_limit=16 * 1024)
    match = _MEDIA_TOOL_VERSION.search(raw.decode("utf-8", errors="replace"))
    return match.group(1) if match else "UNKNOWN"


def ffmpeg_runtime_version() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VideoRuntimeUnavailable("ffmpeg is unavailable")
    raw = _run_bounded([ffmpeg, "-version"], timeout=5.0, stdout_limit=16 * 1024)
    match = _MEDIA_TOOL_VERSION.search(raw.decode("utf-8", errors="replace"))
    return match.group(1) if match else "UNKNOWN"


def video_runtime_capabilities() -> dict[str, object]:
    """Publish path-free probe readiness and fixed source-time limits."""
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    version = None
    if ffprobe is not None:
        try:
            version = _ffprobe_version(ffprobe)
        except (VideoRuntimeUnavailable, VideoSourceInvalid):
            pass
    return {
        "schema_name": "local_steward.video_runtime_capabilities",
        "schema_version": 1,
        "supported_formats": sorted(VIDEO_SOURCE_FORMATS),
        "probe_ready": ffprobe is not None,
        "decode_ready": ffmpeg is not None,
        "ffprobe_version": version,
        "timeline_authority": "SOURCE_PRESENTATION_TIMESTAMPS",
        "stream_limit": MAX_VIDEO_STREAMS,
        "chapter_limit": MAX_VIDEO_CHAPTERS,
        "duration_limit_ms": MAX_VIDEO_DURATION_MS,
        "window_limit_ms": MAX_VIDEO_WINDOW_MS,
        "scene_limit": MAX_VIDEO_SCENES,
        "representative_frame_limit": MAX_VIDEO_REPRESENTATIVE_FRAMES,
        "ocr_frame_limit": MAX_VIDEO_OCR_FRAMES,
        "visual_scan_frame_limit": MAX_VIDEO_VISUAL_SCAN_FRAMES,
        "visual_anchor_limit": MAX_VIDEO_VISUAL_ANCHORS,
        "scene_policy_id": VIDEO_SCENE_POLICY_ID,
        "frame_selection_policy_id": VIDEO_FRAME_SELECTION_POLICY_ID,
        "track_selection_policy_id": VIDEO_TRACK_SELECTION_POLICY_ID,
        "visual_scan_policy_id": VIDEO_VISUAL_SCAN_POLICY_ID,
        "result_pagination": {
            "continuation_schema_name": VIDEO_CONTINUATION_SCHEMA_NAME,
            "result_page_schema_version": VIDEO_RESULT_PAGE_CONTINUATION_SCHEMA_VERSION,
            "cache_reuse_required": True,
            "reruns_analysis": False,
        },
        "result_presentation": {
            "policy_id": VIDEO_RESULT_PRESENTATION_POLICY_ID,
            "round_quotas": dict(VIDEO_RESULT_MODALITY_QUOTAS),
        },
        "coverage_report_schema": {
            "schema_name": "local_steward.video_coverage_report",
            "schema_version": 1,
            "sampled_observation_explicit": True,
        },
        "semantic": video_semantic_runtime_capabilities(),
        "runtime_downloads_allowed": False,
        "persistence_effect": "NONE",
    }


def video_request_digest(
    *,
    source_sha256: str,
    scope_id: str,
    relative_path: str,
    intent: str,
    content_query: str | None,
    analysis: str,
    audio_language: str | None = None,
    audio_model_identity_sha256: str | None = None,
) -> str:
    semantic = video_semantic_runtime_capabilities() if content_query else None
    payload = {
        "source_sha256": source_sha256,
        "scope_id": scope_id,
        "relative_path": relative_path,
        "intent": intent,
        "content_query": content_query,
        "analysis": analysis,
        "audio_language": audio_language,
        "audio_model_identity_sha256": audio_model_identity_sha256,
        "audio_decoding_policy_sha256": AUDIO_DECODING_POLICY_SHA256,
        "scene_policy_id": VIDEO_SCENE_POLICY_ID,
        "frame_selection_policy_id": VIDEO_FRAME_SELECTION_POLICY_ID,
        "track_selection_policy_id": VIDEO_TRACK_SELECTION_POLICY_ID,
        "visual_scan_policy_id": VIDEO_VISUAL_SCAN_POLICY_ID,
        "semantic_model_identity_sha256": (
            semantic.get("model_identity_sha256") if semantic else None
        ),
    }
    from ...evidence import canonical_json

    return sha256(VIDEO_CONTINUATION_DOMAIN.encode() + b"\0" + canonical_json(payload)).hexdigest()


def _item_time_bounds(item: dict[str, object]) -> tuple[int, int] | None:
    location = item.get("location")
    if not isinstance(location, dict):
        return None
    timestamp_ms = location.get("timestamp_ms")
    if type(timestamp_ms) is int:
        return timestamp_ms, timestamp_ms
    start_ms = location.get("start_ms")
    end_ms = location.get("end_ms")
    if type(start_ms) is int and type(end_ms) is int:
        return start_ms, end_ms
    return None


def _overlaps(bounds: tuple[int, int] | None, start_ms: int, end_ms: int) -> bool:
    if bounds is None:
        return False
    item_start, item_end = bounds
    return item_start <= end_ms and item_end >= start_ms


def _video_coverage_report(
    items: list[dict[str, object]],
    *,
    window_start_ms: int,
    window_end_ms: int,
    detected_scene_count: int,
    selected_frame_count: int,
    ocr_frame_count: int,
    include_ocr: bool,
) -> dict[str, object]:
    """Describe sampled multimodal coverage without implying frame completeness."""
    kind_counts: dict[str, int] = {}
    for item in items:
        kind = item.get("kind")
        if isinstance(kind, str):
            kind_counts[kind] = kind_counts.get(kind, 0) + 1

    scenes = [item for item in items if item.get("kind") == "video_scene"]
    frames = [
        item for item in items if item.get("kind") == "video_representative_frame"
    ]
    ocr_items = [item for item in items if item.get("kind") == "video_frame_ocr_text"]
    asr_items = [
        item
        for item in items
        if item.get("kind")
        in {"audio_transcript_segment", "audio_aligned_word", "audio_speaker_turn"}
    ]
    subtitle_items = [
        item for item in items if item.get("kind") == "video_embedded_subtitle_cue"
    ]
    semantic_items = [
        item for item in items if item.get("kind") == "video_visual_semantic_anchor"
    ]

    frame_timestamps = sorted(
        {
            bounds[0]
            for item in frames
            if (bounds := _item_time_bounds(item)) is not None
        }
    )
    ocr_timestamps = sorted(
        {
            bounds[0]
            for item in ocr_items
            if (bounds := _item_time_bounds(item)) is not None
        }
    )
    temporal_groups: list[dict[str, object]] = []
    for scene in scenes:
        bounds = _item_time_bounds(scene)
        node_id = scene.get("node_id")
        if bounds is None or not isinstance(node_id, str):
            continue
        start_ms, end_ms = bounds

        def linked_nodes(candidates: list[dict[str, object]]) -> list[str]:
            return [
                candidate_id
                for candidate in candidates
                if _overlaps(_item_time_bounds(candidate), start_ms, end_ms)
                and isinstance((candidate_id := candidate.get("node_id")), str)
            ]

        temporal_groups.append(
            {
                "scene_node_id": node_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "representative_frame_node_ids": linked_nodes(frames),
                "ocr_node_ids": linked_nodes(ocr_items),
                "embedded_subtitle_node_ids": linked_nodes(subtitle_items),
                "audio_asr_node_ids": linked_nodes(asr_items),
                "visual_semantic_node_ids": linked_nodes(semantic_items),
            }
        )

    asr_accuracy_values: set[str] = set()
    for item in asr_items:
        extension = item.get("extension")
        if not isinstance(extension, dict):
            continue
        accuracy = extension.get("timestamp_accuracy")
        if isinstance(accuracy, str):
            asr_accuracy_values.add(accuracy)
    asr_accuracy = sorted(asr_accuracy_values)
    selected_scene_count = len(scenes)
    return {
        "schema_name": "local_steward.video_coverage_report",
        "schema_version": 1,
        "window_start_ms": window_start_ms,
        "window_end_ms": window_end_ms,
        "modality_item_counts": kind_counts,
        "scene": {
            "detected_count": detected_scene_count,
            "selected_count": selected_scene_count,
            "selection_complete": selected_scene_count == detected_scene_count,
            "unselected_count": max(0, detected_scene_count - selected_scene_count),
            "boundaries": [
                {
                    "node_id": scene.get("node_id"),
                    "start_ms": bounds[0],
                    "end_ms": bounds[1],
                }
                for scene in scenes
                if (bounds := _item_time_bounds(scene)) is not None
            ],
        },
        "representative_frames": {
            "selected_count": selected_frame_count,
            "timestamps_ms": frame_timestamps,
            "coverage": "SAMPLED_REPRESENTATIVE_FRAMES_ONLY",
            "all_other_source_times_visually_unobserved": True,
        },
        "ocr": {
            "requested": include_ocr,
            "observed_frame_count": ocr_frame_count,
            "timestamps_ms": ocr_timestamps,
            "coverage": (
                "SELECTED_REPRESENTATIVE_FRAMES_ONLY" if include_ocr else "NOT_REQUESTED"
            ),
            "all_other_source_times_unobserved": include_ocr,
        },
        "audio_asr": {
            "item_count": len(asr_items),
            "timestamp_accuracy": asr_accuracy or ["MODEL_APPROXIMATE"],
            "word_exact_alignment_claimed": False,
        },
        "embedded_subtitle": {"item_count": len(subtitle_items)},
        "visual_semantic": {"item_count": len(semantic_items)},
        "result_presentation": {
            "policy_id": VIDEO_RESULT_PRESENTATION_POLICY_ID,
            "round_quotas": dict(VIDEO_RESULT_MODALITY_QUOTAS),
            "preserves_items_without_duplication": True,
        },
        "temporal_browse_groups": temporal_groups,
        "semantic_agreement_inferred": False,
    }


def _video_analysis_summary_item(
    coverage_report: dict[str, object], *, window_start_ms: int, window_end_ms: int
) -> dict[str, object]:
    return {
        "kind": "video_analysis_summary",
        "node_id": f"video:analysis-summary:{window_start_ms}-{window_end_ms}",
        "role": "METADATA",
        "text_or_value": (
            "Bounded multimodal coverage summary; frame, OCR, semantic and ASR timing "
            "limits remain explicit."
        ),
        "parent": "video:timeline",
        "location": {"start_ms": window_start_ms, "end_ms": window_end_ms},
        "extension": {
            "source_kind": "VIDEO_ANALYSIS_SUMMARY",
            "projection_derived": True,
            "coverage_report": coverage_report,
        },
    }


def _milliseconds(value: object, *, allow_negative: bool = False) -> int | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (not allow_negative and number < 0):
        return None
    return round(number * 1000)


def _positive_int(value: object) -> int | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result is not None and result > 0 else None


def _integer(value: object) -> int | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _rational(value: object) -> str | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    numerator, denominator = value.split("/", 1)
    try:
        if int(denominator) == 0:
            return None
        int(numerator)
    except ValueError:
        return None
    return value


def _rotation(raw: dict[str, object], tags: dict[str, object]) -> int | None:
    side_data_raw = raw.get("side_data_list")
    if isinstance(side_data_raw, list):
        for side_data in side_data_raw:
            if isinstance(side_data, dict):
                value = side_data.get("rotation")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return round(float(value)) % 360
    tagged = tags.get("rotate")
    if isinstance(tagged, (str, int)) and not isinstance(tagged, bool):
        try:
            return round(float(tagged)) % 360
        except ValueError:
            pass
    return None


def _frame_rate_mode(average: str | None, real: str | None) -> str:
    if average is None or real is None:
        return "UNKNOWN"
    average_fraction = _rational(average)
    real_fraction = _rational(real)
    if average_fraction is None or real_fraction is None:
        return "UNKNOWN"
    average_numerator, average_denominator = map(int, average_fraction.split("/"))
    real_numerator, real_denominator = map(int, real_fraction.split("/"))
    return (
        "CONSTANT"
        if average_numerator * real_denominator == real_numerator * average_denominator
        else "VARIABLE_SUSPECTED"
    )


def _select_primary(streams: list[dict[str, object]], codec_type: str) -> int | None:
    candidates = [
        stream
        for stream in streams
        if stream.get("codec_type") == codec_type
        and type(stream.get("index")) is int
        and (codec_type != "video" or not stream.get("attached_picture"))
    ]
    if not candidates:
        return None
    selected = min(
        candidates,
        key=lambda stream: (
            0 if stream.get("default") is True else 1,
            cast(int, stream["index"]),
        ),
    )
    return cast(int, selected["index"])


def _stream_projection(raw: dict[str, object], *, duration_ms: int) -> dict[str, object]:
    index = raw.get("index")
    codec_type = raw.get("codec_type")
    codec_name = raw.get("codec_name")
    if type(index) is not int or codec_type not in {"video", "audio", "subtitle"}:
        raise VideoSourceInvalid("video stream metadata is invalid")
    start_ms = _milliseconds(raw.get("start_time"), allow_negative=True)
    stream_duration_ms = _milliseconds(raw.get("duration"))
    tags_raw = raw.get("tags")
    disposition_raw = raw.get("disposition")
    tags = cast(dict[str, object], tags_raw) if isinstance(tags_raw, dict) else {}
    disposition = (
        cast(dict[str, object], disposition_raw) if isinstance(disposition_raw, dict) else {}
    )
    projection: dict[str, object] = {
        "index": index,
        "codec_type": codec_type,
        "codec_name": codec_name if isinstance(codec_name, str) else None,
        "start_ms": start_ms if start_ms is not None else 0,
        "end_ms": min(duration_ms, max(0, start_ms or 0) + (stream_duration_ms or duration_ms)),
        "language": tags.get("language") if isinstance(tags.get("language"), str) else None,
        "title": tags.get("title") if isinstance(tags.get("title"), str) else None,
        "default": disposition.get("default") == 1,
        "forced": disposition.get("forced") == 1,
        "attached_picture": disposition.get("attached_pic") == 1,
        "time_base": _rational(raw.get("time_base")),
        "start_pts": _integer(raw.get("start_pts")),
        "duration_ts": _integer(raw.get("duration_ts")),
    }
    if codec_type == "video":
        average_frame_rate = _rational(raw.get("avg_frame_rate"))
        real_frame_rate = _rational(raw.get("r_frame_rate"))
        projection.update(
            {
                "width": _positive_int(raw.get("width")),
                "height": _positive_int(raw.get("height")),
                "pixel_format": raw.get("pix_fmt")
                if isinstance(raw.get("pix_fmt"), str)
                else None,
                "average_frame_rate": average_frame_rate,
                "real_frame_rate": real_frame_rate,
                "frame_rate_mode": _frame_rate_mode(average_frame_rate, real_frame_rate),
                "sample_aspect_ratio": _rational(raw.get("sample_aspect_ratio")),
                "display_aspect_ratio": _rational(raw.get("display_aspect_ratio")),
                "field_order": raw.get("field_order")
                if isinstance(raw.get("field_order"), str)
                else None,
                "color_range": raw.get("color_range")
                if isinstance(raw.get("color_range"), str)
                else None,
                "color_space": raw.get("color_space")
                if isinstance(raw.get("color_space"), str)
                else None,
                "color_transfer": raw.get("color_transfer")
                if isinstance(raw.get("color_transfer"), str)
                else None,
                "color_primaries": raw.get("color_primaries")
                if isinstance(raw.get("color_primaries"), str)
                else None,
                "rotation_degrees": _rotation(raw, tags),
            }
        )
    elif codec_type == "audio":
        projection.update(
            {
                "sample_rate_hz": _positive_int(raw.get("sample_rate")),
                "channels": _positive_int(raw.get("channels")),
                "channel_layout": raw.get("channel_layout")
                if isinstance(raw.get("channel_layout"), str)
                else None,
            }
        )
    return projection


def probe_video(source_path: str, source_format: str) -> dict[str, object]:
    """Validate one video container and project bounded stream/chapter facts."""
    if source_format not in VIDEO_SOURCE_FORMATS:
        raise VideoSourceInvalid("video source format is unsupported")
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise VideoRuntimeUnavailable("ffprobe is unavailable")
    raw = _run_bounded(
        [
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-probesize",
            str(64 * 1024 * 1024),
            "-analyzeduration",
            "60000000",
            "-show_entries",
            "format=format_name,duration,start_time,size,bit_rate:"
            "stream=index,codec_name,codec_type,width,height,pix_fmt,avg_frame_rate,"
            "r_frame_rate,time_base,start_pts,duration_ts,sample_aspect_ratio,"
            "display_aspect_ratio,field_order,color_range,color_space,color_transfer,"
            "color_primaries,sample_rate,channels,channel_layout,start_time,duration:"
            "stream_tags=language,title,rotate:stream_disposition=default,forced,attached_pic:"
            "stream_side_data=rotation:"
            "chapter=id,start_time,end_time:chapter_tags=title",
            "-of",
            "json",
            source_path,
        ],
        timeout=30.0,
        stdout_limit=MAX_VIDEO_PROBE_OUTPUT_BYTES,
    )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VideoSourceInvalid("ffprobe returned invalid JSON") from error
    media_format = payload.get("format")
    streams_raw = payload.get("streams")
    chapters_raw = payload.get("chapters", [])
    if (
        not isinstance(media_format, dict)
        or not isinstance(streams_raw, list)
        or not isinstance(chapters_raw, list)
        or len(streams_raw) > MAX_VIDEO_STREAMS
        or len(chapters_raw) > MAX_VIDEO_CHAPTERS
    ):
        raise VideoSourceInvalid("video container metadata is invalid or exceeds its bound")
    raw_container = media_format.get("format_name")
    containers = (
        {item.strip() for item in raw_container.split(",")}
        if isinstance(raw_container, str)
        else set()
    )
    if not containers.intersection(_CONTAINER_RULES[source_format]):
        raise VideoSourceInvalid("video container identity does not match the selected format")
    duration_ms = _milliseconds(media_format.get("duration"))
    if duration_ms is None or not 0 < duration_ms <= MAX_VIDEO_DURATION_MS:
        raise VideoSourceInvalid("video duration is unavailable or exceeds its bound")
    streams = [
        _stream_projection(stream, duration_ms=duration_ms)
        for stream in streams_raw
        if isinstance(stream, dict) and stream.get("codec_type") in {"video", "audio", "subtitle"}
    ]
    usable_video = [
        stream
        for stream in streams
        if stream["codec_type"] == "video"
        and not stream["attached_picture"]
        and isinstance(stream.get("codec_name"), str)
        and stream.get("width") is not None
        and stream.get("height") is not None
    ]
    if not usable_video:
        raise VideoSourceInvalid("a usable non-attached video stream is required")
    chapters: list[dict[str, object]] = []
    for ordinal, raw_chapter in enumerate(chapters_raw, start=1):
        if not isinstance(raw_chapter, dict):
            raise VideoSourceInvalid("video chapter metadata is invalid")
        start_ms = _milliseconds(raw_chapter.get("start_time"))
        end_ms = _milliseconds(raw_chapter.get("end_time"))
        if start_ms is None or end_ms is None or not 0 <= start_ms <= end_ms <= duration_ms:
            raise VideoSourceInvalid("video chapter range is invalid")
        tags_raw = raw_chapter.get("tags")
        tags = cast(dict[str, object], tags_raw) if isinstance(tags_raw, dict) else {}
        chapters.append(
            {
                "ordinal": ordinal,
                "id": raw_chapter.get("id") if type(raw_chapter.get("id")) is int else ordinal,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "title": tags.get("title") if isinstance(tags.get("title"), str) else None,
            }
        )
    primary_video_index = _select_primary(usable_video, "video")
    if primary_video_index is None:
        raise VideoSourceInvalid("a primary video stream could not be selected")
    return {
        "container": raw_container,
        "duration_ms": duration_ms,
        "container_start_ms": _milliseconds(media_format.get("start_time"), allow_negative=True),
        "bit_rate": media_format.get("bit_rate"),
        "streams": streams,
        "chapters": chapters,
        "primary_video_stream_index": primary_video_index,
        "primary_audio_stream_index": _select_primary(streams, "audio"),
        "primary_subtitle_stream_index": _select_primary(streams, "subtitle"),
        "track_selection_policy_id": VIDEO_TRACK_SELECTION_POLICY_ID,
    }


def _items(probe: dict[str, object]) -> list[dict[str, object]]:
    duration_raw = probe["duration_ms"]
    if type(duration_raw) is not int:
        raise VideoSourceInvalid("video duration projection is invalid")
    duration_ms = duration_raw
    items: list[dict[str, object]] = [
        {
            "kind": "video_document",
            "node_id": "video:timeline",
            "role": "DOCUMENT",
            "text_or_value": None,
            "parent": None,
            "location": {"start_ms": 0, "end_ms": duration_ms},
            "extension": {
                "container": probe["container"],
                "container_start_ms": probe["container_start_ms"],
                "primary_video_stream_index": probe["primary_video_stream_index"],
                "primary_audio_stream_index": probe["primary_audio_stream_index"],
                "primary_subtitle_stream_index": probe["primary_subtitle_stream_index"],
                "track_selection_policy_id": probe["track_selection_policy_id"],
                "timing_authority": "SOURCE_PRESENTATION_TIMESTAMPS",
            },
        }
    ]
    streams = probe["streams"]
    assert isinstance(streams, list)
    for stream in streams:
        assert isinstance(stream, dict)
        kind = str(stream["codec_type"])
        extension = {
            key: value
            for key, value in stream.items()
            if key not in {"index", "start_ms", "end_ms"} and value is not None
        }
        items.append(
            {
                "kind": f"video_{kind}_stream",
                "node_id": f"video:stream:{stream['index']}",
                "role": "METADATA",
                "text_or_value": stream.get("title"),
                "parent": "video:timeline",
                "location": {
                    "stream_index": int(stream["index"]),
                    "start_ms": int(stream["start_ms"]),
                    "end_ms": int(stream["end_ms"]),
                },
                "extension": extension,
            }
        )
    chapters = probe["chapters"]
    assert isinstance(chapters, list)
    for chapter in chapters:
        assert isinstance(chapter, dict)
        items.append(
            {
                "kind": "video_chapter",
                "node_id": f"video:chapter:{chapter['ordinal']}",
                "role": "SECTION",
                "text_or_value": chapter.get("title"),
                "parent": "video:timeline",
                "location": {
                    "ordinal": int(chapter["ordinal"]),
                    "start_ms": int(chapter["start_ms"]),
                    "end_ms": int(chapter["end_ms"]),
                },
                "extension": {"chapter_id": chapter["id"]},
            }
        )
    return items


def detect_video_scenes(
    source_path: str,
    *,
    stream_index: int,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, object]]:
    """Detect bounded content and black transitions on the source timeline."""
    if start_ms < 0 or end_ms <= start_ms or end_ms - start_ms > MAX_VIDEO_WINDOW_MS:
        raise VideoSourceInvalid("video scene window is invalid")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VideoRuntimeUnavailable("ffmpeg is unavailable")
    completed = _run_bounded_completed(
        [
            ffmpeg,
            "-nostdin",
            "-nostats",
            "-v",
            "info",
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-t",
            f"{(end_ms - start_ms) / 1000:.3f}",
            "-i",
            source_path,
            "-map",
            f"0:{stream_index}",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            f"scdet=threshold={VIDEO_SCENE_CANDIDATE_THRESHOLD},"
            "blackdetect=d=0.100:pix_th=0.10:pic_th=0.98",
            "-f",
            "null",
            "-",
        ],
        timeout=max(30.0, min(300.0, (end_ms - start_ms) / 1000 / 2)),
        stdout_limit=1024,
        stderr_limit=512 * 1024,
    )
    stderr = completed.stderr.decode("utf-8", errors="replace")
    changes: list[dict[str, object]] = []
    for match in _SCENE.finditer(stderr):
        timestamp_ms = start_ms + round(float(match.group("time")) * 1000)
        if start_ms < timestamp_ms < end_ms:
            changes.append(
                {
                    "timestamp_ms": timestamp_ms,
                    "score": float(match.group("score")),
                    "detector": "FFMPEG_SCDET",
                    "reason": "CONTENT_CHANGE",
                }
            )
    for match in _BLACK.finditer(stderr):
        for field, reason in (("start", "BLACK_ENTER"), ("end", "BLACK_EXIT")):
            timestamp_ms = start_ms + round(float(match.group(field)) * 1000)
            if start_ms < timestamp_ms < end_ms:
                changes.append(
                    {
                        "timestamp_ms": timestamp_ms,
                        "score": None,
                        "detector": "FFMPEG_BLACKDETECT",
                        "reason": reason,
                    }
                )
    content_changes = [
        change for change in changes if change.get("detector") == "FFMPEG_SCDET"
    ]
    adaptive_changes: list[dict[str, object]] = []
    for index, change in enumerate(content_changes):
        score = change.get("score")
        if not isinstance(score, float):
            continue
        neighborhood = [
            candidate_score
            for candidate in content_changes[max(0, index - 4) : index + 5]
            if isinstance((candidate_score := candidate.get("score")), float)
            and candidate is not change
        ]
        local_baseline = median(neighborhood) if neighborhood else 0.0
        if score >= VIDEO_SCENE_BASE_THRESHOLD or score >= (
            local_baseline * VIDEO_SCENE_ADAPTIVE_RATIO
        ):
            adaptive_changes.append(
                {
                    **change,
                    "adaptive_baseline": round(local_baseline, 6),
                    "adaptive_ratio": VIDEO_SCENE_ADAPTIVE_RATIO,
                }
            )
    changes = [
        *adaptive_changes,
        *(change for change in changes if change.get("detector") == "FFMPEG_BLACKDETECT"),
    ]
    deduplicated: list[dict[str, object]] = []
    for change in sorted(changes, key=lambda item: cast(int, item["timestamp_ms"])):
        timestamp_ms = cast(int, change["timestamp_ms"])
        if deduplicated and timestamp_ms - cast(int, deduplicated[-1]["timestamp_ms"]) < 250:
            previous = deduplicated[-1]
            previous_score = previous.get("score")
            score = change.get("score")
            selected = previous
            if isinstance(score, float) and (
                not isinstance(previous_score, float) or score > previous_score
            ):
                selected = change
            detectors = sorted(
                set(str(previous["detector"]).split("+"))
                | set(str(change["detector"]).split("+"))
            )
            reasons = sorted(
                set(str(previous["reason"]).split("+"))
                | set(str(change["reason"]).split("+"))
            )
            deduplicated[-1] = {
                **selected,
                "detector": "+".join(detectors),
                "reason": "+".join(reasons),
            }
            continue
        deduplicated.append(change)
    boundaries = [
        start_ms,
        *(cast(int, change["timestamp_ms"]) for change in deduplicated),
        end_ms,
    ]
    scenes: list[dict[str, object]] = []
    for ordinal, (left, right) in enumerate(
        zip(boundaries, boundaries[1:], strict=False), start=1
    ):
        if ordinal > MAX_VIDEO_SCENES:
            break
        preceding = deduplicated[ordinal - 2] if ordinal > 1 else None
        scenes.append(
            {
                "ordinal": ordinal,
                "start_ms": left,
                "end_ms": right,
                "boundary_score": preceding.get("score") if preceding else None,
                "boundary_detector": preceding.get("detector") if preceding else None,
                "boundary_reason": preceding.get("reason") if preceding else "WINDOW_START",
                "representative_timestamp_ms": left + max(0, (right - left) // 2),
            }
        )
    return scenes


def _representative_timestamps(start_ms: int, end_ms: int) -> list[int]:
    if start_ms < 0 or end_ms <= start_ms:
        raise VideoSourceInvalid("representative frame range is invalid")
    duration = end_ms - start_ms
    return list(
        dict.fromkeys(
            (
                start_ms + duration // 4,
                start_ms + duration // 2,
                start_ms + (3 * duration) // 4,
            )
        )
    )


def _select_scene_coverage(
    scenes: list[dict[str, object]], *, limit: int
) -> list[dict[str, object]]:
    """Select deterministic timeline-wide scene coverage instead of a leading prefix."""
    if limit <= 0 or not scenes:
        return []
    if len(scenes) <= limit:
        return scenes
    indexes = [round(index * (len(scenes) - 1) / (limit - 1)) for index in range(limit)]
    return [scenes[index] for index in dict.fromkeys(indexes)]


def _visual_scan_timestamps(duration_ms: int, *, limit: int) -> list[int]:
    if duration_ms <= 0 or limit <= 0:
        return []
    sample_count = min(limit, max(1, math.ceil(duration_ms / 10_000)))
    return [
        min(duration_ms - 1, ((2 * index + 1) * duration_ms) // (2 * sample_count))
        for index in range(sample_count)
    ]


def _extract_visual_scan_frame(
    source_path: str,
    target_path: str,
    *,
    stream_index: int,
    timestamp_ms: int,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VideoRuntimeUnavailable("ffmpeg is unavailable")
    _run_bounded_completed(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{timestamp_ms / 1000:.3f}",
            "-i",
            source_path,
            "-map",
            f"0:{stream_index}",
            "-frames:v",
            "1",
            "-vf",
            "scale='min(336,iw)':-2",
            "-c:v",
            "png",
            "-y",
            target_path,
        ],
        timeout=30.0,
        stdout_limit=1024,
    )
    target = Path(target_path)
    if not target.is_file() or target.stat().st_size > 1024 * 1024:
        raise VideoSourceInvalid("visual scan frame is invalid")


def _frame_average_hash(frame_path: str) -> int:
    """Return a compact color-aware perceptual signature for scan de-duplication."""
    Image = import_module("PIL.Image")
    with Image.open(frame_path) as image:
        pixels = list(image.convert("RGB").resize((8, 8)).get_flattened_data())
    signature = 0
    for pixel in pixels:
        for value in pixel:
            signature = (signature << 4) | (int(value) >> 4)
    return signature


def _hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _semantic_windows_from_anchors(
    anchors: list[dict[str, object]], *, duration_ms: int
) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    for anchor in anchors:
        location = anchor.get("location")
        if not isinstance(location, dict) or type(location.get("timestamp_ms")) is not int:
            continue
        timestamp_ms = cast(int, location["timestamp_ms"])
        start_ms = max(0, timestamp_ms - VIDEO_QUERY_PADDING_MS)
        end_ms = min(duration_ms, timestamp_ms + VIDEO_QUERY_PADDING_MS)
        if end_ms <= start_ms:
            continue
        if any(start_ms < existing_end and end_ms > existing_start for existing_start, existing_end in windows):
            continue
        windows.append((start_ms, end_ms))
        if len(windows) >= MAX_VIDEO_VISUAL_ANCHORS:
            break
    return sorted(windows)


def _visual_semantic_anchors(
    source_path: str,
    *,
    stream_index: int,
    duration_ms: int,
    query: str,
) -> tuple[list[dict[str, object]], dict[str, object], list[str]]:
    """Coarsely search the whole source and refine the strongest temporal regions."""
    warnings: list[str] = []
    semantic = video_semantic_runtime_capabilities()
    if semantic.get("ready") is not True:
        return [], {"status": "UNAVAILABLE", "runtime": semantic}, [
            "VIDEO_VISUAL_SEMANTIC_RUNTIME_UNAVAILABLE"
        ]
    with TemporaryDirectory(prefix="steward-video-visual-search-") as temporary:
        frames: list[tuple[int, str]] = []
        signatures: list[int] = []
        duplicate_count = 0
        coarse_decoded_count = 0
        refinement_decoded_count = 0

        def admit(timestamp_ms: int) -> None:
            nonlocal duplicate_count
            frame_path = str(Path(temporary) / f"frame-{len(frames):03d}.png")
            _extract_visual_scan_frame(
                source_path,
                frame_path,
                stream_index=stream_index,
                timestamp_ms=timestamp_ms,
            )
            signature = _frame_average_hash(frame_path)
            if any(_hamming_distance(signature, existing) <= 24 for existing in signatures):
                duplicate_count += 1
                Path(frame_path).unlink(missing_ok=True)
                return
            frames.append((timestamp_ms, frame_path))
            signatures.append(signature)

        coarse_timestamps = _visual_scan_timestamps(
            duration_ms, limit=MAX_VIDEO_VISUAL_SCAN_FRAMES
        )
        for timestamp_ms in coarse_timestamps:
            admit(timestamp_ms)
            coarse_decoded_count += 1
        ranked, model = rank_video_frames(query=query, frames=frames)
        spacing = duration_ms // max(1, len(coarse_timestamps))
        refinement_timestamps: list[int] = []
        for candidate in ranked[:4]:
            timestamp_ms = cast(int, candidate["timestamp_ms"])
            for delta in (-max(500, spacing // 3), max(500, spacing // 3)):
                refined = min(duration_ms - 1, max(0, timestamp_ms + delta))
                if refined not in coarse_timestamps and refined not in refinement_timestamps:
                    refinement_timestamps.append(refined)
        for timestamp_ms in refinement_timestamps[:MAX_VIDEO_VISUAL_REFINEMENT_FRAMES]:
            admit(timestamp_ms)
            refinement_decoded_count += 1
        ranked, model = rank_video_frames(query=query, frames=frames)
        anchors: list[dict[str, object]] = []
        selected_windows: list[tuple[int, int]] = []
        best_similarity = (
            cast(float, ranked[0]["similarity"])
            if ranked
            else float("-inf")
        )
        for candidate in ranked:
            if (
                cast(float, candidate["similarity"])
                < best_similarity - VIDEO_SEMANTIC_RELATIVE_SCORE_MARGIN
            ):
                continue
            timestamp_ms = cast(int, candidate["timestamp_ms"])
            start_ms = max(0, timestamp_ms - VIDEO_QUERY_PADDING_MS)
            end_ms = min(duration_ms, timestamp_ms + VIDEO_QUERY_PADDING_MS)
            if any(
                start_ms < existing_end and end_ms > existing_start
                for existing_start, existing_end in selected_windows
            ):
                continue
            ordinal = len(anchors) + 1
            selected_windows.append((start_ms, end_ms))
            anchors.append(
                {
                    "kind": "video_visual_semantic_anchor",
                    "node_id": f"video:visual-anchor:{ordinal}",
                    "role": "FIGURE",
                    "text_or_value": query,
                    "parent": "video:timeline",
                    "location": {
                        "ordinal": ordinal,
                        "timestamp_ms": timestamp_ms,
                        "stream_index": stream_index,
                    },
                    "extension": {
                        "source_kind": "VISUAL_SEMANTIC_RETRIEVAL",
                        "model_derived": True,
                        "retrieval_candidate_not_truth": True,
                        "similarity": candidate["similarity"],
                        "relative_score_margin": VIDEO_SEMANTIC_RELATIVE_SCORE_MARGIN,
                        "model_id": semantic["model_id"],
                        "model_revision": model.revision,
                        "model_identity_sha256": model.identity_sha256,
                        "policy_id": VIDEO_SEMANTIC_POLICY_ID,
                        "persistence_effect": "NONE",
                    },
                }
            )
            if len(anchors) >= MAX_VIDEO_VISUAL_ANCHORS:
                break
    return (
        anchors,
        {
            "status": "COMPLETE",
            "policy_id": VIDEO_VISUAL_SCAN_POLICY_ID,
            "coarse_candidate_count": len(coarse_timestamps),
            "decoded_frame_count": coarse_decoded_count + refinement_decoded_count,
            "unique_frame_count": len(frames),
            "duplicate_frame_count": duplicate_count,
            "refinement_frame_count": refinement_decoded_count,
            "anchor_count": len(anchors),
            "model_id": semantic["model_id"],
            "model_revision": semantic["model_revision"],
            "model_identity_sha256": semantic["model_identity_sha256"],
            "candidate_authority": "MODEL_DERIVED_RETRIEVAL_NOT_TRUTH",
            "relative_score_margin": VIDEO_SEMANTIC_RELATIVE_SCORE_MARGIN,
            "persistence_effect": "NONE",
        },
        warnings,
    )


def _extract_frame(
    source_path: str,
    target_path: str,
    *,
    stream_index: int,
    timestamp_ms: int,
) -> dict[str, object]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VideoRuntimeUnavailable("ffmpeg is unavailable")
    _run_bounded_completed(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{timestamp_ms / 1000:.3f}",
            "-i",
            source_path,
            "-map",
            f"0:{stream_index}",
            "-frames:v",
            "1",
            "-vf",
            "scale='min(3464,iw)':-2",
            "-c:v",
            "png",
            "-y",
            target_path,
        ],
        timeout=60.0,
        stdout_limit=1024,
    )
    target = Path(target_path)
    if not target.is_file() or target.stat().st_size > MAX_VIDEO_FRAME_BYTES:
        raise VideoSourceInvalid("representative video frame is invalid")
    Image = import_module("PIL.Image")
    with Image.open(target) as image:
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_VIDEO_FRAME_PIXELS:
            raise VideoSourceInvalid("representative video frame exceeds its pixel bound")
    digest = sha256(target.read_bytes()).hexdigest()
    return {
        "timestamp_ms": timestamp_ms,
        "width": width,
        "height": height,
        "mime_type": "image/png",
        "image_bytes": target.stat().st_size,
        "image_sha256": digest,
    }


def _frame_quality(frame_path: str) -> dict[str, object]:
    Image = import_module("PIL.Image")
    ImageFilter = import_module("PIL.ImageFilter")
    ImageStat = import_module("PIL.ImageStat")
    with Image.open(frame_path) as image:
        grayscale = image.convert("L")
        grayscale.thumbnail((512, 512))
        histogram = grayscale.histogram()
        pixels = max(1, sum(histogram))
        mean_luminance = float(ImageStat.Stat(grayscale).mean[0])
        sharpness = float(ImageStat.Stat(grayscale.filter(ImageFilter.FIND_EDGES)).var[0])
        dark_fraction = sum(histogram[:16]) / pixels
        light_fraction = sum(histogram[240:]) / pixels
    exposure_score = max(0.0, 1.0 - abs(mean_luminance - 127.5) / 127.5)
    sharpness_score = min(1.0, sharpness / 500.0)
    clipping_penalty = min(1.0, dark_fraction + light_fraction)
    selection_score = 0.55 * exposure_score + 0.45 * sharpness_score - 0.60 * clipping_penalty
    return {
        "mean_luminance": round(mean_luminance, 3),
        "sharpness": round(sharpness, 3),
        "dark_fraction": round(dark_fraction, 6),
        "light_fraction": round(light_fraction, 6),
        "selection_score": round(selection_score, 6),
    }


def _select_representative_candidate(
    candidates: list[dict[str, object]], *, midpoint_ms: int
) -> dict[str, object]:
    if not candidates:
        raise VideoSourceInvalid("representative frame candidates are absent")
    return max(
        candidates,
        key=lambda candidate: (
            cast(float, candidate["selection_score"]),
            -abs(cast(int, candidate["timestamp_ms"]) - midpoint_ms),
            -cast(int, candidate["timestamp_ms"]),
        ),
    )


def _frame_ocr(frame_path: str) -> list[dict[str, object]]:
    """Run the existing local macOS Vision projection over one ephemeral frame."""
    from .docling_documents import docling_macos_ocr_worker

    result = docling_macos_ocr_worker(frame_path)
    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        raise VideoSourceInvalid("frame OCR returned invalid items")
    text_items: list[dict[str, object]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        text = raw.get("text_or_value")
        if isinstance(text, str) and text.strip():
            text_items.append(raw)
    return text_items


def _normalized_ocr_region(raw: dict[str, object]) -> list[float] | None:
    extension = raw.get("extension")
    if not isinstance(extension, dict):
        return None
    visual_region = extension.get("visual_region")
    if not isinstance(visual_region, dict):
        return None
    bbox = visual_region.get("bbox")
    page_size = visual_region.get("page_size")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not isinstance(page_size, list)
        or len(page_size) != 2
        or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (*bbox, *page_size)
        )
    ):
        return None
    width, height = (float(page_size[0]), float(page_size[1]))
    if width <= 0 or height <= 0:
        return None
    left, top, right, bottom = (float(value) for value in bbox)
    normalized = [left / width, top / height, right / width, bottom / height]
    if not 0 <= normalized[0] < normalized[2] <= 1 or not 0 <= normalized[1] < normalized[3] <= 1:
        return None
    return [round(value, 6) for value in normalized]


def _ocr_confidence(raw: dict[str, object]) -> float | None:
    candidates = [raw.get("confidence")]
    extension = raw.get("extension")
    if isinstance(extension, dict):
        candidates.append(extension.get("confidence"))
    for value in candidates:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
            return round(float(value), 6)
    return None


def _ocr_text_key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _region_iou(left: object, right: object) -> float | None:
    if not (
        isinstance(left, list)
        and isinstance(right, list)
        and len(left) == len(right) == 4
        and all(isinstance(value, float) for value in (*left, *right))
    ):
        return None
    intersection_width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    intersection_height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    intersection = intersection_width * intersection_height
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else None


def _temporal_ocr_tracks(
    observations: list[dict[str, object]], *, namespace: str = ""
) -> list[dict[str, object]]:
    groups: list[list[dict[str, object]]] = []
    for observation in sorted(
        observations,
        key=lambda item: cast(int, cast(dict[str, object], item["location"])["timestamp_ms"]),
    ):
        location = cast(dict[str, object], observation["location"])
        extension = cast(dict[str, object], observation["extension"])
        timestamp_ms = cast(int, location["timestamp_ms"])
        key = cast(str, extension["normalized_text"])
        if groups:
            previous = groups[-1][-1]
            previous_location = cast(dict[str, object], previous["location"])
            previous_extension = cast(dict[str, object], previous["extension"])
            gap = timestamp_ms - cast(int, previous_location["timestamp_ms"])
            overlap = _region_iou(
                previous_extension.get("normalized_bbox"), extension.get("normalized_bbox")
            )
            compatible_region = overlap is None or overlap >= 0.25
            if (
                previous_extension.get("normalized_text") == key
                and 0 <= gap <= MAX_VIDEO_OCR_TRACK_GAP_MS
                and compatible_region
            ):
                groups[-1].append(observation)
                continue
        groups.append([observation])
    tracks: list[dict[str, object]] = []
    prefix = f"{namespace}:" if namespace else ""
    for ordinal, group in enumerate(groups, start=1):
        first = group[0]
        last = group[-1]
        first_location = cast(dict[str, object], first["location"])
        last_location = cast(dict[str, object], last["location"])
        first_extension = cast(dict[str, object], first["extension"])
        tracks.append(
            {
                "kind": "video_text_track",
                "node_id": f"video:text-track:{prefix}{ordinal}",
                "role": "PARAGRAPH",
                "text_or_value": first["text_or_value"],
                "parent": "video:timeline",
                "location": {
                    "ordinal": ordinal,
                    "start_ms": first_location["timestamp_ms"],
                    "end_ms": last_location["timestamp_ms"],
                    "stream_index": first_location["stream_index"],
                },
                "extension": {
                    "source_kind": "VIDEO_TEXT_TRACK",
                    "model_derived": True,
                    "normalized_text": first_extension["normalized_text"],
                    "member_node_ids": [item["node_id"] for item in group],
                    "observation_count": len(group),
                    "continuity": "SAMPLED_OBSERVATIONS_ONLY",
                    "embedded_subtitle_conflated": False,
                },
            }
        )
    return tracks


def _subtitle_time(value: str) -> int:
    hours, minutes, remainder = value.replace(",", ".").split(":")
    seconds, milliseconds = remainder.split(".")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(milliseconds)
    )


def extract_embedded_subtitles(
    source_path: str,
    *,
    streams: list[dict[str, object]],
    start_ms: int,
    end_ms: int,
) -> tuple[list[dict[str, object]], list[str]]:
    """Extract bounded text subtitle tracks while preserving stream identity."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VideoRuntimeUnavailable("ffmpeg is unavailable")
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    items: list[dict[str, object]] = []
    warnings: list[str] = []
    for stream in subtitle_streams[:MAX_VIDEO_SUBTITLE_STREAMS]:
        stream_index = stream.get("index")
        if type(stream_index) is not int:
            continue
        try:
            completed = _run_bounded_completed(
                [
                    ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    source_path,
                    "-map",
                    f"0:{stream_index}",
                    "-f",
                    "srt",
                    "-",
                ],
                timeout=60.0,
                stdout_limit=MAX_VIDEO_SUBTITLE_BYTES,
            )
        except (VideoRuntimeUnavailable, VideoSourceInvalid):
            warnings.append(f"VIDEO_SUBTITLE_STREAM_UNAVAILABLE:{stream_index}")
            continue
        text = completed.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
        for match in _SRT_CUE.finditer(text):
            cue_start = _subtitle_time(match.group("start"))
            cue_end = _subtitle_time(match.group("end"))
            if cue_end <= start_ms or cue_start >= end_ms or cue_end <= cue_start:
                continue
            cue_text = " ".join(line.strip() for line in match.group("text").splitlines()).strip()
            if not cue_text:
                continue
            ordinal = len(items) + 1
            items.append(
                {
                    "kind": "video_embedded_subtitle_cue",
                    "node_id": f"video:subtitle:{stream_index}:{ordinal}",
                    "role": "PARAGRAPH",
                    "text_or_value": cue_text,
                    "parent": "video:timeline",
                    "location": {
                        "start_ms": max(start_ms, cue_start),
                        "end_ms": min(end_ms, cue_end),
                        "stream_index": stream_index,
                        "ordinal": ordinal,
                    },
                    "extension": {
                        "source_kind": "EMBEDDED_SUBTITLE",
                        "language": stream.get("language"),
                        "codec_name": stream.get("codec_name"),
                        "model_derived": False,
                    },
                }
            )
            if len(items) >= MAX_VIDEO_SUBTITLE_CUES:
                warnings.append("VIDEO_SUBTITLE_CUE_LIMIT_REACHED")
                return items, warnings
    if len(subtitle_streams) > MAX_VIDEO_SUBTITLE_STREAMS:
        warnings.append("VIDEO_SUBTITLE_STREAM_LIMIT_REACHED")
    return items, warnings


def _query_windows(
    items: list[dict[str, object]],
    *,
    query: str | None,
    window_start_ms: int,
    window_end_ms: int,
) -> list[dict[str, object]]:
    if not query:
        return []
    folded = query.casefold()
    matches = [
        item
        for item in items
        if isinstance(item.get("text_or_value"), str)
        and folded in cast(str, item["text_or_value"]).casefold()
    ]
    windows: list[dict[str, object]] = []
    for ordinal, match in enumerate(matches[:20], start=1):
        location = match.get("location")
        if not isinstance(location, dict):
            continue
        match_start = location.get("start_ms", location.get("timestamp_ms"))
        match_end = location.get("end_ms", match_start)
        if type(match_start) is not int or type(match_end) is not int:
            continue
        start_ms = max(window_start_ms, match_start - VIDEO_QUERY_PADDING_MS)
        end_ms = min(window_end_ms, match_end + VIDEO_QUERY_PADDING_MS)
        joined: list[str] = []
        source_kinds: set[str] = set()
        for candidate in items:
            candidate_location = candidate.get("location")
            if not isinstance(candidate_location, dict):
                continue
            left = candidate_location.get("start_ms", candidate_location.get("timestamp_ms"))
            right = candidate_location.get("end_ms", left)
            node_id = candidate.get("node_id")
            if type(left) is int and type(right) is int and left <= end_ms and right >= start_ms:
                if isinstance(node_id, str):
                    joined.append(node_id)
                extension = candidate.get("extension")
                if isinstance(extension, dict) and isinstance(extension.get("source_kind"), str):
                    source_kinds.add(extension["source_kind"])
        match_extension = match.get("extension")
        match_source = (
            match_extension.get("source_kind") if isinstance(match_extension, dict) else None
        )
        windows.append(
            {
                "kind": "video_query_window",
                "node_id": f"video:query-window:{ordinal}",
                "role": "SECTION",
                "text_or_value": cast(str, match["text_or_value"]),
                "parent": "video:timeline",
                "location": {"ordinal": ordinal, "start_ms": start_ms, "end_ms": end_ms},
                "extension": {
                    "source_kind": "DERIVED_QUERY_WINDOW",
                    "matched_node_id": match.get("node_id"),
                    "matched_source_kind": match_source,
                    "joined_node_ids": sorted(set(joined)),
                    "represented_source_kinds": sorted(source_kinds),
                    "semantic_agreement_inferred": False,
                },
            }
        )
    return windows


def _decode_windows_from_query_items(
    items: list[dict[str, object]],
    *,
    query: str | None,
    window_start_ms: int,
    window_end_ms: int,
) -> tuple[list[tuple[int, int]], tuple[str, ...]]:
    query_items = _query_windows(
        items,
        query=query,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
    )
    raw_windows: list[tuple[int, int]] = []
    sources: set[str] = set()
    for item in query_items:
        location = item.get("location")
        extension = item.get("extension")
        if isinstance(location, dict):
            start_ms = location.get("start_ms")
            end_ms = location.get("end_ms")
            if type(start_ms) is int and type(end_ms) is int and end_ms > start_ms:
                raw_windows.append((start_ms, end_ms))
        if isinstance(extension, dict) and isinstance(extension.get("matched_source_kind"), str):
            sources.add(extension["matched_source_kind"])
    coalesced: list[tuple[int, int]] = []
    for start_ms, end_ms in sorted(set(raw_windows)):
        if coalesced and start_ms <= coalesced[-1][1]:
            coalesced[-1] = (coalesced[-1][0], max(coalesced[-1][1], end_ms))
        else:
            coalesced.append((start_ms, end_ms))
    return coalesced[:MAX_VIDEO_QUERY_DECODE_WINDOWS], tuple(sorted(sources))


@dataclass(frozen=True, slots=True)
class VideoSceneWorker:
    """Picklable bounded scene, representative-frame and optional OCR worker."""

    source_format: str
    start_ms: int = 0
    include_ocr: bool = False
    request_digest: str = ""
    source_sha256: str = ""
    window_end_ms: int | None = None
    node_namespace: str = ""
    representative_frame_limit: int = MAX_VIDEO_REPRESENTATIVE_FRAMES
    ocr_frame_limit: int = MAX_VIDEO_OCR_FRAMES

    def __call__(self, source_path: str) -> dict[str, Any]:
        probe = probe_video(source_path, self.source_format)
        duration_ms = probe["duration_ms"]
        stream_index = probe["primary_video_stream_index"]
        if type(duration_ms) is not int or type(stream_index) is not int:
            raise VideoSourceInvalid("video projection is invalid")
        if self.start_ms < 0 or self.start_ms >= duration_ms:
            raise VideoSourceInvalid("video window start is outside the source")
        end_ms = min(
            duration_ms,
            self.window_end_ms
            if self.window_end_ms is not None
            else self.start_ms + MAX_VIDEO_WINDOW_MS,
        )
        if end_ms <= self.start_ms or end_ms - self.start_ms > MAX_VIDEO_WINDOW_MS:
            raise VideoSourceInvalid("video window end is outside the source")
        scenes = detect_video_scenes(
            source_path,
            stream_index=stream_index,
            start_ms=self.start_ms,
            end_ms=end_ms,
        )
        items = _items(probe)
        warnings: list[str] = []
        decoded_frames = 0
        candidate_frames = 0
        ocr_frames = 0
        ocr_items = 0
        ocr_observations: list[dict[str, object]] = []
        selected_scenes = _select_scene_coverage(
            scenes, limit=self.representative_frame_limit
        )
        with TemporaryDirectory(prefix="steward-video-scenes-") as temporary:
            for scene in selected_scenes:
                ordinal_raw = scene["ordinal"]
                scene_start_raw = scene["start_ms"]
                scene_end_raw = scene["end_ms"]
                if not all(
                    type(value) is int
                    for value in (ordinal_raw, scene_start_raw, scene_end_raw)
                ):
                    raise VideoSourceInvalid("scene projection is invalid")
                ordinal = cast(int, ordinal_raw)
                scene_start = cast(int, scene_start_raw)
                scene_end = cast(int, scene_end_raw)
                midpoint_ms = scene_start + (scene_end - scene_start) // 2
                candidates: list[dict[str, object]] = []
                for candidate_index, candidate_timestamp_ms in enumerate(
                    _representative_timestamps(scene_start, scene_end), start=1
                ):
                    candidate_path = str(
                        Path(temporary) / f"scene-{ordinal:03d}-{candidate_index}.png"
                    )
                    candidate_frame = _extract_frame(
                        source_path,
                        candidate_path,
                        stream_index=stream_index,
                        timestamp_ms=candidate_timestamp_ms,
                    )
                    candidates.append(
                        {
                            **candidate_frame,
                            **_frame_quality(candidate_path),
                            "frame_path": candidate_path,
                        }
                    )
                    decoded_frames += 1
                    candidate_frames += 1
                selected = _select_representative_candidate(
                    candidates,
                    midpoint_ms=midpoint_ms,
                )
                timestamp_ms = cast(int, selected["timestamp_ms"])
                frame_path = cast(str, selected["frame_path"])
                frame = {key: value for key, value in selected.items() if key != "frame_path"}
                namespace = f"{self.node_namespace}:" if self.node_namespace else ""
                scene_node = f"video:scene:{namespace}{ordinal}"
                items.append(
                    {
                        "kind": "video_scene",
                        "node_id": scene_node,
                        "role": "SECTION",
                        "text_or_value": None,
                        "parent": "video:timeline",
                        "location": {
                            "ordinal": ordinal,
                            "start_ms": scene_start,
                            "end_ms": scene_end,
                            "stream_index": stream_index,
                        },
                        "extension": {
                            "boundary_score": scene["boundary_score"],
                            "boundary_detector": scene["boundary_detector"],
                            "boundary_reason": scene["boundary_reason"],
                            "scene_policy_id": VIDEO_SCENE_POLICY_ID,
                            "coverage_policy": "UNIFORM_ACROSS_DETECTED_SCENES",
                        },
                    }
                )
                items.append(
                    {
                        "kind": "video_representative_frame",
                        "node_id": f"{scene_node}:frame",
                        "role": "FIGURE",
                        "text_or_value": None,
                        "parent": scene_node,
                        "location": {
                            "timestamp_ms": timestamp_ms,
                            "stream_index": stream_index,
                        },
                        "extension": {
                            **frame,
                            "source_kind": "VIDEO_REPRESENTATIVE_FRAME",
                            "selection_policy_id": VIDEO_FRAME_SELECTION_POLICY_ID,
                            "candidate_count": len(candidates),
                            "persistence_effect": "NONE",
                        },
                    }
                )
                if self.include_ocr and ocr_frames < self.ocr_frame_limit:
                    try:
                        frame_text = _frame_ocr(frame_path)
                    except Exception:
                        warnings.append(f"VIDEO_FRAME_OCR_UNAVAILABLE:{ordinal}")
                        continue
                    ocr_frames += 1
                    for item_index, raw in enumerate(frame_text, start=1):
                        text = raw.get("text_or_value")
                        if not isinstance(text, str) or not text.strip():
                            continue
                        extension: dict[str, object] = {
                            "source_kind": "FRAME_OCR",
                            "model_derived": True,
                            "timestamp_accuracy": "DECODE_SEEK_APPROXIMATE",
                            "normalized_text": _ocr_text_key(text),
                        }
                        normalized_region = _normalized_ocr_region(raw)
                        if normalized_region is not None:
                            extension["normalized_bbox"] = normalized_region
                            extension["coordinate_space"] = "NORMALIZED_FRAME_TOP_LEFT"
                        confidence = _ocr_confidence(raw)
                        if confidence is not None:
                            extension["confidence"] = confidence
                        observation: dict[str, object] = {
                            "kind": "video_frame_ocr_text",
                            "node_id": f"{scene_node}:ocr:{item_index}",
                            "role": "PARAGRAPH",
                            "text_or_value": text.strip(),
                            "parent": f"{scene_node}:frame",
                            "location": {
                                "timestamp_ms": timestamp_ms,
                                "stream_index": stream_index,
                                "ordinal": item_index,
                            },
                            "extension": extension,
                        }
                        items.append(observation)
                        ocr_observations.append(observation)
                        ocr_items += 1
        ocr_tracks = _temporal_ocr_tracks(
            ocr_observations,
            namespace=self.node_namespace,
        )
        items.extend(ocr_tracks)
        if len(scenes) > self.representative_frame_limit:
            warnings.append("VIDEO_REPRESENTATIVE_FRAME_LIMIT_REACHED")
        if len(scenes) >= MAX_VIDEO_SCENES and end_ms < duration_ms:
            warnings.append("VIDEO_SCENE_LIMIT_REACHED")
        coverage_report = _video_coverage_report(
            items,
            window_start_ms=self.start_ms,
            window_end_ms=end_ms,
            detected_scene_count=len(scenes),
            selected_frame_count=len(selected_scenes),
            ocr_frame_count=ocr_frames,
            include_ocr=self.include_ocr,
        )
        items.insert(
            1,
            _video_analysis_summary_item(
                coverage_report,
                window_start_ms=self.start_ms,
                window_end_ms=end_ms,
            ),
        )
        continuation = (
            {
                "schema_name": VIDEO_CONTINUATION_SCHEMA_NAME,
                "schema_version": VIDEO_TIME_CONTINUATION_SCHEMA_VERSION,
                "request_digest": self.request_digest,
                "source_sha256": self.source_sha256,
                "next_start_ms": end_ms,
            }
            if end_ms < duration_ms
            else None
        )
        return {
            "backend_name": "FFmpeg",
            "backend_version": _ffprobe_version(shutil.which("ffprobe") or "ffprobe"),
            "warnings": warnings,
            "items": items,
            "resource_extension": {
                "media_kind": "VIDEO",
                "duration_ms": duration_ms,
                "window_start_ms": self.start_ms,
                "window_end_ms": end_ms,
                "video_request_digest": self.request_digest,
                "scene_policy_id": VIDEO_SCENE_POLICY_ID,
                "frame_selection_policy_id": VIDEO_FRAME_SELECTION_POLICY_ID,
                "scene_count": len(scenes),
                "decoded_frame_count": decoded_frames,
                "candidate_frame_count": candidate_frames,
                "selected_frame_count": len(selected_scenes),
                "ocr_frame_count": ocr_frames,
                "ocr_item_count": ocr_items,
                "ocr_track_count": len(ocr_tracks),
                "representative_frame_limit": self.representative_frame_limit,
                "scene_coverage": {
                    "detected_scene_count": len(scenes),
                    "selected_ordinals": [scene["ordinal"] for scene in selected_scenes],
                    "policy": "UNIFORM_ACROSS_DETECTED_SCENES",
                },
                "ocr_frame_limit": self.ocr_frame_limit,
                "coverage_report": coverage_report,
                "persistence_effect": "NONE",
            },
            "continuation": continuation,
        }


@dataclass(frozen=True, slots=True)
class VideoTimelineWorker:
    """Join scene, subtitle and base-ASR facts without collapsing provenance."""

    source_format: str
    start_ms: int
    include_ocr: bool
    request_digest: str
    source_sha256: str
    content_query: str | None = None
    audio_language: str | None = None

    def __call__(self, source_path: str) -> dict[str, Any]:
        probe = probe_video(source_path, self.source_format)
        duration_ms = probe.get("duration_ms")
        streams = probe.get("streams")
        if type(duration_ms) is not int or not isinstance(streams, list):
            raise VideoSourceInvalid("video stream projection is invalid")
        if self.start_ms < 0 or self.start_ms >= duration_ms:
            raise VideoSourceInvalid("video window start is outside the source")
        window_start_ms = self.start_ms
        window_end_ms = min(duration_ms, self.start_ms + MAX_VIDEO_WINDOW_MS)
        typed_streams = cast(list[dict[str, object]], streams)
        items = _items(probe)
        warnings: list[str] = []
        query_range_start = 0 if self.content_query else window_start_ms
        query_range_end = duration_ms if self.content_query else window_end_ms
        subtitle_items, subtitle_warnings = extract_embedded_subtitles(
            source_path,
            streams=typed_streams,
            start_ms=query_range_start,
            end_ms=query_range_end,
        )
        items.extend(subtitle_items)
        warnings.extend(subtitle_warnings)
        subtitle_decode_windows, _subtitle_sources = _decode_windows_from_query_items(
            subtitle_items,
            query=self.content_query,
            window_start_ms=query_range_start,
            window_end_ms=query_range_end,
        )
        visual_anchors: list[dict[str, object]] = []
        visual_search: dict[str, object] | None = None
        primary_video_stream_index = probe.get("primary_video_stream_index")
        if self.content_query and subtitle_decode_windows:
            visual_search = {
                "status": "NOT_NEEDED",
                "reason": "TEXT_ANCHOR_PRESENT",
                "policy_id": VIDEO_VISUAL_SCAN_POLICY_ID,
                "persistence_effect": "NONE",
            }
        elif self.content_query and type(primary_video_stream_index) is int:
            try:
                visual_anchors, visual_search, visual_warnings = _visual_semantic_anchors(
                    source_path,
                    stream_index=primary_video_stream_index,
                    duration_ms=duration_ms,
                    query=self.content_query,
                )
                items.extend(visual_anchors)
                warnings.extend(visual_warnings)
            except (VideoSemanticUnavailable, VideoSourceInvalid, ValueError):
                visual_search = {
                    "status": "UNAVAILABLE",
                    "runtime": video_semantic_runtime_capabilities(),
                }
                warnings.append("VIDEO_VISUAL_SEMANTIC_RUNTIME_UNAVAILABLE")
        audio_streams = [stream for stream in typed_streams if stream.get("codec_type") == "audio"]
        visual_decode_windows = _semantic_windows_from_anchors(
            visual_anchors, duration_ms=duration_ms
        )
        anchored_audio_windows = sorted(
            set([*subtitle_decode_windows, *visual_decode_windows])
        )[:MAX_VIDEO_QUERY_DECODE_WINDOWS]
        audio_windows = (
            anchored_audio_windows
            if self.content_query and anchored_audio_windows
            else [(window_start_ms, window_end_ms)]
        )
        audio_diagnostic_windows: list[dict[str, object]] = []
        if audio_streams:
            for audio_window_index, (audio_start_ms, audio_end_ms) in enumerate(
                audio_windows, start=1
            ):
                try:
                    audio_items, window_diagnostics = transcribe_media_window(
                        source_path,
                        start_ms=audio_start_ms,
                        end_ms=audio_end_ms,
                        language=self.audio_language,
                    )
                    for audio_item in audio_items:
                        node_id = audio_item.get("node_id")
                        if isinstance(node_id, str) and len(audio_windows) > 1:
                            audio_item["node_id"] = (
                                f"video:audio-window:{audio_window_index}:{node_id}"
                            )
                        items.append(audio_item)
                    audio_diagnostic_windows.append(
                        {
                            "start_ms": audio_start_ms,
                            "end_ms": audio_end_ms,
                            "diagnostics": window_diagnostics,
                        }
                    )
                except AudioRuntimeUnavailable:
                    warnings.append("VIDEO_AUDIO_ASR_RUNTIME_UNAVAILABLE")
                    break
                except OSError:
                    warnings.append("VIDEO_AUDIO_ASR_SOURCE_UNAVAILABLE")
                    break
        else:
            warnings.append("VIDEO_AUDIO_STREAM_ABSENT")

        decode_windows, anchor_sources = _decode_windows_from_query_items(
            items,
            query=self.content_query,
            window_start_ms=query_range_start,
            window_end_ms=query_range_end,
        )
        if self.content_query and decode_windows:
            decode_reason = (
                "QUERY_MULTIMODAL_ANCHOR"
                if "VISUAL_SEMANTIC_RETRIEVAL" in anchor_sources
                else "QUERY_TEXT_ANCHOR"
            )
        elif self.content_query:
            decode_windows = [(window_start_ms, window_end_ms)]
            decode_reason = "NO_TEXT_ANCHOR_FALLBACK"
            warnings.append("VIDEO_QUERY_TEXT_ANCHOR_ABSENT")
        else:
            decode_windows = [(window_start_ms, window_end_ms)]
            decode_reason = "BROAD_READ"

        structural_kinds = {
            "video_document",
            "video_analysis_summary",
            "video_video_stream",
            "video_audio_stream",
            "video_subtitle_stream",
            "video_chapter",
        }
        scene_resources: list[dict[str, object]] = []
        remaining_frames = MAX_VIDEO_REPRESENTATIVE_FRAMES
        remaining_ocr_frames = MAX_VIDEO_OCR_FRAMES
        for decode_index, (decode_start_ms, decode_end_ms) in enumerate(
            decode_windows, start=1
        ):
            if remaining_frames <= 0:
                warnings.append("VIDEO_REPRESENTATIVE_FRAME_LIMIT_REACHED")
                break
            scene_result = VideoSceneWorker(
                self.source_format,
                decode_start_ms,
                self.include_ocr,
                self.request_digest,
                self.source_sha256,
                decode_end_ms,
                f"window-{decode_index}",
                remaining_frames,
                remaining_ocr_frames,
            )(source_path)
            scene_items_raw = scene_result.get("items")
            resources_raw = scene_result.get("resource_extension")
            warnings_raw = scene_result.get("warnings")
            if (
                not isinstance(scene_items_raw, list)
                or not isinstance(resources_raw, dict)
                or not isinstance(warnings_raw, list)
            ):
                raise VideoSourceInvalid("scene projection is invalid")
            for scene_item in scene_items_raw:
                if isinstance(scene_item, dict) and scene_item.get("kind") not in structural_kinds:
                    items.append(scene_item)
            resources = cast(dict[str, object], resources_raw)
            scene_resources.append(resources)
            warnings.extend(
                warning for warning in warnings_raw if isinstance(warning, str)
            )
            selected = resources.get("selected_frame_count")
            used_ocr = resources.get("ocr_frame_count")
            if type(selected) is int:
                remaining_frames -= selected
            if type(used_ocr) is int:
                remaining_ocr_frames -= used_ocr

        query_windows = _query_windows(
            items,
            query=self.content_query,
            window_start_ms=query_range_start,
            window_end_ms=query_range_end,
        )
        items.extend(query_windows)
        decoded_ms = sum(end_ms - start_ms for start_ms, end_ms in decode_windows)
        continuation = (
            {
                "schema_name": VIDEO_CONTINUATION_SCHEMA_NAME,
                "schema_version": VIDEO_TIME_CONTINUATION_SCHEMA_VERSION,
                "request_digest": self.request_digest,
                "source_sha256": self.source_sha256,
                "next_start_ms": window_end_ms,
            }
            if window_end_ms < duration_ms
            else None
        )

        def resource_total(field: str) -> int:
            return sum(
                cast(int, resource[field])
                for resource in scene_resources
                if type(resource.get(field)) is int
            )

        audio_diagnostics: dict[str, object] | None = (
            {
                "window_count": len(audio_diagnostic_windows),
                "windows": audio_diagnostic_windows,
            }
            if audio_diagnostic_windows
            else None
        )
        scene_count = resource_total("scene_count")
        selected_frame_count = resource_total("selected_frame_count")
        ocr_frame_count = resource_total("ocr_frame_count")
        coverage_report = _video_coverage_report(
            items,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            detected_scene_count=scene_count,
            selected_frame_count=selected_frame_count,
            ocr_frame_count=ocr_frame_count,
            include_ocr=self.include_ocr,
        )
        items.insert(
            1,
            _video_analysis_summary_item(
                coverage_report,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
            ),
        )
        return {
            "backend_name": "FFmpegMultimodal",
            "backend_version": ffmpeg_runtime_version(),
            "warnings": list(dict.fromkeys(warnings)),
            "items": items,
            "resource_extension": {
                "media_kind": "VIDEO",
                "duration_ms": duration_ms,
                "window_start_ms": window_start_ms,
                "window_end_ms": window_end_ms,
                "video_request_digest": self.request_digest,
                "scene_policy_id": VIDEO_SCENE_POLICY_ID,
                "frame_selection_policy_id": VIDEO_FRAME_SELECTION_POLICY_ID,
                "scene_count": scene_count,
                "decoded_frame_count": resource_total("decoded_frame_count"),
                "candidate_frame_count": resource_total("candidate_frame_count"),
                "selected_frame_count": selected_frame_count,
                "ocr_frame_count": ocr_frame_count,
                "ocr_item_count": resource_total("ocr_item_count"),
                "ocr_track_count": resource_total("ocr_track_count"),
                "video_analysis": (
                    "MULTIMODAL_AND_OCR" if self.include_ocr else "MULTIMODAL"
                ),
                "subtitle_cue_count": len(subtitle_items),
                "audio_stream_count": len(audio_streams),
                "audio_asr": audio_diagnostics,
                "visual_search": visual_search,
                "query_window_count": len(query_windows),
                "decode_plan": {
                    "reason": decode_reason,
                    "anchor_source_kinds": list(anchor_sources),
                    "windows": [
                        {"start_ms": start_ms, "end_ms": end_ms}
                        for start_ms, end_ms in decode_windows
                    ],
                    "decoded_ms": decoded_ms,
                    "avoided_ms": (query_range_end - query_range_start) - decoded_ms,
                    "window_limit": MAX_VIDEO_QUERY_DECODE_WINDOWS,
                },
                "modalities": {
                    "SCENE": True,
                    "REPRESENTATIVE_FRAME": True,
                    "FRAME_OCR": self.include_ocr,
                    "VIDEO_TEXT_TRACK": resource_total("ocr_track_count") > 0,
                    "EMBEDDED_SUBTITLE": bool(subtitle_items),
                    "AUDIO_ASR": bool(audio_diagnostic_windows),
                    **(
                        {"VISUAL_SEMANTIC_RETRIEVAL": True}
                        if visual_anchors
                        else {}
                    ),
                },
                "coverage_report": coverage_report,
                "semantic_agreement_inferred": False,
                "persistence_effect": "NONE",
            },
            "continuation": continuation,
        }


@dataclass(frozen=True, slots=True)
class VideoProbeWorker:
    """Picklable worker for probe-only NEXT-024B inspection."""

    source_format: str

    def __call__(self, source_path: str) -> dict[str, Any]:
        probe = probe_video(source_path, self.source_format)
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            raise VideoRuntimeUnavailable("ffprobe is unavailable")
        streams = probe.get("streams")
        chapters = probe.get("chapters")
        if not isinstance(streams, list) or not isinstance(chapters, list):
            raise VideoSourceInvalid("video projection is invalid")
        return {
            "backend_name": "FFprobe",
            "backend_version": _ffprobe_version(ffprobe),
            "warnings": [],
            "items": _items(probe),
            "resource_extension": {
                "media_kind": "VIDEO",
                "duration_ms": probe["duration_ms"],
                "container": probe["container"],
                "container_start_ms": probe["container_start_ms"],
                "stream_count": len(streams),
                "chapter_count": len(chapters),
                "primary_video_stream_index": probe["primary_video_stream_index"],
                "primary_audio_stream_index": probe["primary_audio_stream_index"],
                "primary_subtitle_stream_index": probe["primary_subtitle_stream_index"],
                "track_selection_policy_id": probe["track_selection_policy_id"],
                "timeline_authority": "SOURCE_PRESENTATION_TIMESTAMPS",
                "decoded_frame_count": 0,
                "decoded_audio_bytes": 0,
                "persistence_effect": "NONE",
            },
        }


__all__ = [
    "MAX_VIDEO_SOURCE_BYTES",
    "MAX_VIDEO_WINDOW_MS",
    "VIDEO_FORMAT_BY_SUFFIX",
    "VIDEO_CONTINUATION_SCHEMA_NAME",
    "VIDEO_FRAME_SELECTION_POLICY_ID",
    "VIDEO_RESULT_PAGE_CONTINUATION_SCHEMA_VERSION",
    "VIDEO_RESULT_MODALITY_QUOTAS",
    "VIDEO_RESULT_PRESENTATION_POLICY_ID",
    "VIDEO_SOURCE_FORMATS",
    "VIDEO_SUFFIX_BY_FORMAT",
    "VIDEO_TIME_CONTINUATION_SCHEMA_VERSION",
    "VIDEO_TRACK_SELECTION_POLICY_ID",
    "VideoProbeWorker",
    "VideoSceneWorker",
    "VideoTimelineWorker",
    "VideoRuntimeUnavailable",
    "probe_video",
    "detect_video_scenes",
    "ffmpeg_runtime_version",
    "extract_embedded_subtitles",
    "video_runtime_capabilities",
    "video_request_digest",
]
