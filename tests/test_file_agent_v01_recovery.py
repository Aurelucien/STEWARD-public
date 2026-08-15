"""Offline V0.1 recovery coverage for serial native tool-call batches."""

import asyncio
import json
from typing import Any

import pytest

from local_steward.file_agent.runtime import (
    AgentRuntime,
    AgentTurnRequest,
    ModelFinalAnswer,
    ModelMessage,
    ModelMessageRole,
    ModelToolBatchResultMessage,
    ModelToolCall,
    ModelToolCallingError,
    ModelToolDescriptor,
    ModelToolResultDisposition,
    ModelToolResultMessage,
    ModelTurnResult,
    OpenAICompatibleToolCallingModel,
    RuntimeFailure,
    RuntimeTool,
    RuntimeToolResult,
    SourceFamily,
    ToolCallingProviderSettings,
    ToolRegistry,
)
from local_steward.file_agent.runtime.preflight import ScriptedFakeToolCallingModel


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


def _not_executed(blocked_by: str) -> dict[str, Any]:
    return {
        "status": "NOT_EXECUTED",
        "reason_code": "PRIOR_CALL_FAILED",
        "executed": False,
        "evidence": False,
        "blocked_by_tool_call_id": blocked_by,
    }


def _calls(*values: str) -> ModelTurnResult:
    return ModelTurnResult(
        tool_calls=tuple(ModelToolCall(f"call-{index}", "probe", {"value": value}) for index, value in enumerate(values, 1))
    )


def _registry(dispatched: list[str], *, source: SourceFamily = SourceFamily.STEWARD_HISTORICAL) -> ToolRegistry:
    def dispatch(arguments: dict[str, Any]) -> RuntimeToolResult:
        value = arguments["value"]
        dispatched.append(value)
        if value == "fail":
            code = "STEWARD_TOOL_FAILED" if source == SourceFamily.STEWARD_HISTORICAL else "FILESYSTEM_TOOL_FAILED"
            raise RuntimeFailure(code, "/internal/path is deliberately not safe output")
        if value == "invariant":
            raise RuntimeFailure("INTERNAL_INVARIANT_FAILED", "invariant failure")
        return RuntimeToolResult(source, {"observed": value})

    registry = ToolRegistry()
    registry.register(RuntimeTool("probe", "Offline probe.", _SCHEMA, source, dispatch))
    return registry


def _run(registry: ToolRegistry, responses: tuple[object, ...]):
    model = ScriptedFakeToolCallingModel(responses)
    result = asyncio.run(AgentRuntime(registry).run(AgentTurnRequest("offline V0.1 test"), model))
    return result, model


def _injected_batch(model: ScriptedFakeToolCallingModel) -> ModelToolBatchResultMessage:
    injected = model.requests[1][-1]
    assert isinstance(injected, ModelToolBatchResultMessage)
    return injected


def test_v01_r1_success_batch_keeps_ordered_success_results() -> None:
    dispatched: list[str] = []
    result, model = _run(
        _registry(dispatched),
        (_calls("one", "two"), ModelTurnResult(final_answer=ModelFinalAnswer("done"))),
    )

    assert result.final_answer == "done" and dispatched == ["one", "two"]
    injected = _injected_batch(model)
    assert [item.disposition for item in injected.results] == [
        ModelToolResultDisposition.SUCCESS,
        ModelToolResultDisposition.SUCCESS,
    ]
    assert [item.provider_call_id for item in injected.results] == ["call-1", "call-2"]


def test_v01_r2_single_recoverable_failure_reinjects_error_and_continues() -> None:
    dispatched: list[str] = []
    result, model = _run(
        _registry(dispatched),
        (_calls("fail"), ModelTurnResult(final_answer=ModelFinalAnswer("PARTIAL: lookup failed"))),
    )

    assert result.final_answer == "PARTIAL: lookup failed" and dispatched == ["fail"]
    injected = model.requests[1][-1]
    assert isinstance(injected, ModelToolResultMessage)
    assert injected.disposition == ModelToolResultDisposition.ERROR
    assert injected.result == {
        "fact_source": "STEWARD_HISTORICAL",
        "tool_name": "probe",
        "status": "ERROR",
        "error_code": "STEWARD_TOOL_FAILED",
    }
    assert "/internal/path" not in json.dumps(injected.result)
    assert result.budget.usage.total_tool_calls_used == 1


def test_v01_r3_second_failure_stops_tail_and_reinjects_complete_original_batch() -> None:
    dispatched: list[str] = []
    initial = _calls("one", "fail", "tail")
    result, model = _run(
        _registry(dispatched),
        (initial, ModelTurnResult(final_answer=ModelFinalAnswer("PARTIAL: historical lookup failed"))),
    )

    assert result.final_answer and dispatched == ["one", "fail"]
    assert [trace.arguments["value"] for trace in result.traces] == ["one", "fail"]
    assert result.budget.usage.total_tool_calls_used == 2
    injected = _injected_batch(model)
    assert injected.tool_calls == initial.tool_calls
    assert [item.provider_call_id for item in injected.results] == ["call-1", "call-2", "call-3"]
    assert [item.disposition for item in injected.results] == [
        ModelToolResultDisposition.SUCCESS,
        ModelToolResultDisposition.ERROR,
        ModelToolResultDisposition.NOT_EXECUTED,
    ]
    assert injected.results[2].result == _not_executed("call-2")
    assert all(trace.tool_name == "probe" for trace in result.traces)


