"""Single-tool schemas for the grant-gated STEWARD Agent Context boundary."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from mcp.types import Tool, ToolAnnotations


SERVER_NAME = "local-steward-agent-context"
SERVER_VERSION = "1"
ADAPTER_SCHEMA_NAME = "local_steward.agent_context_route_result"
ADAPTER_SCHEMA_VERSION = 1
TOOL_NAME = "steward_execute_context_route"
CONFIG_ENVIRONMENT_VARIABLE = "LOCAL_STEWARD_AGENT_MCP_CONFIG"
EXACT_INTEGER_ENCODING_SCHEME = "safe-json-integer-or-decimal-string-v1"
MAX_STRUCTURED_RESULT_BYTES = 393_216
MAX_TEXT_BYTES = 262_144
OPERATION_TIMEOUT_SECONDS = 30.0

SERVER_INSTRUCTIONS = (
    "This server is a dormant STEWARD Context boundary, not a general historical tool. "
    "It admits only BOUNDED_STRUCTURAL_DIAGNOSTIC or "
    "ORDERED_HISTORICAL_CHANGE_EXPLANATION after closed typed routing and exact "
    "single-use grant consumption. Core, incomplete and unsupported requests publish no "
    "Context business result. Successful output includes a deterministic product fact block; "
    "publish that block unaltered before non-authoritative interpretation."
)

_UUID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": (
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}$"
    ),
}

INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "operation_kind": {
            "type": "string",
            "pattern": r"^[A-Z][A-Z0-9_]{0,95}$",
        },
        "ordered_snapshot_ids": {
            "type": "array",
            "items": deepcopy(_UUID_SCHEMA),
            "maxItems": 2,
            "uniqueItems": True,
            "default": [],
        },
        "scope_id": {
            "type": ["string", "null"],
            "minLength": 1,
            "maxLength": 256,
        },
        "path_or_prefix": {
            "type": ["string", "null"],
            "minLength": 1,
            "maxLength": 4096,
        },
        "bounds": {
            "oneOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 12},
                        "offset": {"const": 0, "default": 0},
                    },
                    "required": ["limit"],
                    "additionalProperties": False,
                },
            ]
        },
        "question": {"type": "string", "minLength": 1, "maxLength": 8192},
    },
    "required": ["operation_kind", "question"],
    "additionalProperties": False,
}

_ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "minLength": 1},
        "message": {"type": "string", "minLength": 1},
        "cause_code": {"type": "string", "minLength": 1},
        "missing_fields": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
    },
    "required": ["code", "message"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "schema_name": {"const": ADAPTER_SCHEMA_NAME},
        "schema_version": {"const": ADAPTER_SCHEMA_VERSION},
        "tool_name": {"const": TOOL_NAME},
        "status": {"enum": ["OK", "ERROR"]},
        "route": {"type": "object"},
        "publication": {"type": ["object", "null"]},
        "context_pack": {"type": ["object", "null"]},
        "exact_integer_encoding": {
            "type": "object",
            "properties": {
                "scheme": {"const": EXACT_INTEGER_ENCODING_SCHEME},
                "decimal_string_paths": {
                    "type": "array",
                    "items": {"type": "string", "pattern": r"^/context_pack(?:/.*)?$"},
                    "uniqueItems": True,
                },
            },
            "required": ["scheme", "decimal_string_paths"],
            "additionalProperties": False,
        },
        "fact_block_markdown": {"type": "string"},
        "fact_block_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
        "error": {"oneOf": [{"type": "null"}, deepcopy(_ERROR_SCHEMA)]},
    },
    "required": [
        "schema_name",
        "schema_version",
        "tool_name",
        "status",
        "route",
        "publication",
        "context_pack",
        "exact_integer_encoding",
        "fact_block_markdown",
        "fact_block_sha256",
        "error",
    ],
    "additionalProperties": False,
}

_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def tool_descriptors() -> list[Tool]:
    return [
        Tool(
            name=TOOL_NAME,
            description=(
                "Execute one exact grant-gated STEWARD Context route. Only the two closed "
                "Context operation kinds can succeed; all other routes publish no Context result."
            ),
            inputSchema=deepcopy(INPUT_SCHEMA),
            outputSchema=deepcopy(OUTPUT_SCHEMA),
            annotations=_ANNOTATIONS,
        )
    ]
