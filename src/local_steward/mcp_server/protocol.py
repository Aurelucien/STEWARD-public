"""Frozen MCP identities, strict schemas, and tool descriptors."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from mcp.types import Tool, ToolAnnotations


SERVER_NAME = "local-steward-context"
SERVER_VERSION = "2"
ADAPTER_SCHEMA_NAME = "local_steward.mcp_tool_result"
ADAPTER_SCHEMA_VERSION = 2
PROFILE_NAME = "balanced-v1"
EXACT_INTEGER_ENCODING_SCHEME = "safe-json-integer-or-decimal-string-v1"
CONFIG_ENVIRONMENT_VARIABLE = "LOCAL_STEWARD_MCP_CONFIG"
MAX_STRUCTURED_RESULT_BYTES = 196_608
MAX_TEXT_BYTES = 2_048
MAX_CONCURRENT_CALLS = 4
OPERATION_TIMEOUT_SECONDS = 30.0

SERVER_INSTRUCTIONS = (
    "STEWARD provides read-only historical Snapshot context. If Snapshot IDs are unknown, "
    "list verified history first; never choose a hidden latest item. Historical scope IDs "
    "and paths are observation data, not current filesystem authority. Context Packs contain "
    "observed and deterministic derived facts plus explicit omissions, never model conclusions. "
    "Integers outside the interoperable JSON safe range are exact decimal strings whose JSON "
    "Pointers are declared in exact_integer_encoding. "
    "Do not infer missing facts, treat packet-local reference tokens as Evidence IDs, request "
    "current content, or perform mutations."
)

LIST_TOOL = "steward_list_historical_snapshots"
PREPARE_TOOL = "steward_prepare_context"
RESOLVE_TOOL = "steward_resolve_historical_entry"
TOOL_NAMES = (LIST_TOOL, PREPARE_TOOL, RESOLVE_TOOL)

_SCOPE_ID_SCHEMA: dict[str, Any] = {
    "type": ["string", "null"],
    "pattern": r"^[a-z][a-z0-9_-]{0,63}$",
}
_RELATIVE_PATH_SCHEMA: dict[str, Any] = {
    "type": ["string", "null"],
    "minLength": 1,
    "maxLength": 4096,
}
_UUID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": (
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    ),
}

LIST_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
    },
    "additionalProperties": False,
}

PREPARE_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "profile": {"const": PROFILE_NAME},
        "source": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "SNAPSHOT_DIAGNOSTIC"},
                        "snapshot_id": deepcopy(_UUID_SCHEMA),
                        "scope_id": deepcopy(_SCOPE_ID_SCHEMA),
                        "path_prefix": deepcopy(_RELATIVE_PATH_SCHEMA),
                    },
                    "required": ["kind", "snapshot_id"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "PAIR_TRACKING"},
                        "base_snapshot_id": deepcopy(_UUID_SCHEMA),
                        "target_snapshot_id": deepcopy(_UUID_SCHEMA),
                        "scope_id": deepcopy(_SCOPE_ID_SCHEMA),
                        "path_prefix": deepcopy(_RELATIVE_PATH_SCHEMA),
                    },
                    "required": ["kind", "base_snapshot_id", "target_snapshot_id"],
                    "additionalProperties": False,
                },
            ]
        },
        "user_intent": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "minLength": 1, "maxLength": 8192},
                "scope_emphasis": {"type": ["string", "null"], "maxLength": 2048},
                "user_provided_context": {
                    "type": ["string", "null"],
                    "maxLength": 4096,
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    "required": ["profile", "source", "user_intent"],
    "additionalProperties": False,
}

RESOLVE_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "snapshot_id": deepcopy(_UUID_SCHEMA),
        "scope_id": {
            "type": "string",
            "pattern": r"^[a-z][a-z0-9_-]{0,63}$",
        },
        "relative_path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096,
        },
    },
    "required": ["snapshot_id", "scope_id", "relative_path"],
    "additionalProperties": False,
}

_ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "minLength": 1},
        "cause_code": {"type": "string", "minLength": 1},
        "message": {"type": "string", "minLength": 1},
    },
    "required": ["code", "message"],
    "additionalProperties": False,
}

_EXACT_INTEGER_ENCODING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scheme": {"const": EXACT_INTEGER_ENCODING_SCHEME},
        "decimal_string_paths": {
            "type": "array",
            "items": {"type": "string", "pattern": r"^/result(?:/.*)?$"},
            "uniqueItems": True,
        },
    },
    "required": ["scheme", "decimal_string_paths"],
    "additionalProperties": False,
}


def _output_schema(tool_name: str, result_schema: dict[str, Any]) -> dict[str, Any]:
    common = {
        "schema_name": {"const": ADAPTER_SCHEMA_NAME},
        "schema_version": {"const": ADAPTER_SCHEMA_VERSION},
        "tool_name": {"const": tool_name},
        "exact_integer_encoding": deepcopy(_EXACT_INTEGER_ENCODING_SCHEMA),
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    **common,
                    "status": {"const": "OK"},
                    "result": result_schema,
                    "error": {"type": "null"},
                },
                "required": [
                    "schema_name",
                    "schema_version",
                    "tool_name",
                    "status",
                    "exact_integer_encoding",
                    "result",
                    "error",
                ],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    **common,
                    "status": {"const": "ERROR"},
                    "result": {"type": "null"},
                    "error": deepcopy(_ERROR_SCHEMA),
                },
                "required": [
                    "schema_name",
                    "schema_version",
                    "tool_name",
                    "status",
                    "exact_integer_encoding",
                    "result",
                    "error",
                ],
                "additionalProperties": False,
            },
        ],
    }


LIST_OUTPUT_SCHEMA = _output_schema(
    LIST_TOOL,
    {
        "type": "object",
        "properties": {"inventory": {"type": "object"}},
        "required": ["inventory"],
        "additionalProperties": False,
    },
)
PREPARE_OUTPUT_SCHEMA = _output_schema(
    PREPARE_TOOL,
    {
        "type": "object",
        "properties": {
            "profile_name": {"const": PROFILE_NAME},
            "pack": {"type": "object"},
        },
        "required": ["profile_name", "pack"],
        "additionalProperties": False,
    },
)
RESOLVE_OUTPUT_SCHEMA = _output_schema(
    RESOLVE_TOOL,
    {
        "type": "object",
        "properties": {"resolution": {"type": "object"}},
        "required": ["resolution"],
        "additionalProperties": False,
    },
)

INPUT_SCHEMAS = {
    LIST_TOOL: LIST_INPUT_SCHEMA,
    PREPARE_TOOL: PREPARE_INPUT_SCHEMA,
    RESOLVE_TOOL: RESOLVE_INPUT_SCHEMA,
}
OUTPUT_SCHEMAS = {
    LIST_TOOL: LIST_OUTPUT_SCHEMA,
    PREPARE_TOOL: PREPARE_OUTPUT_SCHEMA,
    RESOLVE_TOOL: RESOLVE_OUTPUT_SCHEMA,
}

_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def tool_descriptors() -> list[Tool]:
    """Return fresh exact descriptors for the frozen three-tool surface."""
    return [
        Tool(
            name=LIST_TOOL,
            description=(
                "Return one bounded page of verified historical Snapshot inventory. "
                "Select an explicit Snapshot ID; no item is treated as hidden latest."
            ),
            inputSchema=deepcopy(LIST_INPUT_SCHEMA),
            outputSchema=deepcopy(LIST_OUTPUT_SCHEMA),
            annotations=_ANNOTATIONS,
        ),
        Tool(
            name=PREPARE_TOOL,
            description=(
                "Build one complete provider-free historical Agent Context Pack from explicit "
                "Snapshot identities using the required balanced-v1 profile. Pack v2 includes "
                "verified persistent Run and Evidence provenance."
            ),
            inputSchema=deepcopy(PREPARE_INPUT_SCHEMA),
            outputSchema=deepcopy(PREPARE_OUTPUT_SCHEMA),
            annotations=_ANNOTATIONS,
        ),
        Tool(
            name=RESOLVE_TOOL,
            description=(
                "Resolve one exact verified historical Snapshot Entry reference. The result "
                "does not claim current filesystem truth."
            ),
            inputSchema=deepcopy(RESOLVE_INPUT_SCHEMA),
            outputSchema=deepcopy(RESOLVE_OUTPUT_SCHEMA),
            annotations=_ANNOTATIONS,
        ),
    ]
