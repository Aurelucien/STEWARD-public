"""Single-call, in-memory orchestration for the provider-neutral Context Layer."""

import json

from .adapter import build_context_packet, render_context_packet
from .canonical import canonical_request_constraints, request_constraints_digest
from .errors import LLMContextInvariantError, LLMModelCallError, LLMUnsupportedTaskDomainError
from ..observation_projection.models import ObservationProjection
from .models import (
    CONTEXT_PROTOCOL_VERSION,
    ContextBudget,
    LLMContextPacket,
    LLMModelRequest,
    LLMTaskDomain,
    ModelCallable,
    REQUEST_CONSTRAINTS_VERSION,
    RequestConstraints,
    ExplorationCapabilityClass,
    SandboxRunResult,
    UserIntentContext,
)
from .output_contract import output_contract_digest, render_output_contract_manifest
from .parser import parse_interpretation_result
from .validation import validate_context_packet, validate_interpretation_result


_INSTRUCTION_CONTRACT = (
    "Use the supplied Observation Projection Context Packet as the only fact source. "
    "Return exactly one JSON object. Separate OBSERVATION, INTERPRETATION, "
    "HYPOTHESIS, and EXPLORATION. Every OBSERVATION requires packet evidence. "
    "A hypothesis is not a fact. Every exploration requires a capability class. "
    "Do not claim filesystem operations, safe deletion, physical reclaimable space, "
    "or a long-term trend from one Snapshot or one Snapshot pair."
)

_SUPPORTED_DOMAINS = frozenset({LLMTaskDomain.STATIC_SNAPSHOT, LLMTaskDomain.STATIC_PAIR_COMPARISON})


def build_request_constraints(
    task_domain: LLMTaskDomain,
    evidence_tokens: tuple[str, ...],
    capability_classes: tuple[ExplorationCapabilityClass, ...],
    expansion_target_tokens: tuple[str, ...] = (),
) -> RequestConstraints:
    """Normalize the safe dynamic registry for one provider-neutral request."""
    if task_domain not in _SUPPORTED_DOMAINS:
        raise LLMUnsupportedTaskDomainError("LLM_TASK_DOMAIN_UNSUPPORTED")
    if any(not isinstance(token, str) for token in evidence_tokens + expansion_target_tokens):
        raise LLMContextInvariantError("REQUEST_CONSTRAINTS_REGISTRY_INVALID")
    if any(not isinstance(item, ExplorationCapabilityClass) for item in capability_classes):
        raise LLMContextInvariantError("REQUEST_CONSTRAINTS_CAPABILITY_INVALID")
    normalized_tokens = tuple(sorted(set(evidence_tokens)))
    normalized_capabilities = tuple(sorted(set(capability_classes), key=lambda item: item.value))
    normalized_expansion = tuple(sorted(set(expansion_target_tokens)))
    if any(token not in normalized_tokens for token in normalized_expansion):
        raise LLMContextInvariantError("REQUEST_CONSTRAINTS_EXPANSION_INVALID")
    if normalized_expansion and ExplorationCapabilityClass.CURRENT_PROJECTION_EXPANSION not in normalized_capabilities:
        raise LLMContextInvariantError("REQUEST_CONSTRAINTS_CAPABILITY_INVALID")
    return RequestConstraints(
        REQUEST_CONSTRAINTS_VERSION,
        task_domain,
        normalized_tokens,
        normalized_capabilities,
        normalized_expansion,
        tuple(sorted((item for item in ExplorationCapabilityClass if item not in normalized_capabilities), key=lambda item: item.value)),
    )


def _packet_request_constraints(packet: LLMContextPacket) -> RequestConstraints:
    expansion_tokens = tuple(
        item.token for item in packet.evidence_references if item.kind.value == "EXPANSION"
    )
    capabilities = (
        ExplorationCapabilityClass.NEW_OBSERVATION_REQUIRED,
        ExplorationCapabilityClass.OUT_OF_SCOPE,
    ) + (
        (ExplorationCapabilityClass.CURRENT_PROJECTION_EXPANSION,) if expansion_tokens else ()
    )
    return build_request_constraints(
        packet.task_domain,
        tuple(item.token for item in packet.evidence_references),
        capabilities,
        expansion_tokens,
    )


def render_request_constraints(value: RequestConstraints) -> str:
    return canonical_request_constraints(value).decode("utf-8")


