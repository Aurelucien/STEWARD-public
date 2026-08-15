"""NEXT-022B operation-scoped audio graph contract."""

from __future__ import annotations

import pytest

from local_steward.file_agent.runtime.audio_graph import (
    AudioDocumentGraph,
    AudioGraphNode,
    build_audio_document_graph,
)


def _graph() -> AudioDocumentGraph:
    return build_audio_document_graph(
        source_sha256="a" * 64,
        duration_ms=10_000,
        window_start_ms=2_000,
        window_end_ms=8_000,
        probe={"container": "wav", "codec": "pcm_s16le", "channels": 1},
        intervals=[{"start_ms": 100, "end_ms": 5_500}],
        segments=[
            {
                "text_or_value": "Hello world.",
                "location": {"start_ms": 2_200, "end_ms": 4_000, "ordinal": 1},
                "extension": {
                    "timestamp_accuracy": "MODEL_APPROXIMATE",
                    "language": "en",
                    "avg_logprob": -0.2,
                },
                "words": [
                    {
                        "text": " Hello",
                        "start_ms": 2_200,
                        "end_ms": 2_800,
                        "probability": 0.9,
                    },
                    {
                        "text": " world.",
                        "start_ms": 2_800,
                        "end_ms": 4_000,
                        "probability": 0.8,
                    },
                    {"text": " ?", "start_ms": None, "end_ms": None},
                ],
            }
        ],
        model_id="pinned/model",
        model_identity_sha256="b" * 64,
    )


def test_audio_graph_is_source_global_deterministic_and_non_persistent() -> None:
    graph = _graph()
    repeat = _graph()

    assert graph == repeat
    assert graph.graph_digest == repeat.graph_digest
    assert graph.summary() == {
        "schema_name": "local_steward.audio_document_graph",
        "schema_version": 1,
        "graph_digest": graph.graph_digest,
        "node_count": 7,
        "node_counts": {
            "audio_approximate_word": 2,
            "audio_sentence_group": 1,
            "audio_source": 1,
            "audio_speech_region": 1,
            "audio_transcript_segment": 1,
            "audio_unaligned_word": 1,
        },
        "window_start_ms": 2000,
        "window_end_ms": 8000,
        "persistence_effect": "NONE",
    }
    assert all(0 <= node.start_ms < node.end_ms <= 10_000 for node in graph.nodes)


def test_default_projection_keeps_next021_output_depth() -> None:
    graph = _graph()
    projected = graph.public_items()

    assert [item["kind"] for item in projected] == [
        "audio_metadata",
        "audio_speech_interval",
        "audio_transcript_segment",
    ]
    assert all("words" not in item for item in projected)
    word_kinds = {
        item["kind"]
        for item in graph.public_items(include_words=True)
        if item["role"] == "TOKEN"
    }
    assert word_kinds == {"audio_approximate_word", "audio_unaligned_word"}


def test_audio_graph_rejects_duplicate_nodes_and_invalid_ranges() -> None:
    node = AudioGraphNode("audio:source", "audio_source", "METADATA", 0, 1000, 0)
    with pytest.raises(ValueError, match="not unique"):
        AudioDocumentGraph("a" * 64, 1000, 0, 1000, (node, node))
    with pytest.raises(ValueError, match="range"):
        AudioGraphNode("bad", "audio_source", "METADATA", 10, 10, 0)
