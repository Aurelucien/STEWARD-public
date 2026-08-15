"""Stateless bounded dispatcher from strict MCP calls to public STEWARD products."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

import anyio
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp.types import CallToolResult, TextContent

from local_steward.agent_context import (
    AgentContextError,
    agent_context_pack_machine_object,
    prepare_agent_context,
)
from local_steward.config import load_config
from local_steward.errors import ConfigurationError
from local_steward.file_agent import (
    AgentToolError,
    SharedToolBudget,
    ToolBudgetLimits,
    ToolExecutionContext,
    serialize_envelope,
    steward_list_snapshots,
    steward_resolve_entry_reference,
)

from .profile import McpArgumentError, build_pack_request, decode_entry_reference
from .protocol import (
    ADAPTER_SCHEMA_NAME,
    ADAPTER_SCHEMA_VERSION,
    EXACT_INTEGER_ENCODING_SCHEME,
    INPUT_SCHEMAS,
    LIST_TOOL,
    MAX_CONCURRENT_CALLS,
    MAX_STRUCTURED_RESULT_BYTES,
    MAX_TEXT_BYTES,
    OPERATION_TIMEOUT_SECONDS,
    OUTPUT_SCHEMAS,
    PREPARE_TOOL,
    RESOLVE_TOOL,
    TOOL_NAMES,
)


JsonObject = dict[str, Any]
Operation = Callable[[], JsonObject]
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991


def canonical_json(value: object) -> bytes:
    """Serialize adapter-local JSON deterministically without product internals."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _facade_machine_object(value: object) -> JsonObject:
    decoded = json.loads(serialize_envelope(value))
    if not isinstance(decoded, dict):
        raise TypeError
    return decoded


def model_safe_json(
    value: Any, *, pointer: str = "/result"
) -> tuple[Any, tuple[str, ...]]:
    """Encode integers outside the interoperable JSON range as exact decimal strings."""
    if type(value) is int and abs(value) > MAX_SAFE_JSON_INTEGER:
        return str(value), (pointer,)
    if isinstance(value, list):
        list_values: list[Any] = []
        list_paths: list[str] = []
        for index, item in enumerate(value):
            encoded, item_paths = model_safe_json(item, pointer=f"{pointer}/{index}")
            list_values.append(encoded)
            list_paths.extend(item_paths)
        return list_values, tuple(list_paths)
    if isinstance(value, dict):
        object_values: dict[Any, Any] = {}
        object_paths: list[str] = []
        if not all(isinstance(key, str) for key in value):
            raise TypeError("MCP JSON object keys must be strings")
        for key in sorted(value):
            item = value[key]
            token = key.replace("~", "~0").replace("/", "~1")
            encoded, item_paths = model_safe_json(item, pointer=f"{pointer}/{token}")
            object_values[key] = encoded
            object_paths.extend(item_paths)
        return object_values, tuple(object_paths)
    return value, ()


def _integer_encoding(paths: tuple[str, ...]) -> JsonObject:
    return {
        "scheme": EXACT_INTEGER_ENCODING_SCHEME,
        "decimal_string_paths": list(paths),
    }


def _safe_message(code: str) -> str:
    messages = {
        "STEWARD_MCP_TOOL_NOT_FOUND": "The requested STEWARD MCP tool is unavailable.",
        "STEWARD_MCP_ARGUMENT_INVALID": "The STEWARD MCP arguments are invalid.",
        "STEWARD_MCP_RESOURCE_LIMIT": "The STEWARD MCP result exceeds the governed limit.",
        "STEWARD_MCP_BUSY": "The STEWARD MCP server is at its concurrent-call limit.",
        "STEWARD_MCP_TIMEOUT": "The STEWARD MCP operation exceeded its time limit.",
        "STEWARD_MCP_SOURCE_UNAVAILABLE": "The configured STEWARD source is unavailable.",
        "STEWARD_MCP_UNAVAILABLE": "The STEWARD MCP operation is unavailable.",
    }
    return messages.get(code, "The STEWARD operation failed safely.")


