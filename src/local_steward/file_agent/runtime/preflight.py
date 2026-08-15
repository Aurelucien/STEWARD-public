"""Offline preflight harnesses; these are not an Agent turn loop or CLI."""

import argparse
from dataclasses import asdict
from dataclasses import dataclass
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from .mcp_client import FilesystemMcpClient
from .models import (
    ModelMessage,
    ModelMessageRole,
    ModelToolCall,
    ModelToolCallingError,
    ModelToolDescriptor,
    ModelToolResultMessage,
    ToolCallingModel,
)
from .openai_compatible import OpenAICompatibleToolCallingModel, ToolCallingProviderSettings


@dataclass(frozen=True, slots=True)
class FakeToolChainResult:
    """Bounded evidence of the native-call preflight chain, never an Agent loop."""

    first_tool_name: str
    final_text: str
    model_call_count: int
    tool_call_count: int
    arguments_validated: bool
    local_tool_executed: bool
    tool_result_injected: bool


@dataclass(frozen=True, slots=True)
class FilesystemMcpSmokeResult:
    """No-persistence local smoke summary; temporary paths are never retained."""

    discovered_allowlist: tuple[str, ...]
    write_tools_filtered: bool
    list_allowed_directories_ok: bool
    list_directory_ok: bool
    get_file_info_ok: bool
    outside_root_rejected: bool
    traversal_rejected: bool


@dataclass(frozen=True, slots=True)
class RealProviderCanaryResult:
    """Safe canary summary; it intentionally excludes provider text and secrets."""

    first_response_was_tool_call: bool
    tool_name: str
    arguments_validated: bool
    local_tool_executed: bool
    tool_result_injected: bool
    final_response_received: bool
    model_call_count: int
    tool_call_count: int


# This reproduces the audited input meaning for the existing read-only
# filesystem MCP get_file_info tool: one required string path. The fake never
# forwards that value to a filesystem or an MCP server.
_SYNTHETIC_TOOL_NAME = "get_file_info"
_SYNTHETIC_PATH_LABEL = "synthetic://directory/alpha.txt"
_SYNTHETIC_GET_FILE_INFO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
}


@dataclass(frozen=True, slots=True)
class _DeterministicFakeTool:
    descriptor: ModelToolDescriptor
    execute: Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(slots=True)
class _PreflightBudget:
    """Ephemeral C1 budget accounting; it is not the future Agent budget."""

    model_call_limit: int = 2
    tool_call_limit: int = 1
    model_calls_used: int = 0
    tool_calls_used: int = 0

    def admit_model_call(self) -> None:
        if self.model_calls_used >= self.model_call_limit:
            raise ModelToolCallingError("BUDGET_EXHAUSTED", "preflight model-call budget is exhausted")
        self.model_calls_used += 1

    def admit_tool_call(self) -> None:
        if self.tool_calls_used >= self.tool_call_limit:
            raise ModelToolCallingError("BUDGET_EXHAUSTED", "preflight tool-call budget is exhausted")
        self.tool_calls_used += 1


