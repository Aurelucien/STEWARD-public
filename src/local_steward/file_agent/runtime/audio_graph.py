"""Operation-scoped immutable audio timeline graph."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from ...evidence import canonical_json


AUDIO_GRAPH_SCHEMA_NAME = "local_steward.audio_document_graph"
AUDIO_GRAPH_SCHEMA_VERSION = 1
_KIND_ORDER = {
    "audio_source": 0,
    "audio_speech_region": 1,
    "audio_speaker_turn": 2,
    "audio_overlap_region": 3,
    "audio_transcript_segment": 4,
    "audio_sentence_group": 5,
    "audio_aligned_word": 6,
    "audio_approximate_word": 7,
    "audio_unaligned_word": 8,
}
_PUBLIC_KIND = {
    "audio_source": "audio_metadata",
    "audio_speech_region": "audio_speech_interval",
}
_PUBLIC_DEFAULT_KINDS = frozenset(
    {"audio_source", "audio_speech_region", "audio_transcript_segment"}
)


def _primitive(value: object) -> str | int | float | bool | None:
    return value if isinstance(value, (str, int, float, bool)) else None


@dataclass(frozen=True, slots=True)
class AudioGraphNode:
    """One source-relative audio observation with immutable scalar metadata."""

    node_id: str
    kind: str
    role: str
    start_ms: int
    end_ms: int
    ordinal: int
    value: str | None = None
    parent: str | None = None
    extension: tuple[tuple[str, str | int | float | bool | None], ...] = ()

    def __post_init__(self) -> None:
        if not self.node_id or self.kind not in _KIND_ORDER or not self.role:
            raise ValueError("audio graph node identity is invalid")
        if (
            type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or self.start_ms < 0
            or self.end_ms <= self.start_ms
            or type(self.ordinal) is not int
            or self.ordinal < 0
        ):
            raise ValueError("audio graph node range is invalid")
        keys = [key for key, _value in self.extension]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("audio graph node extension must be unique and sorted")

    def payload(self, *, public: bool = False) -> dict[str, object]:
        value: dict[str, object] = {
            "kind": _PUBLIC_KIND.get(self.kind, self.kind) if public else self.kind,
            "node_id": self.node_id,
            "role": self.role,
            "location": {
                "start_ms": self.start_ms,
                "end_ms": self.end_ms,
                "ordinal": self.ordinal,
            },
        }
        if self.value is not None:
            value["text_or_value"] = self.value
        if self.parent is not None:
            value["parent"] = self.parent
        if self.extension:
            value["extension"] = dict(self.extension)
        return value


@dataclass(frozen=True, slots=True)
class AudioDocumentGraph:
    """One bounded source-global timeline; never persisted by the runtime."""

    source_sha256: str
    duration_ms: int
    window_start_ms: int
    window_end_ms: int
    nodes: tuple[AudioGraphNode, ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.source_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.source_sha256
        ):
            raise ValueError("audio graph source identity is invalid")
        if (
            type(self.duration_ms) is not int
            or self.duration_ms <= 0
            or type(self.window_start_ms) is not int
            or type(self.window_end_ms) is not int
            or not 0 <= self.window_start_ms < self.window_end_ms <= self.duration_ms
        ):
            raise ValueError("audio graph window is invalid")
        identifiers = [node.node_id for node in self.nodes]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("audio graph node IDs are not unique")
        if self.nodes != tuple(sorted(self.nodes, key=_node_order)):
            raise ValueError("audio graph nodes are not deterministically ordered")
        if any(node.end_ms > self.duration_ms for node in self.nodes):
            raise ValueError("audio graph node is outside the source")

    @property
    def graph_digest(self) -> str:
        return sha256(canonical_json(self.payload())).hexdigest()

    def payload(self) -> dict[str, object]:
        return {
            "schema_name": AUDIO_GRAPH_SCHEMA_NAME,
            "schema_version": AUDIO_GRAPH_SCHEMA_VERSION,
            "source_sha256": self.source_sha256,
            "duration_ms": self.duration_ms,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "nodes": [node.payload() for node in self.nodes],
            "warnings": list(self.warnings),
            "persistence_effect": "NONE",
        }

    def summary(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for node in self.nodes:
            counts[node.kind] = counts.get(node.kind, 0) + 1
        return {
            "schema_name": AUDIO_GRAPH_SCHEMA_NAME,
            "schema_version": AUDIO_GRAPH_SCHEMA_VERSION,
            "graph_digest": self.graph_digest,
            "node_count": len(self.nodes),
            "node_counts": dict(sorted(counts.items())),
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "persistence_effect": "NONE",
        }

    def public_items(
        self,
        *,
        include_words: bool = False,
        include_sentences: bool = False,
        include_speakers: bool = False,
    ) -> list[dict[str, object]]:
        kinds = set(_PUBLIC_DEFAULT_KINDS)
        if include_words:
            kinds.update({"audio_aligned_word", "audio_approximate_word", "audio_unaligned_word"})
        if include_sentences:
            kinds.add("audio_sentence_group")
        if include_speakers:
            kinds.update({"audio_speaker_turn", "audio_overlap_region"})
        return [node.payload(public=True) for node in self.nodes if node.kind in kinds]


def _node_order(node: AudioGraphNode) -> tuple[int, int, int, int, str]:
    return (
        node.start_ms,
        node.end_ms,
        _KIND_ORDER[node.kind],
        node.ordinal,
        node.node_id,
    )


def _extension(**values: object) -> tuple[tuple[str, str | int | float | bool | None], ...]:
    return tuple(
        sorted((key, _primitive(value)) for key, value in values.items() if value is not None)
    )


def _bounded_range(start: object, end: object, *, duration_ms: int) -> tuple[int, int] | None:
    if type(start) is not int or type(end) is not int:
        return None
    bounded_start = max(0, min(start, duration_ms - 1))
    bounded_end = max(bounded_start + 1, min(end, duration_ms))
    return bounded_start, bounded_end


def _sentence_nodes(segments: list[AudioGraphNode]) -> Iterable[AudioGraphNode]:
    group: list[AudioGraphNode] = []
    ordinal = 0
    terminal = (".", "!", "?", "。", "！", "？")
    for segment in segments:
        if group and segment.start_ms - group[-1].end_ms > 1_000:
            ordinal += 1
            yield _sentence_group(group, ordinal, "PAUSE_GT_1000MS")
            group = []
        group.append(segment)
        if (segment.value or "").rstrip().endswith(terminal):
            ordinal += 1
            yield _sentence_group(group, ordinal, "TERMINAL_PUNCTUATION")
            group = []
    if group:
        ordinal += 1
        yield _sentence_group(group, ordinal, "WINDOW_END")


def _sentence_group(segments: list[AudioGraphNode], ordinal: int, boundary: str) -> AudioGraphNode:
    text = " ".join(value for item in segments if (value := (item.value or "").strip()))
    return AudioGraphNode(
        f"audio:sentence:{ordinal:04d}",
        "audio_sentence_group",
        "SECTION",
        segments[0].start_ms,
        segments[-1].end_ms,
        ordinal,
        text or None,
        "audio:timeline",
        _extension(
            authority="MODEL_DERIVED",
            grouping_method="PUNCTUATION_AND_PAUSE",
            boundary_reason=boundary,
            segment_count=len(segments),
        ),
    )


def build_audio_document_graph(
    *,
    source_sha256: str,
    duration_ms: int,
    window_start_ms: int,
    window_end_ms: int,
    probe: dict[str, object],
    intervals: list[dict[str, int]],
    segments: list[dict[str, object]],
    model_id: str | None,
    model_identity_sha256: str | None,
    speaker_turns: list[dict[str, object]] | None = None,
    overlap_regions: list[dict[str, object]] | None = None,
    diarization_model_identity_sha256: str | None = None,
) -> AudioDocumentGraph:
    """Build one deterministic graph from existing probe/VAD/ASR observations."""
    nodes: list[AudioGraphNode] = [
        AudioGraphNode(
            "audio:source",
            "audio_source",
            "METADATA",
            0,
            duration_ms,
            0,
            None,
            None,
            _extension(authority="OBSERVED", **probe),
        )
    ]
    for ordinal, interval in enumerate(intervals, start=1):
        interval_start = interval.get("start_ms")
        interval_end = interval.get("end_ms")
        if type(interval_start) is not int or type(interval_end) is not int:
            continue
        bounds = _bounded_range(
            window_start_ms + interval_start,
            window_start_ms + interval_end,
            duration_ms=duration_ms,
        )
        if bounds is None:
            continue
        nodes.append(
            AudioGraphNode(
                f"audio:speech:{ordinal:04d}",
                "audio_speech_region",
                "STRUCTURE",
                *bounds,
                ordinal,
                None,
                "audio:timeline",
                _extension(
                    authority="MODEL_DERIVED",
                    detector="Silero VAD",
                    timestamp_accuracy="MODEL_APPROXIMATE",
                ),
            )
        )
    for ordinal, turn in enumerate(speaker_turns or [], start=1):
        bounds = _bounded_range(turn.get("start_ms"), turn.get("end_ms"), duration_ms=duration_ms)
        label = turn.get("speaker_label")
        if bounds is None or not isinstance(label, str):
            continue
        nodes.append(
            AudioGraphNode(
                f"audio:speaker-turn:{ordinal:04d}",
                "audio_speaker_turn",
                "STRUCTURE",
                *bounds,
                ordinal,
                label,
                "audio:timeline",
                _extension(
                    authority="MODEL_DERIVED",
                    backend="sherpa-onnx-offline-speaker-diarization",
                    model_identity_sha256=diarization_model_identity_sha256,
                    speaker_label=label,
                    identity_scope="FILE_LOCAL_ANONYMOUS",
                ),
            )
        )
    for ordinal, region in enumerate(overlap_regions or [], start=1):
        bounds = _bounded_range(
            region.get("start_ms"), region.get("end_ms"), duration_ms=duration_ms
        )
        speakers = region.get("speakers")
        if (
            bounds is None
            or not isinstance(speakers, list)
            or len(speakers) < 2
            or not all(isinstance(label, str) for label in speakers)
        ):
            continue
        nodes.append(
            AudioGraphNode(
                f"audio:overlap:{ordinal:04d}",
                "audio_overlap_region",
                "STRUCTURE",
                *bounds,
                ordinal,
                ",".join(speakers),
                "audio:timeline",
                _extension(
                    authority="MODEL_DERIVED",
                    backend="sherpa-onnx-offline-speaker-diarization",
                    model_identity_sha256=diarization_model_identity_sha256,
                    overlap_method="INTERSECTING_DIARIZATION_TURNS",
                    speaker_count=len(speakers),
                ),
            )
        )
    transcript_nodes: list[AudioGraphNode] = []
    aligned_ordinal = 0
    unaligned_ordinal = 0
    word_sequence = 0
    for ordinal, segment in enumerate(segments, start=1):
        location = segment.get("location")
        if not isinstance(location, dict):
            continue
        bounds = _bounded_range(
            location.get("start_ms"), location.get("end_ms"), duration_ms=duration_ms
        )
        text = segment.get("text_or_value")
        if bounds is None or not isinstance(text, str):
            continue
        raw_extension = segment.get("extension")
        extension = raw_extension if isinstance(raw_extension, dict) else {}
        segment_id = f"audio:segment:{ordinal:04d}"
        segment_node = AudioGraphNode(
            segment_id,
            "audio_transcript_segment",
            "PARAGRAPH",
            *bounds,
            ordinal,
            text,
            "audio:timeline",
            _extension(
                authority="MODEL_DERIVED",
                backend="faster-whisper",
                model_id=model_id,
                model_identity_sha256=model_identity_sha256,
                timestamp_accuracy=extension.get("timestamp_accuracy"),
                language=extension.get("language"),
                avg_logprob=extension.get("avg_logprob"),
                no_speech_prob=extension.get("no_speech_prob"),
            ),
        )
        nodes.append(segment_node)
        transcript_nodes.append(segment_node)
        words = segment.get("words")
        if not isinstance(words, list):
            continue
        for word in words:
            if not isinstance(word, dict) or not isinstance(word.get("text"), str):
                continue
            word_sequence += 1
            word_bounds = _bounded_range(
                word.get("start_ms"), word.get("end_ms"), duration_ms=duration_ms
            )
            if word_bounds is None:
                unaligned_ordinal += 1
                word_bounds = bounds
                kind = "audio_unaligned_word"
                node_ordinal = unaligned_ordinal
                status = "UNALIGNED"
            else:
                status_value = word.get("alignment_status")
                status = status_value if isinstance(status_value, str) else "ASR_MODEL_APPROXIMATE"
                aligned_ordinal += 1
                kind = (
                    "audio_aligned_word"
                    if status == "CTC_FORCED_ALIGNED"
                    else "audio_approximate_word"
                )
                node_ordinal = aligned_ordinal
            nodes.append(
                AudioGraphNode(
                    f"{segment_id}:word:{word_sequence:05d}",
                    kind,
                    "TOKEN",
                    *word_bounds,
                    node_ordinal,
                    word["text"],
                    segment_id,
                    _extension(
                        authority="MODEL_DERIVED",
                        alignment_status=status,
                        timestamp_accuracy=word.get("timestamp_accuracy", "MODEL_APPROXIMATE"),
                        probability=word.get("probability"),
                        model_id=model_id,
                        model_identity_sha256=model_identity_sha256,
                        alignment_backend=word.get("alignment_backend"),
                        alignment_model_id=word.get("alignment_model_id"),
                        alignment_model_revision=word.get("alignment_model_revision"),
                        alignment_model_identity_sha256=word.get("alignment_model_identity_sha256"),
                        alignment_reason=word.get("alignment_reason"),
                    ),
                )
            )
    nodes.extend(_sentence_nodes(transcript_nodes))
    ordered = tuple(sorted(nodes, key=_node_order))
    return AudioDocumentGraph(
        source_sha256,
        duration_ms,
        window_start_ms,
        window_end_ms,
        ordered,
    )


__all__ = [
    "AUDIO_GRAPH_SCHEMA_NAME",
    "AUDIO_GRAPH_SCHEMA_VERSION",
    "AudioDocumentGraph",
    "AudioGraphNode",
    "build_audio_document_graph",
]