class McpDispatcher:
    """Execute at most four independent read-only operations with atomic publication."""

    def __init__(
        self,
        config_path: Path,
        *,
        max_result_bytes: int = MAX_STRUCTURED_RESULT_BYTES,
        timeout_seconds: float = OPERATION_TIMEOUT_SECONDS,
        max_concurrent_calls: int = MAX_CONCURRENT_CALLS,
    ) -> None:
        self._config_path = config_path
        self._max_result_bytes = max_result_bytes
        self._timeout_seconds = timeout_seconds
        self._max_concurrent_calls = max_concurrent_calls
        self._active_calls = 0
        self._active_lock = threading.Lock()

    async def dispatch(self, tool_name: str, arguments: object) -> CallToolResult:
        """Validate untouched arguments, then execute one bounded product call."""
        if tool_name not in TOOL_NAMES:
            return self._error("unknown", "STEWARD_MCP_TOOL_NOT_FOUND")
        try:
            schema_valid = isinstance(arguments, dict) and self._schema_valid(
                tool_name, arguments
            )
        except Exception:
            schema_valid = False
        if not schema_valid:
            return self._error(tool_name, "STEWARD_MCP_ARGUMENT_INVALID")
        assert isinstance(arguments, dict)
        try:
            operation = self._operation(tool_name, arguments)
        except McpArgumentError:
            return self._error(tool_name, "STEWARD_MCP_ARGUMENT_INVALID")
        except Exception:
            return self._error(tool_name, "STEWARD_MCP_UNAVAILABLE")

        with self._active_lock:
            if self._active_calls >= self._max_concurrent_calls:
                return self._error(tool_name, "STEWARD_MCP_BUSY")
            self._active_calls += 1

        def counted_operation() -> JsonObject:
            try:
                return operation()
            finally:
                with self._active_lock:
                    self._active_calls -= 1

        try:
            with anyio.fail_after(self._timeout_seconds):
                result = await anyio.to_thread.run_sync(
                    counted_operation, abandon_on_cancel=True
                )
        except TimeoutError:
            return self._error(tool_name, "STEWARD_MCP_TIMEOUT")
        except AgentContextError as error:
            cause = error.cause_code if isinstance(error.cause_code, str) else None
            return self._error(tool_name, error.code, cause_code=cause)
        except AgentToolError as error:
            return self._error(tool_name, error.code)
        except ConfigurationError as error:
            return self._error(
                tool_name,
                "STEWARD_MCP_SOURCE_UNAVAILABLE",
                cause_code=error.code,
            )
        except Exception:
            return self._error(tool_name, "STEWARD_MCP_UNAVAILABLE")
        try:
            return self._success(tool_name, result)
        except Exception:
            return self._error(tool_name, "STEWARD_MCP_UNAVAILABLE")

    def _schema_valid(self, tool_name: str, arguments: JsonObject) -> bool:
        return not any(Draft202012Validator(INPUT_SCHEMAS[tool_name]).iter_errors(arguments))

    def _operation(self, tool_name: str, arguments: JsonObject) -> Operation:
        if tool_name == LIST_TOOL:
            limit = arguments.get("limit", 10)
            offset = arguments.get("offset", 0)

            def list_operation() -> JsonObject:
                context = self._facade_context(max_items=20)
                value = steward_list_snapshots(context, limit=limit, offset=offset)
                return {"inventory": _facade_machine_object(value)}

            return list_operation
        if tool_name == PREPARE_TOOL:
            request = build_pack_request(arguments)

            def prepare_operation() -> JsonObject:
                pack = prepare_agent_context(load_config(self._config_path), request)
                return {
                    "profile_name": "balanced-v1",
                    "pack": agent_context_pack_machine_object(pack),
                }

            return prepare_operation
        if tool_name == RESOLVE_TOOL:
            snapshot_id, scope_id, relative_path = decode_entry_reference(arguments)

            def resolve_operation() -> JsonObject:
                context = self._facade_context(max_items=1)
                value = steward_resolve_entry_reference(
                    context, snapshot_id, scope_id, relative_path
                )
                return {"resolution": _facade_machine_object(value)}

            return resolve_operation
        raise McpArgumentError

    def _facade_context(self, *, max_items: int) -> ToolExecutionContext:
        limits = ToolBudgetLimits(
            max_steward_calls_per_turn=1,
            max_items_per_call=max_items,
            max_items_per_turn=max_items,
            max_serialized_bytes_per_call=MAX_STRUCTURED_RESULT_BYTES,
            max_serialized_bytes_per_turn=MAX_STRUCTURED_RESULT_BYTES,
            max_elapsed_ms_per_call=int(OPERATION_TIMEOUT_SECONDS * 1000),
            max_elapsed_ms_per_turn=int(OPERATION_TIMEOUT_SECONDS * 1000),
        )
        return ToolExecutionContext(
            config=load_config(self._config_path),
            budget=SharedToolBudget(limits),
        )

    def _success(self, tool_name: str, result: JsonObject) -> CallToolResult:
        safe_result, exact_paths = model_safe_json(result)
        if not isinstance(safe_result, dict):
            return self._error(tool_name, "STEWARD_MCP_UNAVAILABLE")
        envelope: JsonObject = {
            "schema_name": ADAPTER_SCHEMA_NAME,
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "tool_name": tool_name,
            "status": "OK",
            "exact_integer_encoding": _integer_encoding(exact_paths),
            "result": safe_result,
            "error": None,
        }
        if len(canonical_json(envelope)) > self._max_result_bytes:
            return self._error(tool_name, "STEWARD_MCP_RESOURCE_LIMIT")
        if not self._output_valid(tool_name, envelope):
            return self._error(tool_name, "STEWARD_MCP_UNAVAILABLE")
        text = self._success_text(tool_name, safe_result)
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            return self._error(tool_name, "STEWARD_MCP_RESOURCE_LIMIT")
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=envelope,
            isError=False,
        )

    def _error(
        self, tool_name: str, code: str, *, cause_code: str | None = None
    ) -> CallToolResult:
        error: JsonObject = {"code": code, "message": _safe_message(code)}
        if cause_code:
            error["cause_code"] = cause_code
        envelope: JsonObject = {
            "schema_name": ADAPTER_SCHEMA_NAME,
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "tool_name": tool_name,
            "status": "ERROR",
            "exact_integer_encoding": _integer_encoding(()),
            "result": None,
            "error": error,
        }
        if tool_name in OUTPUT_SCHEMAS and not self._output_valid(tool_name, envelope):
            envelope["error"] = {
                "code": "STEWARD_MCP_UNAVAILABLE",
                "message": _safe_message("STEWARD_MCP_UNAVAILABLE"),
            }
        text = f"ERROR [{envelope['error']['code']}]: {envelope['error']['message']}"
        assert len(text.encode("utf-8")) <= MAX_TEXT_BYTES
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=envelope,
            isError=True,
        )

    def _output_valid(self, tool_name: str, envelope: JsonObject) -> bool:
        return not any(
            Draft202012Validator(OUTPUT_SCHEMAS[tool_name]).iter_errors(envelope)
        )

    @staticmethod
    def _success_text(tool_name: str, result: JsonObject) -> str:
        if tool_name == LIST_TOOL:
            return "OK: returned verified historical Snapshot inventory; use structuredContent."
        if tool_name == PREPARE_TOOL:
            pack = result.get("pack", {})
            digest = pack.get("pack_digest", "validated") if isinstance(pack, dict) else "validated"
            return f"OK: returned Agent Context Pack {digest}; use structuredContent."
        return (
            "OK: resolved one verified historical Snapshot Entry; current facts require "
            "recheck; use structuredContent."
        )
