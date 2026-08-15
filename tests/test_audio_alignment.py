"""NEXT-022C local forced-alignment contract and routing tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_steward.document_observation import (
    DocumentInspectionInputError,
    DocumentInspectionRequest,
    _validate_request,
)
from local_steward.file_agent.runtime.audio_alignment import (
    ALIGNMENT_BACKEND,
    AlignmentModel,
    AudioAlignmentUnavailable,
    align_transcript_words,
    alignment_runtime_capabilities,
    ctc_token_frames,
)
from local_steward.file_agent.runtime.audio_documents import (
    AudioDocumentWorker,
    audio_request_digest,
)
from local_steward.native_mcp_server.protocol import DOCUMENT_INPUT_SCHEMA


def test_ctc_backtracking_returns_one_ordered_frame_per_token() -> None:
    torch = pytest.importorskip("torch")
    logits = torch.full((7, 4), -8.0)
    logits[:, 0] = 2.0
    logits[1, 1] = 9.0
    logits[3, 2] = 9.0
    logits[5, 3] = 9.0
    frames = ctc_token_frames(torch.log_softmax(logits, dim=-1), [1, 2, 3], 0)

    assert [frame for frame, _score in frames] == [1, 3, 5]
    assert all(0.99 < score <= 1.0 for _frame, score in frames)


def test_installed_alignment_capability_is_local_path_free_and_pinned() -> None:
    capability = alignment_runtime_capabilities()

    assert capability["backend"] == ALIGNMENT_BACKEND
    assert capability["runtime_downloads_allowed"] is False
    assert capability["persistence_effect"] == "NONE"
    assert capability["supported_languages"] == ["en", "ja", "zh"]
    assert all(model["installed"] is True for model in capability["models"])
    assert all(len(model["model_identity_sha256"]) == 64 for model in capability["models"])
    assert {model["evaluation_state"] for model in capability["models"]} == {
        "SUPPORTED_AND_EVALUATED"
    }
    assert capability["language_aliases"]["cmn"] == "zh"
    assert capability["language_aliases"]["jpn"] == "ja"
    assert capability["unlisted_language_state"] == "UNSUPPORTED"
    assert (
        next(model for model in capability["models"] if model["language"] == "zh")[
            "text_normalizer"
        ]
        == "OPENCC_T2S_0.1.7_V1"
    )
    assert "/Users/" not in str(capability)


def test_chinese_alignment_normalizes_tokens_without_rewriting_observed_text() -> None:
    from local_steward.file_agent.runtime.audio_alignment import _normalize_alignment_text

    observed = "氣"

    assert _normalize_alignment_text(observed, language="zh") == "气"
    assert observed == "氣"


def test_audio_analysis_is_validated_and_exposed_without_a_new_tool() -> None:
    schema = DOCUMENT_INPUT_SCHEMA
    assert schema["properties"]["audio_analysis"]["enum"] == [
        "TRANSCRIPT",
        "ALIGNED_WORDS",
        "SPEAKER_TURNS",
        "ALIGNED_WORDS_AND_SPEAKERS",
    ]
    _validate_request(
        DocumentInspectionRequest("scope", "audio.wav", True, audio_analysis="ALIGNED_WORDS")
    )
    with pytest.raises(DocumentInspectionInputError, match="analysis mode"):
        _validate_request(
            DocumentInspectionRequest("scope", "audio.wav", True, audio_analysis="BEST_EFFORT")
        )


def test_continuation_digest_binds_analysis_and_alignment_model() -> None:
    shared = {
        "source_sha256": "a" * 64,
        "scope_id": "downloads",
        "relative_path": "speech.wav",
        "intent": "EVIDENCE",
        "content_query": "evidence",
        "language": "en",
        "model_identity_sha256": "b" * 64,
    }
    transcript = audio_request_digest(**shared)
    aligned = audio_request_digest(
        **shared,
        analysis="ALIGNED_WORDS",
        alignment_model_identity_sha256="c" * 64,
    )
    changed_model = audio_request_digest(
        **shared,
        analysis="ALIGNED_WORDS",
        alignment_model_identity_sha256="d" * 64,
    )

    assert len({transcript, aligned, changed_model}) == 3


def test_aligned_worker_publishes_words_with_distinct_model_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "speech.wav"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents.probe_audio",
        lambda _path: {
            "duration_ms": 2_000,
            "container": "wav",
            "codec": "pcm_s16le",
            "sample_rate_hz": 16_000,
            "channels": 1,
            "channel_layout": None,
            "bit_rate": "256000",
            "stream_index": 0,
        },
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents.resolve_local_audio_model",
        lambda: (tmp_path, "asr-revision", "a" * 64),
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents._decode_window",
        lambda _source, target, **_kwargs: target.write_bytes(b"wav"),
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents._speech_intervals",
        lambda _path: ([{"start_ms": 100, "end_ms": 1_500}], 1_400),
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents._transcribe",
        lambda *_args, **_kwargs: (
            [
                {
                    "text_or_value": " unchanged text",
                    "location": {"start_ms": 100, "end_ms": 1_500, "ordinal": 1},
                    "extension": {"timestamp_accuracy": "MODEL_APPROXIMATE", "language": "en"},
                    "words": [{"text": " unchanged", "start_ms": 100, "end_ms": 800}],
                }
            ],
            "en",
            1,
        ),
    )
    alignment_model = AlignmentModel("en", "pinned/alignment", "revision", tmp_path, "b" * 64)

    def align(_wav: Path, segments: list[dict[str, object]], **_kwargs: object):
        copied = [dict(segment) for segment in segments]
        copied[0]["words"] = [
            {
                "text": " unchanged",
                "start_ms": 240,
                "end_ms": 720,
                "probability": 0.95,
                "alignment_status": "CTC_FORCED_ALIGNED",
                "timestamp_accuracy": "MODEL_ALIGNED",
                "alignment_backend": ALIGNMENT_BACKEND,
                "alignment_model_id": alignment_model.model_id,
                "alignment_model_revision": alignment_model.revision,
                "alignment_model_identity_sha256": alignment_model.identity_sha256,
            }
        ]
        return copied, alignment_model, 1, 0

    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents.align_transcript_words", align
    )
    result = AudioDocumentWorker("WAV", "TRANSCRIBE", analysis="ALIGNED_WORDS")(str(source))

    word = next(item for item in result["items"] if item["kind"] == "audio_aligned_word")
    assert word["text_or_value"] == " unchanged"
    assert word["location"]["start_ms"] == 240
    assert word["extension"]["alignment_status"] == "CTC_FORCED_ALIGNED"
    assert word["extension"]["timestamp_accuracy"] == "MODEL_ALIGNED"
    assert word["extension"]["alignment_model_id"] == "pinned/alignment"
    assert result["resource_extension"]["aligned_word_count"] == 1
    assert result["resource_extension"]["audio_analysis"] == "ALIGNED_WORDS"


def test_default_transcript_never_invokes_forced_alignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "speech.wav"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents.probe_audio",
        lambda _path: {
            "duration_ms": 1_000,
            "container": "wav",
            "codec": "pcm_s16le",
            "sample_rate_hz": 16_000,
            "channels": 1,
            "channel_layout": None,
            "bit_rate": "256000",
            "stream_index": 0,
        },
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents.resolve_local_audio_model",
        lambda: (tmp_path, "revision", "a" * 64),
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents._decode_window",
        lambda _source, target, **_kwargs: target.write_bytes(b"wav"),
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents._speech_intervals",
        lambda _path: ([], 0),
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents._transcribe",
        lambda *_args, **_kwargs: ([], "en", 0),
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents.align_transcript_words",
        lambda *_args, **_kwargs: pytest.fail("default transcript loaded forced alignment"),
    )

    result = AudioDocumentWorker("WAV", "TRANSCRIBE")(str(source))

    assert result["resource_extension"]["audio_analysis"] == "TRANSCRIPT"
    assert result["resource_extension"]["alignment_backend"] is None


def test_unalignable_segment_preserves_every_word_as_explicitly_unaligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = AlignmentModel("en", "pinned/alignment", "revision", tmp_path, "b" * 64)
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_alignment.resolve_local_alignment_model",
        lambda _language: model,
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_alignment._read_pcm",
        lambda _path: ([0], 16_000),
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_alignment._align_segment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AudioAlignmentUnavailable("CTC emissions cannot cover the requested tokens")
        ),
    )
    segments = [
        {
            "text_or_value": "preserved text",
            "location": {"start_ms": 0, "end_ms": 1000, "ordinal": 1},
            "words": [
                {"text": " preserved", "start_ms": 0, "end_ms": 500},
                {"text": " text", "start_ms": 500, "end_ms": 1000},
            ],
        }
    ]

    output, returned_model, aligned, unaligned = align_transcript_words(
        tmp_path / "window.wav", segments, language="en", window_start_ms=0
    )

    assert returned_model == model
    assert aligned == 0
    assert unaligned == 2
    assert output[0]["text_or_value"] == "preserved text"
    assert [word["text"] for word in output[0]["words"]] == [" preserved", " text"]
    assert all(word["alignment_status"] == "UNALIGNED" for word in output[0]["words"])
    assert all(word["start_ms"] is None for word in output[0]["words"])
    assert all("CTC emissions" in word["alignment_reason"] for word in output[0]["words"])
