"""Regression coverage for cached long-audio result pagination and host path aliases."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import wave

import pytest

from local_steward.file_agent.runtime.audio_documents import AudioDocumentWorker
from local_steward.file_agent.runtime.structured_documents import (
    IsolatedParserWorker,
    _WorkerExecution,
)
from local_steward.native_mcp_server import NativeStewardDispatcher, create_codex_host_policy
from local_steward.native_mcp_server.host_paths import admit_host_absolute_file
from local_steward.native_mcp_server.protocol import DOCUMENT_TOOL

from .test_steward_native_agent_surface import _session


def _wav(path: Path, *, seconds: int = 1, sample_rate: int = 16_000) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(b"\0\0" * sample_rate * seconds)


def _audio_payload(target: AudioDocumentWorker) -> dict[str, Any]:
    assert target.request_digest is not None
    assert target.source_sha256 is not None
    items: list[dict[str, object]] = [
        {
            "kind": "audio_document",
            "node_id": "audio:root",
            "role": "DOCUMENT",
            "text_or_value": None,
            "parent": None,
            "location": {"start_ms": 0, "end_ms": 125_000},
            "extension": {"model_derived": True},
        }
    ]
    for ordinal in range(1, 125):
        items.append(
            {
                "kind": "audio_transcript_segment",
                "node_id": f"audio:segment:{ordinal:04d}",
                "role": "PARAGRAPH",
                "text_or_value": f"segment {ordinal}",
                "parent": "audio:root",
                "location": {
                    "start_ms": ordinal * 1_000,
                    "end_ms": ordinal * 1_000 + 900,
                    "ordinal": ordinal,
                },
                "extension": {"model_derived": True},
            }
        )
    return {
        "backend_name": "FasterWhisper",
        "backend_version": "test",
        "warnings": [],
        "items": items,
        "resource_extension": {
            "media_kind": "AUDIO",
            "duration_ms": 125_000,
            "window_start_ms": target.start_ms,
            "window_end_ms": 125_000,
            "audio_request_digest": target.request_digest,
            "base_transcript_digest": "d" * 64,
            "model_derived": True,
            "persistence_effect": "NONE",
        },
        "continuation": None,
    }


@pytest.mark.anyio
async def test_audio_read_continuation_pages_over_100_items_without_rerunning_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())
    _config, scope, session = _session(tmp_path)
    source = scope / "long.wav"
    _wav(source)
    before = (source.stat().st_size, source.stat().st_mtime_ns, sha256(source.read_bytes()).hexdigest())
    calls: list[int] = []

    monkeypatch.setattr(
        "local_steward.file_agent.runtime.structured_documents.audio_runtime_capabilities",
        lambda: {"probe_ready": True, "vad_ready": True, "asr_ready": True},
    )
    monkeypatch.setattr(
        "local_steward.file_agent.runtime.structured_documents.resolve_local_audio_model",
        lambda: (tmp_path, "revision", "a" * 64),
    )

    def fake_run(worker: IsolatedParserWorker, _source_path: Path) -> _WorkerExecution:
        assert isinstance(worker.worker_target, AudioDocumentWorker)
        calls.append(worker.worker_target.start_ms)
        return _WorkerExecution(
            "COMPLETE", _audio_payload(worker.worker_target), 1, 1_024
        )

    monkeypatch.setattr(IsolatedParserWorker, "run", fake_run)
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    first = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {"action": "READ", "absolute_path": str(source), "limit": 100},
    )
    first_page = first.structuredContent["result"]["document"]
    continuation = first_page["continuation"]

    assert first.isError is False
    assert first_page["returned_count"] == 100
    assert first_page["has_more"] is True
    assert first_page["next_offset"] == 100
    assert continuation == {
        "schema_name": "local_steward.audio_continuation",
        "schema_version": 2,
        "kind": "RESULT_PAGE",
        "request_digest": continuation["request_digest"],
        "source_sha256": first_page["source_sha256"],
        "window_start_ms": 0,
        "next_offset": 100,
        "limit": 100,
        "next_window": None,
    }
    assert first_page["execution"]["attempts"][0]["cache_status"] == "MISS"
    omissions = first.structuredContent["result"]["evidence_packet"]["omissions"]
    assert any(item["reason_code"] == "AUDIO_RESULT_PAGE_LIMIT" for item in omissions)

    second = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            "action": "READ",
            "absolute_path": str(source),
            "audio_continuation": continuation,
        },
    )
    second_page = second.structuredContent["result"]["document"]

    assert second.isError is False
    assert second_page["offset"] == 100
    assert second_page["limit"] == 100
    assert second_page["returned_count"] == 25
    assert second_page["has_more"] is False
    assert second_page["next_offset"] is None
    assert second_page["continuation"] is None
    assert second_page["execution"]["attempts"][0]["cache_status"] == "HIT"
    first_ids = {item["node_id"] for item in first_page["items"]}
    second_ids = {item["node_id"] for item in second_page["items"]}
    assert len(first_ids | second_ids) == 125
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
            "audio_continuation": continuation,
        },
    )
    assert expired.isError is True
    assert calls == [0]


def test_macos_tmp_alias_is_canonicalized_before_exact_file_safety_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())
    _config, _scope, session = _session(tmp_path)
    with TemporaryDirectory(prefix="steward-audio-alias-", dir="/tmp") as directory:
        canonical_directory = Path(directory).resolve()
        source = canonical_directory / "alias.wav"
        source.write_bytes(b"audio")
        alias = Path("/tmp") / canonical_directory.name / source.name

        binding = admit_host_absolute_file(session, str(alias))

        assert binding.relative_path == source.name
        assert binding.config.scopes[0].normalized_path == canonical_directory
        assert binding.config.scopes[0].raw_path == str(canonical_directory)
