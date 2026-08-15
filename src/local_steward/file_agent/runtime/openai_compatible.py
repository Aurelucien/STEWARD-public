"""Replaceable OpenAI-compatible native tool-calling adapter.

This module is intentionally separate from the historical LLM Context sandbox.
It uses only the standard-library HTTP transport, has no provider-specific name
in the provider-neutral protocol, and never stores prompts, tool results, or
credentials.
"""

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from .models import (
    ModelConversationItem,
    ModelFinalAnswer,
    ModelMessage,
    ModelToolCall,
    ModelToolBatchResultMessage,
    ModelToolCallingError,
    ModelToolDescriptor,
    ModelToolResultMessage,
    ModelTurnResult,
    strict_json_object,
)


@dataclass(frozen=True, slots=True)
class _TransportResponse:
    body: bytes
    http_status: int | None
    content_type: str | None


ToolCallingProviderTransport = Callable[
    [str, dict[str, str], bytes, float, int], bytes | _TransportResponse
]
ProviderResponseObserver = Callable[[dict[str, Any]], None]


_AUDIT_SECRET_MARKERS = (
    "authorization",
    "bearer ",
    "api_key",
    "api-key",
    "steward_file_agent_provider_api_key",
)
_ABSOLUTE_PATH_TEXT = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^\s\"'\\]+/)*[^\s\"'\\]+")


def _safe_audit_text(value: str) -> str | None:
    """Return a record-safe provider string, never a credential-bearing value."""
    if any(marker in value.casefold() for marker in _AUDIT_SECRET_MARKERS):
        return None
    return _ABSOLUTE_PATH_TEXT.sub("[ABSOLUTE_PATH]", value)


def _audit_sha256(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None


def _content_audit(value: object, present: bool) -> dict[str, Any]:
    if not present:
        return {"content_state": "absent", "content_length": 0, "content_sha256": None, "content_text": None}
    if value is None:
        return {"content_state": "null", "content_length": 0, "content_sha256": None, "content_text": None}
    if not isinstance(value, str):
        return {"content_state": "nonempty", "content_length": None, "content_sha256": None, "content_text": None}
    safe = _safe_audit_text(value)
    return {
        "content_state": "empty" if value == "" else "nonempty",
        "content_length": len(value),
        "content_sha256": _audit_sha256(safe),
        "content_text": safe,
    }


def _tool_call_audit(raw_call: object, ordinal: int, known_tools: dict[str, ModelToolDescriptor]) -> dict[str, Any]:
    raw = raw_call if isinstance(raw_call, dict) else {}
    call_id = raw.get("id")
    function = raw.get("function")
    function_map = function if isinstance(function, dict) else {}
    name = function_map.get("name")
    arguments = function_map.get("arguments")
    safe_name = _safe_audit_text(name) if isinstance(name, str) else None
    safe_arguments = _safe_audit_text(arguments) if isinstance(arguments, str) else None
    parsed_arguments: dict[str, Any] | None = None
    strict_json_parse_ok = False
    json_object_ok = False
    duplicate_json_key_detected = False
    if isinstance(safe_arguments, str):
        try:
            parsed_arguments = strict_json_object(safe_arguments)
            strict_json_parse_ok = True
            json_object_ok = True
        except ModelToolCallingError as error:
            duplicate_json_key_detected = "duplicate JSON" in str(error)
    schema_validation_ok: bool | None = None
    if isinstance(name, str) and name in known_tools and parsed_arguments is not None:
        try:
            Draft202012Validator(known_tools[name].input_schema).validate(parsed_arguments)
            schema_validation_ok = True
        except (SchemaError, ValidationError):
            schema_validation_ok = False
    elif isinstance(name, str):
        schema_validation_ok = False
    return {
        "ordinal": ordinal,
        "call_id_present": isinstance(call_id, str) and bool(call_id),
        "call_id_normalized": _audit_sha256(_safe_audit_text(call_id)) if isinstance(call_id, str) else None,
        "tool_type": raw.get("type") if isinstance(raw.get("type"), str) else None,
        "tool_name": safe_name,
        "arguments_present": isinstance(arguments, str),
        "arguments_length": len(arguments) if isinstance(arguments, str) else None,
        "arguments_sha256": _audit_sha256(safe_arguments),
        "safe_arguments": parsed_arguments,
        "strict_json_parse_ok": strict_json_parse_ok,
        "json_object_ok": json_object_ok,
        "duplicate_json_key_detected": duplicate_json_key_detected,
        "schema_validation_ok": schema_validation_ok,
        "scenario_allowlist_ok": None,
        "duplicate_call_id": False,
        "duplicate_name_and_arguments": False,
        "budget_admission_ok": None,
        "scope_validation_status": None,
        "required_fact_relevance": None,
    }


def _response_audit(envelope: dict[str, Any], known_tools: dict[str, ModelToolDescriptor]) -> dict[str, Any]:
    """Produce a bounded structural record before adapter semantic acceptance."""
    choices = envelope.get("choices")
    choice_count = len(choices) if isinstance(choices, list) else None
    selected = choices[0] if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict) else None
    message = selected.get("message") if isinstance(selected, dict) and isinstance(selected.get("message"), dict) else None
    raw_tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    tool_calls = raw_tool_calls if isinstance(raw_tool_calls, list) else []
    calls = [_tool_call_audit(item, ordinal, known_tools) for ordinal, item in enumerate(tool_calls)]
    call_ids: set[str] = set()
    signatures: set[tuple[str | None, str | None]] = set()
    for call in calls:
        call_id = call["call_id_normalized"]
        if call_id is not None:
            call["duplicate_call_id"] = call_id in call_ids
            call_ids.add(call_id)
        signature = (call["tool_name"], call["arguments_sha256"])
        if signature[0] is not None and signature[1] is not None:
            call["duplicate_name_and_arguments"] = signature in signatures
            signatures.add(signature)
    return {
        "response_received": True,
        "returned_model": envelope.get("model") if isinstance(envelope.get("model"), str) else None,
        "choice_count": choice_count,
        "selected_choice_index": 0 if selected is not None else None,
        "finish_reason_present": isinstance(selected, dict) and "finish_reason" in selected,
        "finish_reason_value": selected.get("finish_reason") if isinstance(selected, dict) and isinstance(selected.get("finish_reason"), str) else None,
        **(_content_audit(message.get("content"), "content" in message) if isinstance(message, dict) else _content_audit(None, False)),
        "tool_calls_field_present": isinstance(message, dict) and "tool_calls" in message,
        "tool_call_count": len(tool_calls) if isinstance(raw_tool_calls, list) else None,
        "tool_calls": calls,
        "parser_rejection_stage": None,
        "parser_rejection_code": None,
    }