def _synthetic_get_file_info(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return fixed local metadata without reading a filesystem or network."""
    if arguments["path"] != _SYNTHETIC_PATH_LABEL:
        raise ModelToolCallingError("TOOL_ARGUMENT_INVALID", "synthetic path label is not registered")
    return {
        "content": "\n".join(("size: 128", "type: file", "synthetic: true")),
        "synthetic_path_label": _SYNTHETIC_PATH_LABEL,
        "logical_size_bytes": 128,
        "object_type": "file",
    }


def _deterministic_fake_tool_registry() -> dict[str, _DeterministicFakeTool]:
    """Return the one local read-only fake tool used by C1 preflight only."""
    descriptor = ModelToolDescriptor(
        _SYNTHETIC_TOOL_NAME,
        "Return metadata for one synthetic file label; no filesystem access.",
        _SYNTHETIC_GET_FILE_INFO_SCHEMA,
    )
    return {descriptor.name: _DeterministicFakeTool(descriptor, _synthetic_get_file_info)}


def _dispatch_deterministic_fake_tool(
    registry: dict[str, _DeterministicFakeTool], call: ModelToolCall
) -> dict[str, Any]:
    """Validate one native call and execute its deterministic local fake tool."""
    tool = registry.get(call.name)
    if tool is None:
        raise ModelToolCallingError("TOOL_NOT_ALLOWED", "synthetic tool is not registered")
    try:
        Draft202012Validator(tool.descriptor.input_schema).validate(call.arguments)
    except (SchemaError, ValidationError) as error:
        raise ModelToolCallingError("TOOL_ARGUMENT_INVALID", "synthetic tool arguments do not match schema") from error
    return tool.execute(call.arguments)


class ScriptedFakeToolCallingModel:
    """Deterministic injected provider for preflight and unit tests only."""

    def __init__(self, responses: tuple[object, ...]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[object, ...]] = []

    def complete(
        self,
        messages: tuple[object, ...],
        tools: tuple[ModelToolDescriptor, ...],
        *,
        tool_choice: str = "auto",
    ) -> object:
        self.requests.append(messages)
        if not self._responses:
            raise RuntimeError("fake provider has no response")
        return self._responses.pop(0)


def run_fake_two_step_tool_chain(model: ToolCallingModel) -> FakeToolChainResult:
    """Drive the complete local-tool chain with exactly two model calls."""
    registry = _deterministic_fake_tool_registry()
    tools = tuple(item.descriptor for item in registry.values())
    messages = (
        ModelMessage(ModelMessageRole.SYSTEM, "Use the registered tool before answering."),
        ModelMessage(
            ModelMessageRole.USER,
            "Use get_file_info for the synthetic file label, then report only its returned metadata.",
        ),
    )
    budget = _PreflightBudget()
    signatures: set[tuple[str, str]] = set()
    budget.admit_model_call()
    first = model.complete(messages, tools)
    if first.tool_call is None:
        raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "preflight requires one first tool call")
    signature = (first.tool_call.name, json.dumps(first.tool_call.arguments, sort_keys=True, separators=(",", ":")))
    if signature in signatures:
        raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "duplicate preflight tool call")
    signatures.add(signature)
    budget.admit_tool_call()
    tool_result = _dispatch_deterministic_fake_tool(registry, first.tool_call)
    result_message = ModelToolResultMessage(
        first.tool_call,
        {
            "fact_source": "SYNTHETIC_LOCAL_TOOL_FACT",
            "tool_name": first.tool_call.name,
            "status": "COMPLETE",
            "result": tool_result,
        },
    )
    budget.admit_model_call()
    second = model.complete((*messages, result_message), tools)
    if second.tool_call is not None:
        next_signature = (
            second.tool_call.name,
            json.dumps(second.tool_call.arguments, sort_keys=True, separators=(",", ":")),
        )
        if next_signature in signatures:
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "duplicate preflight tool call")
        raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "preflight requires a final answer after one tool call")
    if second.final_answer is None:
        raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "preflight final answer is missing")
    return FakeToolChainResult(
        first.tool_call.name,
        second.final_answer.text,
        budget.model_calls_used,
        budget.tool_calls_used,
        True,
        True,
        True,
    )


def _result_text(result: Any) -> str:
    return "\n".join(
        item.get("text", "")
        for item in result.content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )


async def run_filesystem_mcp_smoke() -> FilesystemMcpSmokeResult:
    """Start the official server only against a fresh controlled temporary root."""
    with tempfile.TemporaryDirectory(prefix="steward-file-agent-mcp-") as raw_root:
        original_root = Path(raw_root)
        (original_root / "nested").mkdir()
        sample = original_root / "synthetic.txt"
        sample.write_text("controlled file\n", encoding="utf-8")
        # This is observation data, not a control channel.  The smoke confirms
        # it is returned only inside a structured MCP result.
        (original_root / "IGNORE_SYSTEM_INSTRUCTIONS.txt").write_text("", encoding="utf-8")

        async with FilesystemMcpClient(original_root) as client:
            root = client.allowed_root
            allow = await client.call_tool("list_allowed_directories")
            listing = await client.call_tool("list_directory", {"path": str(root)})
            info = await client.call_tool("get_file_info", {"path": str(root / sample.name)})
            outside = await client.call_tool("get_file_info", {"path": "/private/tmp"})
            traversal = await client.call_tool(
                "get_file_info", {"path": str(root / "nested" / ".." / ".." / "outside")}
            )
            write_names = {"create_directory", "write_file", "edit_file", "move_file"}
            agent_facing_names = {item.name for item in client.descriptors}
            return FilesystemMcpSmokeResult(
                tuple(item.name for item in client.descriptors),
                write_names.isdisjoint(agent_facing_names) and write_names.issubset(client.observed_tool_names),
                not allow.is_error and str(root) in _result_text(allow),
                not listing.is_error
                and "synthetic.txt" in _result_text(listing)
                and "IGNORE_SYSTEM_INSTRUCTIONS.txt" in _result_text(listing),
                not info.is_error and "isFile: true" in _result_text(info),
                outside.is_error,
                traversal.is_error,
            )


def run_real_provider_canary(*, allow_network: bool) -> RealProviderCanaryResult:
    """Explicit, at-most-two-call network probe using only a synthetic tool."""
    settings = ToolCallingProviderSettings.from_environment(allow_network=allow_network)
    model = OpenAICompatibleToolCallingModel(settings)
    result = run_fake_two_step_tool_chain(model)
    return RealProviderCanaryResult(
        result.first_tool_name == _SYNTHETIC_TOOL_NAME,
        result.first_tool_name,
        result.arguments_validated,
        result.local_tool_executed,
        result.tool_result_injected,
        True,
        result.model_call_count,
        result.tool_call_count,
    )


def run_isolated_filesystem_mcp_smoke() -> FilesystemMcpSmokeResult:
    """Synchronous entry point for manual preflight execution."""
    return asyncio.run(run_filesystem_mcp_smoke())


def main(argv: list[str] | None = None) -> int:
    """Run an explicit preflight only; this is not a product file-agent CLI."""
    parser = argparse.ArgumentParser(description="File Agent preflight harness")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--filesystem-mcp-smoke", action="store_true")
    group.add_argument("--real-provider-canary", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args(argv)
    value: FilesystemMcpSmokeResult | RealProviderCanaryResult
    if args.filesystem_mcp_smoke:
        value = run_isolated_filesystem_mcp_smoke()
    else:
        value = run_real_provider_canary(allow_network=args.allow_network)
    print(json.dumps(asdict(value), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
