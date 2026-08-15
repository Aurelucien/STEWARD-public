"""Offline coverage for the self-owned, serial read-only Agent runtime."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from local_steward.file_agent import SharedToolBudget, ToolExecutionContext
from local_steward.file_agent.runtime import (
    FILESYSTEM_READ_ONLY_ALLOWLIST,
    AgentRuntime,
    AgentTurnRequest,
    CombinedBudget,
    CombinedBudgetLimits,
    McpClientError,
    McpToolDescriptor,
    McpToolResult,
    RuntimeFailure,
    RuntimeTool,
    RuntimeToolResult,
    ScopeBinding,
    ScopeBindings,
    SourceFamily,
    StewardRuntimeDependencies,
    ToolRegistry,
    register_filesystem_tools,
    register_steward_tools,
)
from local_steward.file_agent.runtime.models import (
    ModelFinalAnswer,
    ModelToolBatchResultMessage,
    ModelToolCall,
    ModelToolResultMessage,
    ModelTurnResult,
)
from local_steward.file_agent.runtime.preflight import ScriptedFakeToolCallingModel
from local_steward.observation_projection import (
    ProjectionBudget,
    ProjectionPolicy,
)

from .test_file_agent_facade import _fixture


def _run(runtime: AgentRuntime, responses: tuple[object, ...]):
    return asyncio.run(runtime.run(AgentTurnRequest("offline test"), ScriptedFakeToolCallingModel(responses)))


def _tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RuntimeTool(
            "local_echo",
            "Return its validated value without external access.",
            {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
            SourceFamily.SYNTHETIC,
            lambda value: RuntimeToolResult(SourceFamily.SYNTHETIC, {"value": value["value"]}),
        )
    )
    return registry


def _call(call_id: str = "call-1", value: str = "alpha") -> ModelTurnResult:
    return ModelTurnResult(tool_call=ModelToolCall(call_id, "local_echo", {"value": value}))


def test_runtime_executes_serial_chain_and_injects_tagged_tool_result() -> None:
    fake = ScriptedFakeToolCallingModel((_call(), ModelTurnResult(final_answer=ModelFinalAnswer("done"))))
    result = asyncio.run(AgentRuntime(_tool_registry()).run(AgentTurnRequest("x"), fake))

    assert result.final_answer == "done" and result.failure_code is None
    assert result.budget.usage.model_calls_used == 2
    assert result.budget.usage.synthetic_tool_calls_used == 1
    assert result.traces[0].source_family == SourceFamily.SYNTHETIC
    injected = fake.requests[1][-1]
    assert isinstance(injected, ModelToolResultMessage)
    assert injected.result["fact_source"] == "SYNTHETIC"


def test_runtime_preserves_a_preamble_on_a_single_tool_turn_and_waits_for_final_answer() -> None:
    fake = ScriptedFakeToolCallingModel(
        (
            ModelTurnResult(
                tool_call=ModelToolCall("call-1", "local_echo", {"value": "alpha"}),
                assistant_preamble="I will inspect the registered fact.",
            ),
            ModelTurnResult(final_answer=ModelFinalAnswer("done")),
        )
    )

    result = asyncio.run(AgentRuntime(_tool_registry()).run(AgentTurnRequest("x"), fake))

    assert result.final_answer == "done" and result.budget.usage.model_calls_used == 2
    injected = fake.requests[1][-1]
    assert isinstance(injected, ModelToolResultMessage)
    assert injected.assistant_preamble == "I will inspect the registered fact."


def test_runtime_preserves_a_preamble_on_a_batch_without_changing_provider_order() -> None:
    fake = ScriptedFakeToolCallingModel(
        (
            ModelTurnResult(
                tool_calls=(
                    ModelToolCall("one", "local_echo", {"value": "a"}),
                    ModelToolCall("two", "local_echo", {"value": "b"}),
                ),
                assistant_preamble="I will inspect both registered facts.",
            ),
            ModelTurnResult(final_answer=ModelFinalAnswer("done")),
        )
    )

    result = asyncio.run(AgentRuntime(_tool_registry()).run(AgentTurnRequest("x"), fake))

    assert result.final_answer == "done"
    assert [trace.arguments["value"] for trace in result.traces] == ["a", "b"]
    injected = fake.requests[1][-1]
    assert isinstance(injected, ModelToolBatchResultMessage)
    assert injected.assistant_preamble == "I will inspect both registered facts."
    assert [item.provider_call_id for item in injected.results] == ["one", "two"]


def test_runtime_executes_bounded_batch_in_provider_order_and_reinjects_all_results() -> None:
    fake = ScriptedFakeToolCallingModel((
        ModelTurnResult(tool_calls=(
            ModelToolCall("one", "local_echo", {"value": "a"}),
            ModelToolCall("two", "local_echo", {"value": "b"}),
        )),
        ModelTurnResult(final_answer=ModelFinalAnswer("done")),
    ))
    result = asyncio.run(AgentRuntime(_tool_registry()).run(AgentTurnRequest("x"), fake))
    assert result.final_answer == "done"
    assert [trace.arguments["value"] for trace in result.traces] == ["a", "b"]
    injected = fake.requests[1][-1]
    assert isinstance(injected, ModelToolBatchResultMessage)
    assert [item.provider_call_id for item in injected.results] == ["one", "two"]


def test_runtime_accepts_a_direct_final_answer_without_tool_execution() -> None:
    result = _run(
        AgentRuntime(_tool_registry()),
        (ModelTurnResult(final_answer=ModelFinalAnswer("direct offline answer")),),
    )
    assert result.final_answer == "direct offline answer"
    assert not result.traces and result.budget.usage.model_calls_used == 1


@pytest.mark.parametrize(
    ("response", "failure"),
    [
        (ModelTurnResult(tool_call=ModelToolCall("x", "not_registered", {})), "TOOL_NOT_ALLOWED"),
        (ModelTurnResult(tool_call=ModelToolCall("x", "local_echo", {})), "TOOL_ARGUMENT_INVALID"),
    ],
)
def test_runtime_rejects_unknown_tool_and_invalid_arguments(response: ModelTurnResult, failure: str) -> None:
    result = _run(AgentRuntime(_tool_registry()), (response,))
    assert result.failure_code == failure
    assert result.traces[0].failure_code == failure


def test_runtime_rejects_duplicate_signature_even_with_another_provider_call_id() -> None:
    result = _run(
        AgentRuntime(_tool_registry()),
        (_call("one"), _call("two"), ModelTurnResult(final_answer=ModelFinalAnswer("never"))),
    )
    assert result.failure_code == "MODEL_TOOL_CALL_INVALID"
    assert len(result.traces) == 2 and result.traces[-1].failure_code == "MODEL_TOOL_CALL_INVALID"


def test_runtime_enforces_model_and_tool_limits_and_accounts_results() -> None:
    model_limited = _run(
        AgentRuntime(_tool_registry(), CombinedBudget(CombinedBudgetLimits(max_model_calls=1))),
        (_call(),),
    )
    assert model_limited.failure_code == "AGENT_STEP_LIMIT_REACHED"
    assert model_limited.budget.usage.model_calls_used == 1

    tool_limited = _run(
        AgentRuntime(_tool_registry(), CombinedBudget(CombinedBudgetLimits(max_total_tool_calls=1))),
        (_call("one", "a"), _call("two", "b")),
    )
    assert tool_limited.failure_code == "AGENT_STEP_LIMIT_REACHED"
    assert tool_limited.budget.usage.total_tool_calls_used == 1


def test_runtime_classifies_model_failure_without_provider_or_network() -> None:
    class FailingModel:
        def complete(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("offline fake failure")

    result = asyncio.run(AgentRuntime(_tool_registry()).run(AgentTurnRequest("x"), FailingModel()))  # type: ignore[arg-type]
    assert result.failure_code == "MODEL_CALL_FAILED"


def test_runtime_rejects_missing_final_or_tool_response_shape() -> None:
    class MalformedModel:
        def complete(self, *args: object, **kwargs: object) -> object:
            return object()

    result = asyncio.run(AgentRuntime(_tool_registry()).run(AgentTurnRequest("x"), MalformedModel()))  # type: ignore[arg-type]
    assert result.failure_code == "MODEL_TOOL_CALL_INVALID"


class _FakeFilesystemPort:
    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._descriptors = tuple(
            McpToolDescriptor(
                name,
                {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False},
                True,
            )
            for name in FILESYSTEM_READ_ONLY_ALLOWLIST
        )

    @property
    def descriptors(self) -> tuple[McpToolDescriptor, ...]:
        return self._descriptors

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> McpToolResult:
        self.calls.append((name, arguments))
        if self.fail:
            raise McpClientError(self.fail, "fake failure")
        return McpToolResult(name, ({"type": "text", "text": "offline fake"},), None, False)


def test_filesystem_registry_uses_only_six_read_only_descriptors_and_no_lifecycle() -> None:
    port = _FakeFilesystemPort()
    registry = ToolRegistry()
    register_filesystem_tools(registry, port)
    assert tuple(tool.name for tool in registry.tools) == FILESYSTEM_READ_ONLY_ALLOWLIST
    result = _run(
        AgentRuntime(registry),
        (
            ModelTurnResult(tool_call=ModelToolCall("call", "get_file_info", {"path": "/fake"})),
            ModelTurnResult(final_answer=ModelFinalAnswer("current metadata reviewed")),
        ),
    )
    assert result.final_answer and result.traces[0].source_family == SourceFamily.FILESYSTEM_CURRENT
    assert port.calls == [("get_file_info", {"path": "/fake"})]


def test_filesystem_mcp_unavailable_is_classified_and_can_be_reported_to_model() -> None:
    port = _FakeFilesystemPort(fail="FILESYSTEM_MCP_UNAVAILABLE")
    registry = ToolRegistry()
    register_filesystem_tools(registry, port)
    result = _run(
        AgentRuntime(registry),
        (
            ModelTurnResult(tool_call=ModelToolCall("call", "get_file_info", {"path": "/fake"})),
            ModelTurnResult(final_answer=ModelFinalAnswer("current lookup unavailable")),
        ),
    )
    assert result.final_answer == "current lookup unavailable"
    assert result.traces[0].failure_code == "FILESYSTEM_MCP_UNAVAILABLE"


def test_filesystem_tool_failure_is_classified_without_starting_mcp() -> None:
    port = _FakeFilesystemPort(fail="FILESYSTEM_TOOL_FAILED")
    registry = ToolRegistry()
    register_filesystem_tools(registry, port)
    result = _run(
        AgentRuntime(registry),
        (
            ModelTurnResult(tool_call=ModelToolCall("call", "get_file_info", {"path": "/fake"})),
            ModelTurnResult(final_answer=ModelFinalAnswer("tool failure noted")),
        ),
    )
    assert result.final_answer == "tool failure noted"
    assert result.traces[0].failure_code == "FILESYSTEM_TOOL_FAILED"


def test_scope_binding_is_default_deny_and_rejects_traversal_and_root_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    bindings = ScopeBindings((ScopeBinding("managed", root),), (str(root),), ("managed",))
    assert bindings.require("managed").resolve_relative_path("dir/file.txt") == str(root / "dir/file.txt")
    for value in ("/outside", "../outside", "dir/../outside", "", "dir//file"):
        with pytest.raises(RuntimeFailure, match="SCOPE_BINDING_FAILED"):
            bindings.require("managed").resolve_relative_path(value)
    with pytest.raises(RuntimeFailure, match="SCOPE_BINDING_FAILED"):
        bindings.require("unknown")
    with pytest.raises(RuntimeFailure, match="SCOPE_BINDING_FAILED"):
        ScopeBindings((ScopeBinding("managed", root),), (str(tmp_path / "other"),))


def _policy() -> ProjectionPolicy:
    return ProjectionPolicy(0, "raw-path", ProjectionBudget(8, 8, 8, 4, 0, 2, 1, (("TRACKING_FACT", 8),), 100_000))


def _steward_registry(config) -> ToolRegistry:  # type: ignore[no-untyped-def]
    registry = ToolRegistry()
    register_steward_tools(registry, StewardRuntimeDependencies(ToolExecutionContext(config, SharedToolBudget()), _policy()))
    return registry


def test_historical_entry_reference_descriptor_requires_an_exact_scoped_relative_path(tmp_path: Path) -> None:
    config, _base, _target, _database_before, _evidence_before = _fixture(tmp_path)
    tool = next(tool for tool in _steward_registry(config).tools if tool.name == "steward_resolve_entry_reference")
    schema = tool.input_schema
    relative_path = schema["properties"]["relative_path"]
    description = f"{tool.description} {relative_path['description']}".lower()

    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"snapshot_id", "scope_id", "relative_path"}
    assert schema["required"] == ["snapshot_id", "scope_id", "relative_path"]
    assert relative_path["type"] == "string" and relative_path["minLength"] == 1
    for phrase in (
        "complete snapshot-scoped relative path",
        "exactly match an entry",
        "basename is not",
        "current filesystem path",
        "inspect or search the historical snapshot",
        "no basename, fuzzy, or scope-changing fallback",
        "not a current filesystem handle",
    ):
        assert phrase in description


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("steward_list_snapshots", {}),
        ("steward_inspect_snapshot", {"snapshot_id": "TARGET"}),
        ("steward_inspect_structure", {"snapshot_id": "TARGET", "depth": 1}),
        ("steward_compare_snapshots", {"left_snapshot_id": "BASE", "right_snapshot_id": "TARGET"}),
        ("steward_inspect_growth", {"base_snapshot_id": "BASE", "target_snapshot_id": "TARGET", "depth": 1}),
        ("steward_inspect_duplicates", {"snapshot_id": "TARGET"}),
        ("steward_inspect_relations", {"base_snapshot_id": "BASE", "target_snapshot_id": "TARGET"}),
        ("steward_project_snapshot", {"mode": "SNAPSHOT_DIAGNOSTIC", "snapshot_id": "TARGET", "depth": 1}),
        ("steward_resolve_entry_reference", {"snapshot_id": "TARGET", "scope_id": "managed", "relative_path": "a.txt"}),
    ],
)
def test_all_nine_steward_tools_run_through_runtime_without_mutating_fixtures(
    tmp_path: Path, tool_name: str, arguments: dict[str, Any]
) -> None:
    config, base, target, database_before, evidence_before = _fixture(tmp_path)
    substituted = {
        key: target.snapshot_id if value == "TARGET" else base.snapshot_id if value == "BASE" else value
        for key, value in arguments.items()
    }
    result = _run(
        AgentRuntime(_steward_registry(config)),
        (
            ModelTurnResult(tool_call=ModelToolCall("one", tool_name, substituted)),
            ModelTurnResult(final_answer=ModelFinalAnswer("historical result reviewed")),
        ),
    )
    assert result.final_answer and result.traces[0].source_family == SourceFamily.STEWARD_HISTORICAL
    from local_steward.database import database_path

    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before


def test_steward_projection_pair_and_shared_budget_are_preserved(tmp_path: Path) -> None:
    config, base, target, _database_before, _evidence_before = _fixture(tmp_path)
    registry = _steward_registry(config)
    result = _run(
        AgentRuntime(registry),
        (
            ModelTurnResult(
                tool_call=ModelToolCall(
                    "one",
                    "steward_project_snapshot",
                    {"mode": "PAIR_TRACKING", "base_snapshot_id": base.snapshot_id, "target_snapshot_id": target.snapshot_id},
                )
            ),
            ModelTurnResult(final_answer=ModelFinalAnswer("pair projection reviewed")),
        ),
    )
    assert result.final_answer and result.budget.usage.steward_tool_calls_used == 1
    assert result.budget.usage.steward_items_returned >= 0
