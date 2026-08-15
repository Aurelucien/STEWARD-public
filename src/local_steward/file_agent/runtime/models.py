"""Provider-neutral, turn-local native tool-calling protocol.

These types do not describe Snapshot, Projection, Evidence, Finding, Proposal,
or Action data.  They are only the narrow exchange between a future runtime and
an injected model port.
"""

from dataclasses import dataclass
from enum import Enum
import json
from typing import Any, Protocol, TypeAlias


class ModelToolCallingError(RuntimeError):
    """A safe, classified model-port protocol failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ModelMessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ModelToolResultDisposition(str, Enum):
    """Provider-neutral disposition of one provider-requested tool call."""

    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    NOT_EXECUTED = "NOT_EXECUTED"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "duplicate JSON object key")
        result[key] = value
    return result


def strict_json_object(value: str) -> dict[str, Any]:
    """Parse exactly one JSON object and reject duplicate object keys."""
    try:
        decoded = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, ModelToolCallingError):
            raise
        raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool arguments are not strict JSON") from error
    if not isinstance(decoded, dict):
        raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool arguments must be a JSON object")
    return decoded


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """A non-tool message sent to a provider."""

    role: ModelMessageRole
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "model message content must be text")


@dataclass(frozen=True, slots=True)
class ModelToolDescriptor:
    """One runtime-registered provider-visible function descriptor."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool name is invalid")
        if not isinstance(self.description, str) or not isinstance(self.input_schema, dict):
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool descriptor is invalid")


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    """Exactly one native provider function call."""

    provider_call_id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.provider_call_id, str) or not self.provider_call_id:
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool call ID is missing")
        if not isinstance(self.name, str) or not self.name:
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool call name is missing")
        if not isinstance(self.arguments, dict):
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool arguments must be an object")


@dataclass(frozen=True, slots=True)
class ModelFinalAnswer:
    """A final text response, mutually exclusive with a tool call."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "final answer must be non-empty text")


@dataclass(frozen=True, slots=True)
class ModelTurnResult:
    """One provider response: a final answer or an ordered native tool batch."""

    final_answer: ModelFinalAnswer | None = None
    tool_call: ModelToolCall | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()
    assistant_preamble: str | None = None

    def __post_init__(self) -> None:
        calls = self.tool_calls or ((self.tool_call,) if self.tool_call is not None else ())
        if self.tool_call is not None and self.tool_calls:
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool call fields are contradictory")
        if (self.final_answer is None) == (not bool(calls)):
            raise ModelToolCallingError(
                "MODEL_TOOL_CALL_INVALID", "model response must contain exactly one final answer or tool batch"
            )
        if len(calls) > 3:
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool-call batch exceeds limit")
        if len({call.provider_call_id for call in calls}) != len(calls):
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "duplicate tool call ID")
        if self.assistant_preamble is not None:
            if not isinstance(self.assistant_preamble, str) or not self.assistant_preamble.strip():
                raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "assistant preamble must be non-empty text")
            if not calls:
                raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "final answer cannot contain an assistant preamble")
        object.__setattr__(self, "tool_calls", calls)
        object.__setattr__(self, "tool_call", calls[0] if len(calls) == 1 else None)


@dataclass(frozen=True, slots=True)
class ModelToolResultMessage:
    """A tagged, untrusted tool result returned to the model on a later call."""

    tool_call: ModelToolCall
    result: dict[str, Any]
    assistant_preamble: str | None = None
    disposition: ModelToolResultDisposition = ModelToolResultDisposition.SUCCESS

    def __post_init__(self) -> None:
        if not isinstance(self.result, dict):
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool result must be an object")
        if self.assistant_preamble is not None and (
            not isinstance(self.assistant_preamble, str) or not self.assistant_preamble.strip()
        ):
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "assistant preamble must be non-empty text")
        if not isinstance(self.disposition, ModelToolResultDisposition):
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool result disposition is invalid")
        if self.disposition == ModelToolResultDisposition.NOT_EXECUTED:
            expected = {
                "status": "NOT_EXECUTED",
                "reason_code": "PRIOR_CALL_FAILED",
                "executed": False,
                "evidence": False,
            }
            if any(self.result.get(key) != value for key, value in expected.items()):
                raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "not-executed result is invalid")

    @property
    def provider_call_id(self) -> str:
        return self.tool_call.provider_call_id

    @property
    def tool_name(self) -> str:
        return self.tool_call.name


@dataclass(frozen=True, slots=True)
class ModelToolBatchResultMessage:
    """One assistant native batch followed by matching tagged tool results."""

    results: tuple[ModelToolResultMessage, ...]
    assistant_preamble: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()

    def __post_init__(self) -> None:
        if not self.results:
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool-result batch is invalid")
        if self.assistant_preamble is not None and (
            not isinstance(self.assistant_preamble, str) or not self.assistant_preamble.strip()
        ):
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "assistant preamble must be non-empty text")
        calls = self.tool_calls or tuple(result.tool_call for result in self.results)
        result_ids = tuple(result.provider_call_id for result in self.results)
        call_ids = tuple(call.provider_call_id for call in calls)
        if len(calls) != len(self.results) or call_ids != result_ids:
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool result call IDs do not match assistant batch")
        if len(set(call_ids)) != len(call_ids):
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "duplicate tool result call ID")
        object.__setattr__(self, "tool_calls", calls)


ModelConversationItem: TypeAlias = ModelMessage | ModelToolResultMessage | ModelToolBatchResultMessage


class ToolCallingModel(Protocol):
    """Injected port; a future loop owns call limits and sequencing."""

    def complete(
        self,
        messages: tuple[ModelConversationItem, ...],
        tools: tuple[ModelToolDescriptor, ...],
        *,
        tool_choice: str = "auto",
    ) -> ModelTurnResult: ...