@dataclass(frozen=True, slots=True)
class ToolCallingTransportMetadata:
    """Safe per-call transport facts; no credentials, prompt, or response body."""

    requested_model: str
    returned_model: str | None
    finish_reason: str
    http_status: int | None
    response_content_type: str | None
    response_byte_length: int


@dataclass(frozen=True, slots=True)
class ToolCallingProviderSettings:
    """Explicitly network-gated OpenAI-compatible connection settings."""

    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = 60.0
    max_tokens: int = 1024
    temperature: float = 0
    max_response_bytes: int = 1_000_000
    allow_network: bool = False

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.base_url, self.api_key, self.model)):
            raise ModelToolCallingError("MODEL_CALL_FAILED", "provider configuration is invalid")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ModelToolCallingError("MODEL_CALL_FAILED", "provider timeout is invalid")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int) or self.max_tokens <= 0:
            raise ModelToolCallingError("MODEL_CALL_FAILED", "provider token limit is invalid")
        if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)):
            raise ModelToolCallingError("MODEL_CALL_FAILED", "provider temperature is invalid")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes <= 0
        ):
            raise ModelToolCallingError("MODEL_CALL_FAILED", "provider response limit is invalid")

    @classmethod
    def from_environment(cls, *, allow_network: bool) -> "ToolCallingProviderSettings":
        """Read only the explicit File Agent canary variables after opt-in."""
        if not allow_network:
            raise ModelToolCallingError("MODEL_CALL_FAILED", "network use requires explicit opt-in")
        values = {
            "base_url": os.environ.get("STEWARD_FILE_AGENT_PROVIDER_BASE_URL"),
            "api_key": os.environ.get("STEWARD_FILE_AGENT_PROVIDER_API_KEY"),
            "model": os.environ.get("STEWARD_FILE_AGENT_PROVIDER_MODEL"),
        }
        if not all(values.values()):
            raise ModelToolCallingError("MODEL_CALL_FAILED", "provider environment is incomplete")
        raw_timeout = os.environ.get("STEWARD_FILE_AGENT_PROVIDER_TIMEOUT_SECONDS", "60")
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise ModelToolCallingError("MODEL_CALL_FAILED", "provider timeout is invalid") from error
        return cls(
            base_url=str(values["base_url"]),
            api_key=str(values["api_key"]),
            model=str(values["model"]),
            timeout_seconds=timeout,
            allow_network=True,
        )