def test_v01_r4_first_failure_stops_entire_remaining_batch() -> None:
    dispatched: list[str] = []
    result, model = _run(
        _registry(dispatched),
        (_calls("fail", "tail"), ModelTurnResult(final_answer=ModelFinalAnswer("UNAVAILABLE"))),
    )

    assert result.final_answer == "UNAVAILABLE" and dispatched == ["fail"]
    assert len(result.traces) == 1 and result.budget.usage.total_tool_calls_used == 1
    injected = _injected_batch(model)
    assert [item.disposition for item in injected.results] == [
        ModelToolResultDisposition.ERROR,
        ModelToolResultDisposition.NOT_EXECUTED,
    ]


def test_v01_r14_to_r17_model_may_reissue_tail_but_runtime_never_does() -> None:
    dispatched: list[str] = []
    result, model = _run(
        _registry(dispatched),
        (
            _calls("one", "fail", "tail"),
            _calls("tail"),
            ModelTurnResult(final_answer=ModelFinalAnswer("PARTIAL: recovered with later query")),
        ),
    )

    assert result.final_answer and dispatched == ["one", "fail", "tail"]
    assert len(model.requests) == 3
    first_reinjection = _injected_batch(model)
    assert first_reinjection.results[-1].disposition == ModelToolResultDisposition.NOT_EXECUTED
    assert result.budget.usage.total_tool_calls_used == 3


@pytest.mark.parametrize(
    ("source", "expected_code", "expected_fact_source"),
    [
        (SourceFamily.STEWARD_HISTORICAL, "STEWARD_TOOL_FAILED", "STEWARD_HISTORICAL"),
        (SourceFamily.FILESYSTEM_CURRENT, "FILESYSTEM_TOOL_FAILED", "FILESYSTEM_CURRENT"),
    ],
)
def test_v01_r18_to_r20_nonexecution_has_no_facts_and_preserves_source_attribution(
    source: SourceFamily, expected_code: str, expected_fact_source: str
) -> None:
    dispatched: list[str] = []
    _result, model = _run(
        _registry(dispatched, source=source),
        (_calls("fail", "tail"), ModelTurnResult(final_answer=ModelFinalAnswer("PARTIAL"))),
    )

    injected = _injected_batch(model)
    assert injected.results[0].result["fact_source"] == expected_fact_source
    assert injected.results[0].result["error_code"] == expected_code
    assert injected.results[1].result == _not_executed("call-1")
    assert "fact_source" not in injected.results[1].result
    assert dispatched == ["fail"]


def test_v01_r21_invariant_failure_is_not_reinjected() -> None:
    dispatched: list[str] = []
    result, model = _run(_registry(dispatched), (_calls("invariant", "tail"),))

    assert result.failure_code == "INTERNAL_INVARIANT_FAILED"
    assert dispatched == ["invariant"] and len(model.requests) == 1
    assert len(result.traces) == 1 and result.traces[0].failure_code == "INTERNAL_INVARIANT_FAILED"


def test_v01_r5_r6_r7_r8_r9_r22_r23_serializer_preserves_all_original_ids_offline() -> None:
    captured: list[dict[str, Any]] = []
    calls = tuple(ModelToolCall(f"call-{index}", "probe", {"value": str(index)}) for index in range(1, 5))
    batch = ModelToolBatchResultMessage(
        (
            ModelToolResultMessage(calls[0], {"status": "COMPLETE"}),
            ModelToolResultMessage(calls[1], {"status": "ERROR", "error_code": "STEWARD_TOOL_FAILED"}, disposition=ModelToolResultDisposition.ERROR),
            ModelToolResultMessage(calls[2], _not_executed("call-2"), disposition=ModelToolResultDisposition.NOT_EXECUTED),
            ModelToolResultMessage(calls[3], _not_executed("call-2"), disposition=ModelToolResultDisposition.NOT_EXECUTED),
        ),
        "Original preamble.",
        calls,
    )

    def transport(_url: str, _headers: dict[str, str], payload: bytes, _timeout: float, _maximum: int) -> bytes:
        captured.append(json.loads(payload))
        return b'{"model":"offline","choices":[{"finish_reason":"stop","message":{"content":"done"}}]}'

    adapter = OpenAICompatibleToolCallingModel(
        ToolCallingProviderSettings("https://provider.invalid", "test-key", "offline", allow_network=True), transport
    )
    descriptor = ModelToolDescriptor("probe", "Offline probe.", _SCHEMA)
    adapter.complete((ModelMessage(ModelMessageRole.USER, "x"), batch), (descriptor,))
    adapter.complete((ModelMessage(ModelMessageRole.USER, "x"), batch), (descriptor,))

    first_wire = captured[0]["messages"]
    assert [call["id"] for call in first_wire[1]["tool_calls"]] == ["call-1", "call-2", "call-3", "call-4"]
    assert [item["tool_call_id"] for item in first_wire[2:]] == ["call-1", "call-2", "call-3", "call-4"]
    payload = json.loads(first_wire[4]["content"])
    assert payload == _not_executed("call-2")
    assert captured[0]["messages"] == captured[1]["messages"]
    with pytest.raises(ModelToolCallingError, match="exceeds limit"):
        ModelTurnResult(tool_calls=calls)


def test_v01_call_id_mismatch_is_hard_dto_failure() -> None:
    first = ModelToolCall("call-1", "probe", {"value": "one"})
    second = ModelToolCall("call-2", "probe", {"value": "two"})
    with pytest.raises(ModelToolCallingError, match="call IDs"):
        ModelToolBatchResultMessage((ModelToolResultMessage(first, {"status": "COMPLETE"}),), tool_calls=(second,))
