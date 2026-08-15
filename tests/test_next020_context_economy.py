"""Context-economy contract for the Codex-facing Skill and MCP discovery surface."""

from __future__ import annotations

import json
from pathlib import Path

from local_steward.native_mcp_server.protocol import (
    DOCUMENT_INPUT_SCHEMA,
    SERVER_INSTRUCTIONS,
    TOOL_NAMES,
    tool_descriptors,
)


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = (
    ROOT
    / "experiments"
    / "steward_exoskeleton"
    / "r4d_r3d_plugin_source"
    / "skills"
    / "steward-codex"
)


def _compact_bytes(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def test_fixed_codex_surface_is_progressively_disclosed_and_bounded() -> None:
    descriptors = tool_descriptors()
    tools_payload = [item.model_dump(by_alias=True, exclude_none=True) for item in descriptors]
    skill_bytes = (SKILL_ROOT / "SKILL.md").stat().st_size

    assert tuple(item.name for item in descriptors) == TOOL_NAMES
    assert all(item.outputSchema is None for item in descriptors)
    assert skill_bytes < 3_500
    assert len(SERVER_INSTRUCTIONS.encode("utf-8")) < 1_500
    assert _compact_bytes(tools_payload) < 9_000
    assert (
        skill_bytes + len(SERVER_INSTRUCTIONS.encode("utf-8")) + _compact_bytes(tools_payload)
        < 12_750
    )

    assert (SKILL_ROOT / "references" / "audio-routing.md").stat().st_size < 2_000
    assert (SKILL_ROOT / "references" / "video-routing.md").stat().st_size < 1_800

    references = {path.name for path in (SKILL_ROOT / "references").iterdir()}
    assert references == {
        "audio-routing.md",
        "document-routing.md",
        "evidence-delivery.md",
        "execution-continuity.md",
        "history-and-lifecycle.md",
        "video-routing.md",
    }


def test_document_schema_is_flat_and_keeps_strict_declared_fields() -> None:
    assert "oneOf" not in DOCUMENT_INPUT_SCHEMA
    assert DOCUMENT_INPUT_SCHEMA["additionalProperties"] is False
    properties = DOCUMENT_INPUT_SCHEMA["properties"]
    assert set(("absolute_path", "query", "scope_id", "relative_path")) <= set(properties)
    assert properties["diagnostic_detail"]["enum"] == ["COMPACT", "FULL"]
    assert "literal term" in properties["content_query"]["description"]


def test_audio_routing_is_one_hop_and_does_not_load_document_format_matrix() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    audio = (SKILL_ROOT / "references" / "audio-routing.md").read_text(encoding="utf-8")
    document = (SKILL_ROOT / "references" / "document-routing.md").read_text(encoding="utf-8")

    assert "For audio, read [audio-routing.md]" in skill
    compact_audio = " ".join(audio.split())
    assert "Broad `READ` already contains enough evidence" in compact_audio
    assert "Do not call `CAPABILITIES`" in compact_audio
    assert "`ALIGNED_WORDS`" in compact_audio
    assert "`SPEAKER_TURNS`" in compact_audio
    assert "anonymous and file-local" in compact_audio
    assert (
        "English, Japanese and Chinese are the evaluated local alignment registry" in compact_audio
    )
    assert "`base_transcript_digest`" in compact_audio
    assert "`REFERENCE_UNAVAILABLE`" in compact_audio
    assert "WAV, FLAC" not in document
