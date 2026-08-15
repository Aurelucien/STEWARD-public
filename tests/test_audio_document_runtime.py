"""NEXT-021 bounded audio admission, timeline and native-route coverage."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import wave

import pytest

from local_steward.document_evidence import build_document_evidence_selection
from local_steward.document_discovery import match_document_path, normalize_document_query
from local_steward.document_observation import DocumentInspectionPage
from local_steward.file_agent.runtime.audio_documents import (
    MAX_AUDIO_SPEECH_MS,
    AudioDocumentWorker,
    audio_request_digest,
    audio_runtime_capabilities,
    probe_audio,
)
from local_steward.file_agent.runtime.structured_documents import (
    CURRENT_FILESYSTEM_AUDIO,
    DocumentResourceUsage,
    NormalizedDocumentItem,
)
from local_steward.grounded_evidence import build_document_evidence_packet
from local_steward.native_mcp_server import NativeStewardDispatcher, create_codex_host_policy
from local_steward.native_mcp_server.protocol import DOCUMENT_TOOL

from .test_steward_native_agent_surface import _session


def _wav(path: Path, *, seconds: int = 1, sample_rate: int = 16_000) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(b"\0\0" * sample_rate * seconds)


def test_ffprobe_admits_one_audio_stream_and_capabilities_are_path_free(tmp_path: Path) -> None:
    source = tmp_path / "bounded.wav"
    _wav(source, seconds=2)

    probe = probe_audio(str(source))
    capabilities = audio_runtime_capabilities()

    assert probe == {
        "duration_ms": 2000,
        "container": "wav",
        "codec": "pcm_s16le",
        "sample_rate_hz": 16000,
        "channels": 1,
        "channel_layout": None,
        "bit_rate": "256000",
        "stream_index": 0,
    }
    assert capabilities["probe_ready"] is True
    assert capabilities["vad_ready"] is True
    assert capabilities["asr_ready"] is True
    assert capabilities["runtime_downloads_allowed"] is False
    assert capabilities["persistence_effect"] == "NONE"
    assert "/Users/" not in json.dumps(capabilities, sort_keys=True)


def test_audio_worker_normalizes_model_timeline_and_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "long.mp3"
    source.write_bytes(b"synthetic")
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents.probe_audio",
        lambda _path: {
            "duration_ms": 1_000_000,
            "container": "mp3",
            "codec": "mp3",
            "sample_rate_hz": 44_100,
            "channels": 2,
            "channel_layout": "stereo",
            "bit_rate": "192000",
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
        lambda _path: ([{"start_ms": 100, "end_ms": 2000}], MAX_AUDIO_SPEECH_MS),
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.audio_documents._transcribe",
        lambda *_args, **_kwargs: (
            [
                {
                    "kind": "audio_transcript_segment",
                    "role": "PARAGRAPH",
                    "text_or_value": "bounded evidence",
                    "parent": "audio:timeline",
                    "location": {"start_ms": 100, "end_ms": 2000, "ordinal": 1},
                    "extension": {"model_derived": True},
                }
            ],
            "en",
            2,
        ),
    )

    worker = AudioDocumentWorker(
        "MP3",
        "TRANSCRIBE",
        request_digest="b" * 64,
        source_sha256="c" * 64,
    )
    result = worker(str(source))

    assert result["backend_name"] == "FasterWhisper"
    assert result["warnings"] == ["AUDIO_SPEECH_LIMIT_REACHED"]
    assert result["items"][-1]["text_or_value"] == "bounded evidence"
    assert result["resource_extension"]["model_derived"] is True
    assert result["continuation"] == {
        "schema_name": "local_steward.audio_continuation",
        "schema_version": 1,
        "request_digest": "b" * 64,
        "source_sha256": "c" * 64,
        "next_start_ms": 2000,
    }


def test_audio_evidence_is_model_derived_with_native_time_range() -> None:
    item = NormalizedDocumentItem(
        "audio_transcript_segment",
        "Codex answers from local evidence",
        "audio:timeline",
        {"start_ms": 1200, "end_ms": 3450, "ordinal": 1},
        {"model_derived": True, "timestamp_accuracy": "MODEL_APPROXIMATE"},
        role="PARAGRAPH",
    )
    selection = build_document_evidence_selection(
        (item,),
        source_sha256="d" * 64,
        query="local evidence",
        mode="AUTO",
        context_items=0,
        max_characters=4096,
        limit=10,
        offset=0,
        searchable=True,
        source_format="FLAC",
    )
    page = DocumentInspectionPage(
        4,
        "COMPLETE",
        "FLAC",
        "FasterWhisper",
        "1.2.1",
        CURRENT_FILESYSTEM_AUDIO,
        "downloads",
        "speech.flac",
        "d" * 64,
        None,
        (),
        DocumentResourceUsage(10, 0, 1, 100, 1, 100),
        (item,),
        1,
        1,
        10,
        0,
        False,
        None,
        "e" * 64,
        None,
        None,
        "READ",
        selection,
    )

    packet = build_document_evidence_packet(page)
    fact = packet["facts"][0]

    assert packet["verification"]["status"] == "MODEL_OBSERVATION_COMPLETE"
    assert fact["native_location"] == {
        "kind": "AUDIO_TIME_RANGE",
        "label": "1.200s, to 3.450s",
        "locator": "start_ms:1200/end_ms:3450",
    }
    assert fact["authority"] == "MODEL_DERIVED"
    assert fact["timestamp_accuracy"] == "MODEL_APPROXIMATE"
    assert packet["delivery"]["model_output_must_not_be_described_as_verbatim"] is True


def test_audio_continuation_digest_binds_source_request_and_model() -> None:
    first = audio_request_digest(
        source_sha256="a" * 64,
        scope_id="downloads",
        relative_path="speech.wav",
        intent="EVIDENCE",
        content_query="evidence",
        language="en",
        model_identity_sha256="b" * 64,
    )
    changed = audio_request_digest(
        source_sha256="a" * 64,
        scope_id="downloads",
        relative_path="speech.wav",
        intent="EVIDENCE",
        content_query="different",
        language="en",
        model_identity_sha256="b" * 64,
    )

    assert first != changed
    assert len(first) == 64


def test_audio_filename_discovery_normalizes_unicode_composition() -> None:
    decomposed = "audio/独自のアプローチでの表現.wav"
    query = normalize_document_query("独自のアプローチでの表現")

    assert match_document_path(decomposed, query) == (1, "BASENAME_PREFIX")


@pytest.mark.anyio
async def test_native_structure_routes_unique_audio_without_running_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())
    _config, scope, session = _session(tmp_path)
    source = scope / "bounded.wav"
    _wav(source)
    before = (
        source.stat().st_size,
        source.stat().st_mtime_ns,
        sha256(source.read_bytes()).hexdigest(),
    )
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    result = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {"action": "STRUCTURE", "query": "bounded.wav", "extensions": ["WAV"]},
    )

    document = result.structuredContent["result"]["document"]
    assert result.isError is False
    assert document["source_kind"] == CURRENT_FILESYSTEM_AUDIO
    assert document["source_format"] == "WAV"
    assert document["backend_name"] == "FFprobe"
    assert document["media"]["duration_ms"] == 1000
    assert result.structuredContent["selection"][0]["policy"] == "QUERY_UNIQUE"
    assert before == (
        source.stat().st_size,
        source.stat().st_mtime_ns,
        sha256(source.read_bytes()).hexdigest(),
    )
