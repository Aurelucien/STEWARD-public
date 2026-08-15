"""NEXT-023B deterministic base-transcript policy and depth invariance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from local_steward.file_agent.runtime.audio_documents import (
    AUDIO_DECODING_POLICY,
    AUDIO_DECODING_POLICY_ID,
    AUDIO_DECODING_POLICY_SHA256,
    _transcribe,
    audio_request_digest,
    audio_runtime_capabilities,
    base_transcript_digest,
)


@dataclass
class _Word:
    word: str
    start: float
    end: float
    probability: float


@dataclass
class _Segment:
    text: str
    start: float
    end: float
    words: list[_Word]
    avg_logprob: float = -0.1
    no_speech_prob: float = 0.01


def test_decoder_policy_is_explicit_path_free_and_continuation_bound() -> None:
    capability = audio_runtime_capabilities()
    first = audio_request_digest(
        source_sha256="a" * 64,
        scope_id="downloads",
        relative_path="speech.wav",
        intent="READ",
        content_query=None,
        language="en",
        model_identity_sha256="b" * 64,
    )
    changed = audio_request_digest(
        source_sha256="a" * 64,
        scope_id="downloads",
        relative_path="speech.wav",
        intent="READ",
        content_query=None,
        language="en",
        model_identity_sha256="b" * 64,
        decoding_policy_sha256="c" * 64,
    )

    assert capability["decoding_policy_id"] == AUDIO_DECODING_POLICY_ID
    assert capability["decoding_policy_sha256"] == AUDIO_DECODING_POLICY_SHA256
    assert capability["decoding_policy"] == AUDIO_DECODING_POLICY
    assert capability["decoding_policy"]["temperature"] == 0.0
    assert capability["decoding_policy"]["cpu_threads"] == 1
    assert first != changed


def test_transcribe_passes_one_deterministic_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor: dict[str, object] = {}
    invocation: dict[str, object] = {}

    class FakeModel:
        def __init__(self, model_path: str, **kwargs: object) -> None:
            constructor.update({"model_path": model_path, **kwargs})

        def transcribe(self, source: str, **kwargs: object):
            invocation.update({"source": source, **kwargs})
            return (
                [_Segment(" stable", 0.1, 0.9, [_Word(" stable", 0.1, 0.9, 0.9)])],
                SimpleNamespace(language="en"),
            )

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    segments, language, words = _transcribe(
        Path("window.wav"),
        model_path=Path("local-model"),
        language="en",
        intervals=[{"start_ms": 0, "end_ms": 1000}],
        window_start_ms=2000,
        window_duration_ms=1000,
    )

    assert constructor == {
        "model_path": "local-model",
        "device": "cpu",
        "compute_type": "int8",
        "cpu_threads": 1,
        "num_workers": 1,
        "local_files_only": True,
    }
    assert invocation["temperature"] == 0.0
    assert invocation["task"] == "transcribe"
    assert invocation["condition_on_previous_text"] is False
    assert invocation["vad_filter"] is False
    assert invocation["beam_size"] == 5
    assert language == "en"
    assert words == 1
    assert segments[0]["location"] == {"start_ms": 2100, "end_ms": 2900, "ordinal": 1}


def test_base_digest_is_stable_before_advanced_analysis() -> None:
    base = [
        {
            "kind": "audio_transcript_segment",
            "text_or_value": "unchanged",
            "location": {"start_ms": 10, "end_ms": 900, "ordinal": 1},
            "words": [{"text": " unchanged", "start_ms": 10, "end_ms": 900}],
        }
    ]
    aligned = [
        {
            **base[0],
            "words": [
                {
                    "text": " unchanged",
                    "start_ms": 20,
                    "end_ms": 880,
                    "alignment_status": "CTC_FORCED_ALIGNED",
                }
            ],
        }
    ]
    speaker_augmented = [*aligned]

    assert base_transcript_digest(base) == base_transcript_digest(aligned)
    assert base_transcript_digest(base) == base_transcript_digest(speaker_augmented)