def parse_request_constraints(raw: str) -> RequestConstraints:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise LLMContextInvariantError("REQUEST_CONSTRAINTS_CANONICAL_INVALID") from error
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "task_domain", "evidence_tokens", "capability_classes", "expansion_target_tokens",
        "excluded_capability_classes", "evidence_token_wire_type", "evidence_array_unique",
        "top_level_reference_rule", "token_order_rule", "empty_registry_rule",
    }:
        raise LLMContextInvariantError("REQUEST_CONSTRAINTS_CANONICAL_INVALID")
    try:
        schema_version = value["schema_version"]
        task_domain = LLMTaskDomain(value["task_domain"])
        tokens = value["evidence_tokens"]
        capabilities = value["capability_classes"]
        targets = value["expansion_target_tokens"]
        excluded_capabilities = value["excluded_capability_classes"]
        wire_type = value["evidence_token_wire_type"]
        array_unique = value["evidence_array_unique"]
        top_level_rule = value["top_level_reference_rule"]
        token_order_rule = value["token_order_rule"]
        empty_registry_rule = value["empty_registry_rule"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ValueError
        if not isinstance(tokens, list) or not all(isinstance(item, str) for item in tokens):
            raise ValueError
        if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
            raise ValueError
        if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
            raise ValueError
        if not isinstance(excluded_capabilities, list) or not all(isinstance(item, str) for item in excluded_capabilities):
            raise ValueError
        if not all(isinstance(item, str) for item in (wire_type, top_level_rule, token_order_rule, empty_registry_rule)):
            raise ValueError
        if not isinstance(array_unique, bool):
            raise ValueError
        constraints = RequestConstraints(
            schema_version,
            task_domain,
            tuple(tokens),
            tuple(ExplorationCapabilityClass(item) for item in capabilities),
            tuple(targets),
            tuple(ExplorationCapabilityClass(item) for item in excluded_capabilities),
            wire_type,
            array_unique,
            top_level_rule,
            token_order_rule,
            empty_registry_rule,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise LLMContextInvariantError("REQUEST_CONSTRAINTS_CANONICAL_INVALID") from error
    if render_request_constraints(constraints) != raw:
        raise LLMContextInvariantError("REQUEST_CONSTRAINTS_CANONICAL_INVALID")
    return constraints


def build_model_request_from_context(
    task_domain: LLMTaskDomain,
    context_json: str,
    constraints: RequestConstraints,
) -> LLMModelRequest:
    """Build one complete request without mixing static and dynamic contracts."""
    if constraints.task_domain != task_domain:
        raise LLMContextInvariantError("RESULT_TASK_DOMAIN_MISMATCH")
    request = LLMModelRequest(
        CONTEXT_PROTOCOL_VERSION,
        task_domain,
        _INSTRUCTION_CONTRACT,
        context_json,
        render_output_contract_manifest(),
        render_request_constraints(constraints),
        output_contract_digest(),
        request_constraints_digest(constraints),
    )
    validate_model_request_contract(request)
    return request


def validate_model_request_contract(request: LLMModelRequest) -> RequestConstraints:
    """Block model invocation unless static and dynamic request identities match."""
    if request.required_output_schema != render_output_contract_manifest():
        raise LLMContextInvariantError("OUTPUT_CONTRACT_MANIFEST_INVALID")
    if request.output_contract_digest != output_contract_digest():
        raise LLMContextInvariantError("OUTPUT_CONTRACT_DIGEST_INVALID")
    constraints = parse_request_constraints(request.request_constraints_json)
    normalized = build_request_constraints(
        constraints.task_domain,
        constraints.evidence_tokens,
        constraints.capability_classes,
        constraints.expansion_target_tokens,
    )
    if normalized != constraints:
        raise LLMContextInvariantError("REQUEST_CONSTRAINTS_CANONICAL_INVALID")
    if constraints.task_domain != request.task_domain:
        raise LLMContextInvariantError("RESULT_TASK_DOMAIN_MISMATCH")
    if request.request_constraints_digest != request_constraints_digest(constraints):
        raise LLMContextInvariantError("REQUEST_CONSTRAINTS_DIGEST_INVALID")
    return constraints


def build_model_request(packet: LLMContextPacket) -> LLMModelRequest:
    """Build pure provider-neutral request data without invoking a model."""
    if packet.task_domain not in _SUPPORTED_DOMAINS:
        raise LLMUnsupportedTaskDomainError("LLM_TASK_DOMAIN_UNSUPPORTED")
    violations = validate_context_packet(packet)
    if violations:
        raise LLMContextInvariantError(violations[0].code)
    return build_model_request_from_context(
        packet.task_domain,
        render_context_packet(packet),
        _packet_request_constraints(packet),
    )


def run_once(
    projection: ObservationProjection,
    user_intent: UserIntentContext,
    budget: ContextBudget,
    model: ModelCallable,
) -> SandboxRunResult:
    """Build one packet, call one injected model once, parse once, validate once."""
    packet = build_context_packet(projection, user_intent, budget)
    request = build_model_request(packet)
    try:
        validate_model_request_contract(request)
        raw_response = model(request)
    except Exception as error:
        raise LLMModelCallError("LLM_MODEL_CALL_FAILED") from error
    try:
        parsed = parse_interpretation_result(raw_response)
    except Exception as error:
        return SandboxRunResult(
            packet,
            request,
            raw_response,
            None,
            None,
            getattr(error, "code", "LLM_OUTPUT_PARSE_INVALID"),
            getattr(error, "failure_subtype", None),
        )
    validation = validate_interpretation_result(packet, parsed)
    return SandboxRunResult(packet, request, raw_response, parsed, validation, None)
