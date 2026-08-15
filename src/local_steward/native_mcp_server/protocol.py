"""Frozen risk-separated descriptors for the native STEWARD Agent surface."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from mcp.types import Tool, ToolAnnotations

from ..codex_identity import (
    MCP_SERVER_NAME,
    NATIVE_SERVER_VERSION,
    NATIVE_SURFACE_IDENTITY,
    integration_identity_machine_object,
)
from .surface_manifest import build_surface_manifest, surface_manifest_json
from .thread_attribution import (
    MAX_HOST_SESSION_CHARS,
    THREAD_ATTRIBUTION_SCHEMA_NAME,
    THREAD_ATTRIBUTION_SCHEMA_VERSION,
)


SERVER_NAME = MCP_SERVER_NAME
SERVER_VERSION = NATIVE_SERVER_VERSION
ADAPTER_SCHEMA_NAME = "local_steward.native_agent_result"
ADAPTER_SCHEMA_VERSION = 3
EXACT_INTEGER_ENCODING_SCHEME = "safe-json-integer-or-decimal-string-v1"
CONFIG_ENVIRONMENT_VARIABLE = "LOCAL_STEWARD_NATIVE_CONFIG"
HOST_POLICY_ENVIRONMENT_VARIABLE = "LOCAL_STEWARD_NATIVE_HOST_POLICY"
MAX_STRUCTURED_RESULT_BYTES = 524_288
MAX_TEXT_BYTES = 2_048
OPERATION_TIMEOUT_SECONDS = 120.0

HISTORY_TOOL = "steward_history"
DOCUMENT_TOOL = "steward_read_document"
CODE_TOOL = "steward_code_execution"
UPDATE_TOOL = "steward_update_snapshot"
RECOVERY_TOOL = "steward_recover_snapshot_run"
TOOL_NAMES = (HISTORY_TOOL, DOCUMENT_TOOL, CODE_TOOL, UPDATE_TOOL, RECOVERY_TOOL)

SURFACE_MANIFEST = build_surface_manifest(TOOL_NAMES)
SURFACE_MANIFEST_JSON = surface_manifest_json(TOOL_NAMES)

SERVER_INSTRUCTIONS = (
    f"STEWARD native service {NATIVE_SURFACE_IDENTITY}. Identity: "
    + json.dumps(
        integration_identity_machine_object(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    + ". Tools, in stable order: "
    + ", ".join(TOOL_NAMES)
    + ". Use structured results. Preserve source identity, verification, citations, native "
    "locations, unknowns, omissions, and typed failures. Snapshots are not current state. Reads "
    "do not persist parsed content; writes use Codex approval. With "
    "STEWARD_HOST_OBSERVER_V1_ACTIVE, skip manual code pre/postflight; otherwise "
    "steward_code_execution is a read-only fixed-project fallback."
)

_SELECTOR: dict[str, Any] = {
    "type": "object",
    "properties": {
        "policy": {
            "enum": [
                "EXACT_ID",
                "ONLY_COMPATIBLE",
                "LATEST_VALID",
                "PREVIOUS_VALID",
            ]
        },
        "snapshot_id": {"type": "string", "minLength": 1},
        "anchor_snapshot_id": {"type": "string", "minLength": 1},
        "scope_id": {"type": "string", "minLength": 1},
    },
    "required": ["policy"],
    "additionalProperties": False,
}

HISTORY_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "action": {
            "enum": [
                "STATUS",
                "LIST_SNAPSHOTS",
                "INSPECT_SNAPSHOT",
                "ANALYZE_SNAPSHOT",
                "REVIEW_CHANGES",
                "EXPLAIN_CHANGES",
            ]
        },
        "selector": deepcopy(_SELECTOR),
        "base_selector": deepcopy(_SELECTOR),
        "question": {"type": "string", "minLength": 1, "maxLength": 8192},
        "analysis_profile": {"type": "string", "minLength": 1, "maxLength": 64},
        "scope_id": {"type": ["string", "null"]},
        "path_prefix": {"type": ["string", "null"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "continuation": {
            "type": "object",
            "properties": {
                "request_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "offset": {"type": "integer", "minimum": 0},
            },
            "required": ["request_digest", "offset"],
            "additionalProperties": False,
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

DOCUMENT_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "action": {
            "enum": [
                "CAPABILITIES",
                "AUTO",
                "DISCOVER",
                "STRUCTURE",
                "LOCATE",
                "EVIDENCE",
                "EVIDENCE_SET",
                "READ",
                "EXTRACT_TABLE",
                "EXTRACT_FORMULA",
                "VIEW",
            ]
        },
        "absolute_path": {"type": "string", "minLength": 1},
        "scope_id": {"type": "string", "minLength": 1},
        "relative_path": {"type": "string", "minLength": 1},
        "query": {"type": "string", "minLength": 1, "maxLength": 512},
        "extensions": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "PDF", "EPUB", "DOCX", "XLSX", "PPTX",
                    "PNG", "JPEG", "TIFF",
                    ".pdf", ".epub", ".docx", ".xlsx", ".pptx",
                    ".png", ".jpg", ".jpeg", ".tif", ".tiff",
                    "WAV", "FLAC", "MP3", "M4A", "AAC", "OGG", "OPUS",
                    ".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus",
                    "MP4", "MOV", "MKV", "WEBM",
                    ".mp4", ".m4v", ".mov", ".mkv", ".webm",
                ],
            },
            "minItems": 1,
            "maxItems": 36,
            "uniqueItems": True,
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "expected_source_sha256": {
            "type": ["string", "null"],
            "pattern": "^[0-9a-f]{64}$",
        },
        "content_query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": "Short literal term expected in the source, not a natural-language question.",
        },
        "content_limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "default": 20,
            "description": "Bounded match/item count; omit for STRUCTURE.",
        },
        "content_offset": {"type": "integer", "minimum": 0, "default": 0},
        "evidence_mode": {
            "enum": ["AUTO", "MATCH", "WINDOW", "SECTION"],
            "default": "AUTO",
        },
        "evidence_context_items": {
            "type": "integer",
            "minimum": 0,
            "maximum": 8,
            "default": 2,
        },
        "evidence_max_characters": {
            "type": "integer",
            "minimum": 512,
            "maximum": 32768,
            "default": 12000,
        },
        "evidence_page": {"type": "integer", "minimum": 1, "maximum": 10000},
        "snapshot_selector": deepcopy(_SELECTOR),
        "max_documents": {
            "type": "integer",
            "minimum": 1,
            "maximum": 8,
            "default": 4,
        },
        "batch_size": {
            "type": "integer",
            "minimum": 1,
            "maximum": 2,
            "default": 2,
        },
        "per_document_timeout_seconds": {
            "type": "number",
            "minimum": 5,
            "maximum": 120,
            "default": 45,
        },
        "collection_continuation": {
            "type": "object",
            "description": "Pass the returned collection continuation unchanged.",
        },
        "page": {"type": "integer", "minimum": 1, "maximum": 10000},
        "node_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "visual_scale": {
            "type": "number",
            "minimum": 0.5,
            "maximum": 3.0,
            "default": 2.0,
        },
        "diagnostic_detail": {
            "enum": ["COMPACT", "FULL"],
            "default": "COMPACT",
            "description": "EVIDENCE/EVIDENCE_SET only; FULL adds parser/resource diagnostics.",
        },
        "audio_language": {
            "type": "string",
            "minLength": 1,
            "maxLength": 16,
            "pattern": "^[A-Za-z]+(?:-[A-Za-z]+)*$",
            "description": "Optional ASR language hint; omit for local detection.",
        },
        "audio_analysis": {
            "enum": [
                "TRANSCRIPT",
                "ALIGNED_WORDS",
                "SPEAKER_TURNS",
                "ALIGNED_WORDS_AND_SPEAKERS",
            ],
            "default": "TRANSCRIPT",
            "description": (
                "Optional local audio depth. TRANSCRIPT is the low-cost default; "
                "other modes require their pinned local analysis runtime."
            ),
        },
        "audio_continuation": {
            "type": "object",
            "description": (
                "Pass unchanged; RESULT_PAGE reuses cached ASR, others advance source time."
            ),
        },
        "video_analysis": {
            "enum": [
                "SCENES",
                "SCENES_AND_OCR",
                "MULTIMODAL",
                "MULTIMODAL_AND_OCR",
            ],
            "default": "MULTIMODAL",
            "description": (
                "Bounded video projection; multimodal adds subtitles and local base ASR."
            ),
        },
        "video_timestamp_ms": {
            "type": "integer",
            "minimum": 0,
            "description": "VIEW only; source-relative representative frame timestamp.",
        },
        "video_continuation": {
            "type": "object",
            "description": (
                "Pass unchanged; RESULT_PAGE reuses cached multimodal analysis, "
                "others advance source time."
            ),
        },
    },
    "additionalProperties": False,
}

_SCOPE_SELECTOR: dict[str, Any] = {
    "type": "object",
    "properties": {
        "policy": {"enum": ["EXACT_ID", "ONLY_COMPATIBLE"]},
        "scope_id": {"type": "string", "minLength": 1},
        "absolute_path": {"type": "string", "minLength": 1},
    },
    "required": ["policy"],
    "additionalProperties": False,
    "oneOf": [
        {
            "properties": {"policy": {"const": "EXACT_ID"}},
            "required": ["scope_id"],
            "not": {"required": ["absolute_path"]},
        },
        {
            "properties": {"policy": {"const": "ONLY_COMPATIBLE"}},
            "not": {"anyOf": [{"required": ["scope_id"]}, {"required": ["absolute_path"]}]},
        },
        {
            "required": ["absolute_path"],
            "not": {"required": ["scope_id"]},
        },
    ],
}

UPDATE_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "action": {"enum": ["ACQUIRE", "REFRESH"]},
        "scope": deepcopy(_SCOPE_SELECTOR),
        "base_selector": deepcopy(_SELECTOR),
        "max_entries": {"type": "integer", "minimum": 1, "maximum": 100000},
        "max_duration_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 600},
        "change_limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
        "change_offset": {"type": "integer", "minimum": 0, "default": 0},
    },
    "required": ["action", "scope"],
    "additionalProperties": False,
}

RECOVERY_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"run_id": {"type": "string", "minLength": 1}},
    "required": ["run_id"],
    "additionalProperties": False,
}

CODE_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "phase": {"enum": ["PREFLIGHT", "POSTFLIGHT"]},
        "target_paths": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 256},
            "maxItems": 64,
            "uniqueItems": True,
        },
        "baseline": {"type": "object"},
        "validation_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "status": {"enum": ["PASS", "FAIL", "SKIPPED", "NOT_RUN", "UNKNOWN"]},
                    "exit_code": {
                        "type": ["integer", "null"],
                        "minimum": -255,
                        "maximum": 255,
                    },
                    "output_sha256": {
                        "type": ["string", "null"],
                        "pattern": "^[0-9a-f]{64}$",
                    },
                },
                "required": ["check_id", "status"],
                "additionalProperties": False,
            },
            "maxItems": 32,
        },
    },
    "required": ["phase"],
    "additionalProperties": False,
}

INPUT_SCHEMAS = {
    HISTORY_TOOL: HISTORY_INPUT_SCHEMA,
    DOCUMENT_TOOL: DOCUMENT_INPUT_SCHEMA,
    CODE_TOOL: CODE_INPUT_SCHEMA,
    UPDATE_TOOL: UPDATE_INPUT_SCHEMA,
    RECOVERY_TOOL: RECOVERY_INPUT_SCHEMA,
}

_THREAD_ATTRIBUTION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_name": {"const": THREAD_ATTRIBUTION_SCHEMA_NAME},
        "schema_version": {"const": THREAD_ATTRIBUTION_SCHEMA_VERSION},
        "status": {"enum": ["HOST_BOUND", "HOST_UNAVAILABLE"]},
        "host_kind": {"const": "CODEX"},
        "thread_reference": {
            "type": ["string", "null"],
            "minLength": 1,
            "maxLength": MAX_HOST_SESSION_CHARS,
        },
        "thread_signature": {
            "type": ["string", "null"],
            "pattern": "^[0-9a-f]{64}$",
        },
        "reference_semantics": {"enum": ["ANONYMIZED_CONVERSATION_ID", "UNAVAILABLE"]},
        "source": {"enum": ["MCP_REQUEST_META_OPENAI_SESSION", "NONE"]},
        "source_field": {"const": "openai/session"},
        "verification": {"const": "HOST_REPORTED_NOT_ATTESTED"},
        "correlation_only": {"const": True},
        "authorization_effect": {"const": "NONE"},
        "model_supplied": {"const": False},
        "reason_code": {
            "enum": [
                None,
                "MCP_CLIENT_SESSION_META_ABSENT",
                "MCP_CLIENT_SESSION_META_INVALID",
            ]
        },
    },
    "required": [
        "schema_name",
        "schema_version",
        "status",
        "host_kind",
        "thread_reference",
        "thread_signature",
        "reference_semantics",
        "source",
        "source_field",
        "verification",
        "correlation_only",
        "authorization_effect",
        "model_supplied",
        "reason_code",
    ],
    "additionalProperties": False,
}


def _output_schema(tool_name: str) -> dict[str, Any]:
    common: dict[str, Any] = {
        "schema_name": {"const": ADAPTER_SCHEMA_NAME},
        "schema_version": {"const": ADAPTER_SCHEMA_VERSION},
        "tool_name": {"const": tool_name},
        "risk_class": {"type": "string"},
        "session": {"type": "object"},
        "authority": {"type": "object"},
        "thread_attribution": deepcopy(_THREAD_ATTRIBUTION_OUTPUT_SCHEMA),
        "selection": {"type": "array", "items": {"type": "object"}},
        "exact_integer_encoding": {
            "type": "object",
            "properties": {
                "scheme": {"const": EXACT_INTEGER_ENCODING_SCHEME},
                "decimal_string_paths": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^/result(?:/.*)?$"},
                    "uniqueItems": True,
                },
            },
            "required": ["scheme", "decimal_string_paths"],
            "additionalProperties": False,
        },
    }
    required = [
        "schema_name",
        "schema_version",
        "tool_name",
        "risk_class",
        "status",
        "session",
        "authority",
        "thread_attribution",
        "selection",
        "exact_integer_encoding",
        "result",
        "error",
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    **common,
                    "status": {"const": "OK"},
                    "result": {"type": "object"},
                    "error": {"type": "null"},
                },
                "required": required,
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    **common,
                    "status": {"const": "ERROR"},
                    "result": {"type": "null"},
                    "error": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "minLength": 1},
                            "cause_code": {"type": ["string", "null"]},
                            "message": {"type": "string", "minLength": 1},
                        },
                        "required": ["code", "cause_code", "message"],
                        "additionalProperties": False,
                    },
                },
                "required": required,
                "additionalProperties": False,
            },
        ],
    }


OUTPUT_SCHEMAS = {name: _output_schema(name) for name in TOOL_NAMES}


def tool_descriptors() -> list[Tool]:
    annotations = {
        HISTORY_TOOL: ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        DOCUMENT_TOOL: ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        CODE_TOOL: ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        UPDATE_TOOL: ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        RECOVERY_TOOL: ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    }
    descriptions = {
        HISTORY_TOOL: (
            "Read health, verified Snapshot history, analyses, or changes."
        ),
        DOCUMENT_TOOL: (
            "Read cited local documents, images, audio, or video. Use EVIDENCE for facts, READ "
            "for broad content, EXTRACT_TABLE/FORMULA for native data, and VIEW for layout or a "
            "frame. Depth preserves modality; content_query is literal."
        ),
        CODE_TOOL: (
            "Observe fixed-project Git PREFLIGHT/POSTFLIGHT; never run commands."
        ),
        UPDATE_TOOL: (
            "Acquire or refresh one bounded metadata Snapshot; no user-file change."
        ),
        RECOVERY_TOOL: ("Recover one exact incomplete Run; no user-file change."),
    }
    return [
        Tool(
            name=name,
            description=descriptions[name],
            inputSchema=deepcopy(INPUT_SCHEMAS[name]),
            annotations=annotations[name],
        )
        for name in TOOL_NAMES
    ]
