"""Strict parsing for one provider-neutral LLM JSON response object."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import NoReturn

from .errors import LLMOutputParseError
from .output_contract import OutputContractStructuralError, validate_output_contract_structure
from .models import (
    CONTEXT_PROTOCOL_VERSION,
    EvidenceReferenceUse,
    ExplorationCapabilityClass,
    ExplorationItem,
    HypothesisItem,
    InterpretationItem,
    InterpretationStatus,
    LLMInterpretationResult,
    LLMTaskDomain,
    ObservationItem,
    SemanticClass,
)


JsonObject = dict[str, object]


def _invalid() -> NoReturn:
    raise LLMOutputParseError("LLM_OUTPUT_PARSE_INVALID")


def _object_pairs(pairs: Iterable[tuple[str, object]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        _invalid()
    return value


def _array(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        _invalid()
    return tuple(value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        _invalid()
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid()
    return value


def _fields(value: JsonObject, expected: frozenset[str]) -> None:
    if set(value) != expected:
        _invalid()


def _enum(enum_type, value: object):  # type: ignore[no-untyped-def]
    try:
        return enum_type(_string(value))
    except ValueError as error:
        raise LLMOutputParseError("LLM_OUTPUT_PARSE_INVALID") from error


def _uses(value: object) -> tuple[EvidenceReferenceUse, ...]:
    tokens = tuple(EvidenceReferenceUse(_string(item)) for item in _array(value))
    if len({item.token for item in tokens}) != len(tokens):
        _invalid()
    return tokens


def _strings(value: object) -> tuple[str, ...]:
    return tuple(_string(item) for item in _array(value))


def _observation(value: object) -> ObservationItem:
    item = _object(value)
    _fields(item, frozenset({"semantic_class", "statement", "evidence_references"}))
    if _enum(SemanticClass, item["semantic_class"]) != SemanticClass.OBSERVATION:
        _invalid()
    return ObservationItem(SemanticClass.OBSERVATION, _string(item["statement"]), _uses(item["evidence_references"]))


def _interpretation(value: object) -> InterpretationItem:
    item = _object(value)
    _fields(item, frozenset({"semantic_class", "statement", "supporting_evidence_references", "qualifications"}))
    if _enum(SemanticClass, item["semantic_class"]) != SemanticClass.INTERPRETATION:
        _invalid()
    return InterpretationItem(
        SemanticClass.INTERPRETATION,
        _string(item["statement"]),
        _uses(item["supporting_evidence_references"]),
        _strings(item["qualifications"]),
    )


def _hypothesis(value: object) -> HypothesisItem:
    item = _object(value)
    _fields(
        item,
        frozenset(
            {
                "semantic_class", "statement", "supporting_evidence_references", "missing_information",
                "competing_explanation", "discriminating_observation",
            }
        ),
    )
    if _enum(SemanticClass, item["semantic_class"]) != SemanticClass.HYPOTHESIS:
        _invalid()
    return HypothesisItem(
        SemanticClass.HYPOTHESIS,
        _string(item["statement"]),
        _uses(item["supporting_evidence_references"]),
        _strings(item["missing_information"]),
        _optional_string(item["competing_explanation"]),
        _optional_string(item["discriminating_observation"]),
    )


def _exploration(value: object) -> ExplorationItem:
    item = _object(value)
    _fields(
        item,
        frozenset(
            {
                "semantic_class", "question", "target", "supporting_evidence_references",
                "missing_information", "expected_value", "capability_class",
            }
        ),
    )
    if _enum(SemanticClass, item["semantic_class"]) != SemanticClass.EXPLORATION:
        _invalid()
    return ExplorationItem(
        SemanticClass.EXPLORATION,
        _string(item["question"]),
        _string(item["target"]),
        _uses(item["supporting_evidence_references"]),
        _strings(item["missing_information"]),
        _string(item["expected_value"]),
        _enum(ExplorationCapabilityClass, item["capability_class"]),
    )


def parse_interpretation_result(raw: str) -> LLMInterpretationResult:
    """Accept exactly one UTF-8 JSON object in the frozen provider-neutral shape."""
    try:
        validate_output_contract_structure(raw)
    except OutputContractStructuralError as error:
        raise LLMOutputParseError("LLM_OUTPUT_PARSE_INVALID", failure_subtype=error.failure_subtype) from error
    if not isinstance(raw, str):
        _invalid()
    try:
        decoded: object = json.loads(raw, object_pairs_hook=_object_pairs)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise LLMOutputParseError("LLM_OUTPUT_PARSE_INVALID") from error
    value = _object(decoded)
    _fields(
        value,
        frozenset(
            {
                "protocol_version", "task_domain", "status", "summary", "observations", "interpretations",
                "hypotheses", "explorations", "unknowns", "limitations", "evidence_references",
            }
        ),
    )
    if _integer(value["protocol_version"]) != CONTEXT_PROTOCOL_VERSION:
        _invalid()
    return LLMInterpretationResult(
        CONTEXT_PROTOCOL_VERSION,
        _enum(LLMTaskDomain, value["task_domain"]),
        _enum(InterpretationStatus, value["status"]),
        _string(value["summary"]),
        tuple(_observation(item) for item in _array(value["observations"])),
        tuple(_interpretation(item) for item in _array(value["interpretations"])),
        tuple(_hypothesis(item) for item in _array(value["hypotheses"])),
        tuple(_exploration(item) for item in _array(value["explorations"])),
        _strings(value["unknowns"]),
        _strings(value["limitations"]),
        _uses(value["evidence_references"]),
    )