def _default_transport(
    url: str, headers: dict[str, str], payload: bytes, timeout: float, max_response_bytes: int
) -> _TransportResponse:
    request = Request(url, data=payload, headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - caller must explicitly opt in
        return _TransportResponse(
            response.read(max_response_bytes + 1),
            response.status,
            response.headers.get_content_type(),
        )


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    suffix = "/chat/completions"
    return normalized if normalized.endswith(suffix) else normalized + suffix


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _strict_response_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ModelToolCallingError("MODEL_CALL_FAILED", "provider response is not strict JSON") from error
    if not isinstance(value, dict):
        raise ModelToolCallingError("MODEL_CALL_FAILED", "provider response must be an object")
    return value


def _wire_message(item: ModelConversationItem) -> list[dict[str, Any]]:
    if isinstance(item, ModelMessage):
        return [{"role": item.role.value, "content": item.content}]
    if isinstance(item, ModelToolResultMessage):
        call = item.tool_call
        arguments = json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        # OpenAI-compatible APIs need both the assistant's prior native call and
        # the matching tagged tool result in the following request.
        return [
            {
                "role": "assistant",
                "content": item.assistant_preamble,
                "tool_calls": [
                    {
                        "id": call.provider_call_id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": arguments},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call.provider_call_id,
                "content": json.dumps(item.result, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            },
        ]
    if isinstance(item, ModelToolBatchResultMessage):
        calls = item.tool_calls
        assistant = {
            "role": "assistant", "content": item.assistant_preamble,
            "tool_calls": [
                {"id": call.provider_call_id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}}
                for call in calls
            ],
        }
        tools = [
            {"role": "tool", "tool_call_id": result.provider_call_id, "content": json.dumps(result.result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}
            for result in item.results
        ]
        return [assistant, *tools]
    raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "unsupported model conversation item")


def _wire_tool(descriptor: ModelToolDescriptor) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": descriptor.name,
            "description": descriptor.description,
            "parameters": descriptor.input_schema,
        },
    }


@dataclass(slots=True)
class OpenAICompatibleToolCallingModel:
    """One native call per invocation; sequencing belongs to the future runtime."""

    settings: ToolCallingProviderSettings
    transport: ToolCallingProviderTransport = _default_transport
    response_observer: ProviderResponseObserver | None = field(default=None, repr=False)
    last_metadata: ToolCallingTransportMetadata | None = field(init=False, default=None)
    last_response_observation: dict[str, Any] | None = field(init=False, default=None)

    def complete(
        self,
        messages: tuple[ModelConversationItem, ...],
        tools: tuple[ModelToolDescriptor, ...],
        *,
        tool_choice: str = "auto",
    ) -> ModelTurnResult:
        self.last_metadata = None
        self.last_response_observation = None
        if not self.settings.allow_network:
            raise ModelToolCallingError("MODEL_CALL_FAILED", "network use requires explicit opt-in")
        if tool_choice not in {"auto", "none", "required"}:
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool choice is unsupported")
        known_tools = {item.name for item in tools}
        if len(known_tools) != len(tools):
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool descriptors must have unique names")
        try:
            wire_messages = [part for item in messages for part in _wire_message(item)]
            payload = json.dumps(
                {
                    "model": self.settings.model,
                    "messages": wire_messages,
                    "tools": [_wire_tool(item) for item in tools],
                    "tool_choice": tool_choice,
                    "stream": False,
                    "max_tokens": self.settings.max_tokens,
                    "temperature": self.settings.temperature,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "provider request is not serializable") from error
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            received = self.transport(
                _chat_completions_url(self.settings.base_url),
                headers,
                payload,
                float(self.settings.timeout_seconds),
                self.settings.max_response_bytes,
            )
        except HTTPError as error:
            raise ModelToolCallingError("MODEL_CALL_FAILED", f"provider returned HTTP status {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise ModelToolCallingError("MODEL_CALL_FAILED", "provider transport failed") from error
        if isinstance(received, bytes):
            response = _TransportResponse(received, None, None)
        elif isinstance(received, _TransportResponse):
            response = received
        else:
            raise ModelToolCallingError("MODEL_CALL_FAILED", "provider transport returned an invalid response")
        if response.http_status is not None and not 200 <= response.http_status < 300:
            raise ModelToolCallingError("MODEL_CALL_FAILED", "provider returned a non-success status")
        if len(response.body) > self.settings.max_response_bytes:
            raise ModelToolCallingError("MODEL_CALL_FAILED", "provider response exceeds the byte limit")

        observation: dict[str, Any] | None = None
        try:
            envelope = _strict_response_object(response.body)
            observation = _response_audit(envelope, {item.name: item for item in tools})
            self.last_response_observation = observation
            choices = envelope.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise ModelToolCallingError("MODEL_CALL_FAILED", "provider response must contain exactly one choice")
            choice = choices[0]
            message = choice.get("message")
            finish_reason = choice.get("finish_reason")
            if not isinstance(message, dict) or not isinstance(finish_reason, str):
                raise ModelToolCallingError("MODEL_CALL_FAILED", "provider response choice is invalid")
            returned_model = envelope.get("model")
            if returned_model is not None and not isinstance(returned_model, str):
                raise ModelToolCallingError("MODEL_CALL_FAILED", "provider returned model is invalid")

            content = message.get("content")
            tool_calls = message.get("tool_calls")
            if tool_calls is None:
                tool_calls = []
            if not isinstance(tool_calls, list):
                raise ModelToolCallingError("MODEL_CALL_FAILED", "provider tool calls are invalid")
            if len(tool_calls) > 3:
                raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool-call batch exceeds limit")
            if "function_call" in message:
                raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "legacy function calls are not supported")

            if tool_calls:
                if finish_reason != "tool_calls":
                    raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool call finish reason is contradictory")
                if content is not None and not isinstance(content, str):
                    raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool call response content is invalid")
                preamble = content if isinstance(content, str) and content.strip() else None
                if preamble is not None and _safe_audit_text(preamble) is None:
                    raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "tool call preamble is unsafe")
                calls: list[ModelToolCall] = []
                for raw_call in tool_calls:
                    if not isinstance(raw_call, dict) or raw_call.get("type") != "function":
                        raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "provider tool call is invalid")
                    call_id = raw_call.get("id")
                    function = raw_call.get("function")
                    if not isinstance(call_id, str) or not call_id or not isinstance(function, dict):
                        raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "provider tool call is incomplete")
                    name = function.get("name")
                    arguments = function.get("arguments")
                    if not isinstance(name, str) or not name or not isinstance(arguments, str):
                        raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "provider tool call is incomplete")
                    if name not in known_tools:
                        raise ModelToolCallingError("TOOL_NOT_ALLOWED", "provider requested an unregistered tool")
                    calls.append(ModelToolCall(call_id, name, strict_json_object(arguments)))
                result = ModelTurnResult(
                    tool_calls=tuple(calls),
                    assistant_preamble=preamble,
                )
            else:
                if finish_reason != "stop":
                    raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "final response finish reason is unsupported")
                if not isinstance(content, str) or not content.strip():
                    raise ModelToolCallingError("MODEL_TOOL_CALL_INVALID", "provider final response is missing")
                result = ModelTurnResult(final_answer=ModelFinalAnswer(content))
        except ModelToolCallingError as error:
            if observation is not None:
                observation["parser_rejection_stage"] = "strict_adapter_semantics"
                observation["parser_rejection_code"] = error.code
                self._notify_response_observer(observation)
            raise

        self.last_metadata = ToolCallingTransportMetadata(
            self.settings.model,
            returned_model,
            finish_reason,
            response.http_status,
            response.content_type,
            len(response.body),
        )
        if observation is not None:
            self._notify_response_observer(observation)
        return result

    def _notify_response_observer(self, observation: dict[str, Any]) -> None:
        """Observers receive only the bounded, redacted structural record."""
        if self.response_observer is None:
            return
        try:
            self.response_observer(json.loads(json.dumps(observation, ensure_ascii=False, allow_nan=False)))
        except Exception:
            # Observability cannot alter provider acceptance or failure semantics.
            return
