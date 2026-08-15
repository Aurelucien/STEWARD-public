"""Canonical static wire contract for ``LLMInterpretationResult``.

This module deliberately describes only the frozen JSON grammar. Packet-local
reference membership and semantic validation remain in ``validation.py``.
"""

from __future__ import annotations

import json
from dataclasses import MISSING, dataclass, fields
from enum import Enum
from hashlib import sha256
from types import UnionType
from typing import Any, NoReturn, get_args, get_origin, get_type_hints

from ..evidence import canonical_json
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


OUTPUT_CONTRACT_ID = "local_steward.llm_interpretation_result"
OUTPUT_CONTRACT_VERSION = 1
OUTPUT_CONTRACT_DIGEST_DOMAIN = "local_steward.llm_output_contract.v1"


class OutputContractStructuralError(ValueError):
    """Raised when a response violates the static output wire grammar."""

    def __init__(self, failure_subtype: str) -> None:
        self.failure_subtype = failure_subtype
        super().__init__("LLM_OUTPUT_CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class WireObject:
    """One exact JSON object and the typed dataclass it represents."""

    name: str
    model_type: type[Any]
    fields: tuple["WireField", ...]
    additional_fields_forbidden: bool = True


@dataclass(frozen=True, slots=True)
class WireField:
    """A narrow, immutable field descriptor for the frozen result grammar."""

    name: str
    wire_type: str
    nullable: bool = False
    literal: str | int | None = None
    enum_type: type[Enum] | None = None
    object_schema: WireObject | None = None
    array_item_wire_type: str | None = None
    array_item_object: WireObject | None = None
    array_item_model: type[Any] | None = None
    unique_items: bool = False
    allows_empty: bool = True


@dataclass(frozen=True, slots=True)
class OutputContractDescriptor:
    """The static, provider-neutral contract; not a second result model."""

    contract_id: str
    contract_version: int
    root: WireObject
    duplicate_object_keys_forbidden: bool
    enum_types: tuple[type[Enum], ...]


def _field(
    name: str,
    wire_type: str,
    *,
    nullable: bool = False,
    literal: str | int | None = None,
    enum_type: type[Enum] | None = None,
    object_schema: WireObject | None = None,
    array_item_wire_type: str | None = None,
    array_item_object: WireObject | None = None,
    array_item_model: type[Any] | None = None,
    unique_items: bool = False,
    allows_empty: bool = True,
) -> WireField:
    return WireField(
        name,
        wire_type,
        nullable,
        literal,
        enum_type,
        object_schema,
        array_item_wire_type,
        array_item_object,
        array_item_model,
        unique_items,
        allows_empty,
    )


_OBSERVATION = WireObject(
    "ObservationItem",
    ObservationItem,
    (
        _field("semantic_class", "string", literal=SemanticClass.OBSERVATION.value, enum_type=SemanticClass),
        _field("statement", "string"),
        _field(
            "evidence_references",
            "array",
            array_item_wire_type="string",
            array_item_model=EvidenceReferenceUse,
            unique_items=True,
        ),
    ),
)
_INTERPRETATION = WireObject(
    "InterpretationItem",
    InterpretationItem,
    (
        _field("semantic_class", "string", literal=SemanticClass.INTERPRETATION.value, enum_type=SemanticClass),
        _field("statement", "string"),
        _field(
            "supporting_evidence_references",
            "array",
            array_item_wire_type="string",
            array_item_model=EvidenceReferenceUse,
            unique_items=True,
        ),
        _field("qualifications", "array", array_item_wire_type="string"),
    ),
)
_HYPOTHESIS = WireObject(
    "HypothesisItem",
    HypothesisItem,
    (
        _field("semantic_class", "string", literal=SemanticClass.HYPOTHESIS.value, enum_type=SemanticClass),
        _field("statement", "string"),
        _field(
            "supporting_evidence_references",
            "array",
            array_item_wire_type="string",
            array_item_model=EvidenceReferenceUse,
            unique_items=True,
        ),
        _field("missing_information", "array", array_item_wire_type="string"),
        _field("competing_explanation", "string", nullable=True),
        _field("discriminating_observation", "string", nullable=True),
    ),
)
_EXPLORATION = WireObject(
    "ExplorationItem",
    ExplorationItem,
    (
        _field("semantic_class", "string", literal=SemanticClass.EXPLORATION.value, enum_type=SemanticClass),
        _field("question", "string"),
        _field("target", "string"),
        _field(
            "supporting_evidence_references",
            "array",
            array_item_wire_type="string",
            array_item_model=EvidenceReferenceUse,
            unique_items=True,
        ),
        _field("missing_information", "array", array_item_wire_type="string"),
        _field("expected_value", "string"),
        _field("capability_class", "string", enum_type=ExplorationCapabilityClass),
    ),
)
_ROOT = WireObject(
    "LLMInterpretationResult",
    LLMInterpretationResult,
    (
        _field("protocol_version", "integer", literal=CONTEXT_PROTOCOL_VERSION),
        _field("task_domain", "string", enum_type=LLMTaskDomain),
        _field("status", "string", enum_type=InterpretationStatus),
        _field("summary", "string"),
        _field("observations", "array", array_item_object=_OBSERVATION),
        _field("interpretations", "array", array_item_object=_INTERPRETATION),
        _field("hypotheses", "array", array_item_object=_HYPOTHESIS),
        _field("explorations", "array", array_item_object=_EXPLORATION),
        _field("unknowns", "array", array_item_wire_type="string"),
        _field("limitations", "array", array_item_wire_type="string"),
        _field(
            "evidence_references",
            "array",
            array_item_wire_type="string",
            array_item_model=EvidenceReferenceUse,
            unique_items=True,
        ),
    ),
)

CANONICAL_OUTPUT_CONTRACT = OutputContractDescriptor(
    OUTPUT_CONTRACT_ID,
    OUTPUT_CONTRACT_VERSION,
    _ROOT,
    True,
    (LLMTaskDomain, InterpretationStatus, SemanticClass, ExplorationCapabilityClass),
)


def _enum_values(enum_type: type[Enum]) -> list[str]:
    return [str(item.value) for item in enum_type]


def _field_manifest(field: WireField) -> dict[str, object]:
    value: dict[str, object] = {
        "name": field.name,
        "nullable": field.nullable,
        "wire_type": field.wire_type,
    }
    if field.wire_type in {"array", "string"}:
        value["allows_empty"] = field.allows_empty
    if field.literal is not None:
        value["literal"] = field.literal
    if field.enum_type is not None:
        value["enum"] = _enum_values(field.enum_type)
    if field.object_schema is not None:
        value["object"] = _object_manifest(field.object_schema)
    if field.wire_type == "array":
        array_item: dict[str, object] = {}
        if field.array_item_wire_type is not None:
            array_item["wire_type"] = field.array_item_wire_type
        if field.array_item_object is not None:
            array_item["object"] = _object_manifest(field.array_item_object)
        value["array_item"] = array_item
        value["unique_items"] = field.unique_items
    return value


def _object_manifest(schema: WireObject) -> dict[str, object]:
    return {
        "additional_fields_forbidden": schema.additional_fields_forbidden,
        "fields": [_field_manifest(field) for field in schema.fields],
        "required_fields": [field.name for field in schema.fields],
        "wire_type": "object",
    }


def output_contract_manifest(descriptor: OutputContractDescriptor = CANONICAL_OUTPUT_CONTRACT) -> dict[str, object]:
    """Return the complete static contract as ordinary machine JSON data."""
    return {
        "contract_id": descriptor.contract_id,
        "contract_version": descriptor.contract_version,
        "duplicate_object_keys_forbidden": descriptor.duplicate_object_keys_forbidden,
        "enums": {enum_type.__name__: _enum_values(enum_type) for enum_type in descriptor.enum_types},
        "root": _object_manifest(descriptor.root),
    }


def canonical_output_contract_manifest(
    descriptor: OutputContractDescriptor = CANONICAL_OUTPUT_CONTRACT,
) -> bytes:
    """Serialize the static contract using the project's canonical JSON form."""
    return canonical_json(output_contract_manifest(descriptor))


def render_output_contract_manifest(
    descriptor: OutputContractDescriptor = CANONICAL_OUTPUT_CONTRACT,
) -> str:
    """Render exactly the canonical machine manifest for a future model request."""
    return canonical_output_contract_manifest(descriptor).decode("utf-8")


def output_contract_digest(descriptor: OutputContractDescriptor = CANONICAL_OUTPUT_CONTRACT) -> str:
    """Return the domain-separated SHA-256 identity of one static contract."""
    return sha256(
        OUTPUT_CONTRACT_DIGEST_DOMAIN.encode("utf-8") + b"\0" + canonical_output_contract_manifest(descriptor)
    ).hexdigest()


def _reject(failure_subtype: str) -> NoReturn:
    raise OutputContractStructuralError(failure_subtype)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _reject("OUTPUT_CONTRACT_FAILURE")
        value[key] = item
    return value


def _validate_field(value: object, field: WireField) -> None:
    if value is None:
        if field.nullable:
            return
        _reject("OUTPUT_TYPE_FAILURE")
    if field.wire_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            _reject("OUTPUT_TYPE_FAILURE")
    elif field.wire_type == "string":
        if not isinstance(value, str):
            _reject("OUTPUT_TYPE_FAILURE")
        if not field.allows_empty and not value:
            _reject("OUTPUT_CONTRACT_FAILURE")
    elif field.wire_type == "object":
        if field.object_schema is None:
            _reject("OUTPUT_CONTRACT_FAILURE")
        _validate_object(value, field.object_schema)
    elif field.wire_type == "array":
        if not isinstance(value, list):
            _reject("OUTPUT_TYPE_FAILURE")
        for item in value:
            if field.array_item_wire_type == "string":
                if not isinstance(item, str):
                    _reject("OUTPUT_REFERENCE_FAILURE")
            elif field.array_item_object is not None:
                _validate_object(item, field.array_item_object)
            else:
                _reject("OUTPUT_CONTRACT_FAILURE")
        if field.unique_items and len(set(value)) != len(value):
            _reject("OUTPUT_REFERENCE_FAILURE")
        if not field.allows_empty and not value:
            _reject("OUTPUT_CONTRACT_FAILURE")
    else:
        _reject("OUTPUT_CONTRACT_FAILURE")
    if field.literal is not None and value != field.literal:
        _reject("OUTPUT_ENUM_FAILURE" if field.enum_type is not None else "OUTPUT_CONTRACT_FAILURE")
    if field.enum_type is not None and value not in {item.value for item in field.enum_type}:
        _reject("OUTPUT_ENUM_FAILURE")


def _validate_object(value: object, schema: WireObject) -> None:
    if not isinstance(value, dict):
        _reject("OUTPUT_SHAPE_FAILURE")
    expected = {field.name for field in schema.fields}
    if schema.additional_fields_forbidden and set(value) != expected:
        _reject("OUTPUT_SHAPE_FAILURE")
    for field in schema.fields:
        if field.name not in value:
            _reject("OUTPUT_SHAPE_FAILURE")
        _validate_field(value[field.name], field)


def validate_output_contract_structure(
    raw: str,
    descriptor: OutputContractDescriptor = CANONICAL_OUTPUT_CONTRACT,
) -> None:
    """Validate only static JSON grammar; never packet-local or semantic rules."""
    if not isinstance(raw, str):
        _reject("JSON_SYNTAX_FAILURE")
    try:
        decoded: object = json.loads(raw, object_pairs_hook=_pairs)
    except (json.JSONDecodeError, UnicodeDecodeError, OutputContractStructuralError) as error:
        if isinstance(error, OutputContractStructuralError):
            raise OutputContractStructuralError(error.failure_subtype) from error
        raise OutputContractStructuralError("JSON_SYNTAX_FAILURE") from error
    _validate_object(decoded, descriptor.root)


def _annotation_is_nullable(annotation: object) -> bool:
    return get_origin(annotation) in {UnionType, Any} or type(None) in get_args(annotation)


def _assert_annotation(owner: type[Any], field: WireField) -> None:
    annotation = get_type_hints(owner)[field.name]
    if field.wire_type == "integer":
        if annotation is not int:
            raise AssertionError(f"{owner.__name__}.{field.name} integer parity")
    elif field.wire_type == "string":
        if field.enum_type is not None:
            if annotation is not field.enum_type:
                raise AssertionError(f"{owner.__name__}.{field.name} enum parity")
        elif not field.nullable and annotation is not str:
            raise AssertionError(f"{owner.__name__}.{field.name} string parity")
        elif field.nullable and not _annotation_is_nullable(annotation):
            raise AssertionError(f"{owner.__name__}.{field.name} nullability parity")
    elif field.wire_type == "array":
        if get_origin(annotation) is not tuple:
            raise AssertionError(f"{owner.__name__}.{field.name} array parity")
        item_type = get_args(annotation)[0]
        expected = field.array_item_model or (
            field.array_item_object.model_type if field.array_item_object is not None else str
        )
        if item_type is not expected:
            raise AssertionError(f"{owner.__name__}.{field.name} item parity")


def _assert_object_parity(schema: WireObject) -> None:
    actual_fields = fields(schema.model_type)
    expected_names = tuple(field.name for field in schema.fields)
    if tuple(field.name for field in actual_fields) != expected_names:
        raise AssertionError(f"{schema.name} field parity")
    if any(field.default is not MISSING or field.default_factory is not MISSING for field in actual_fields):
        raise AssertionError(f"{schema.name} default parity")
    if not schema.additional_fields_forbidden:
        raise AssertionError(f"{schema.name} exactness parity")
    for field in schema.fields:
        _assert_annotation(schema.model_type, field)
        if field.array_item_object is not None:
            _assert_object_parity(field.array_item_object)
        if field.array_item_model is EvidenceReferenceUse:
            if tuple(item.name for item in fields(EvidenceReferenceUse)) != ("token",):
                raise AssertionError("EvidenceReferenceUse wire parity")
    expected_literal = {
        ObservationItem: SemanticClass.OBSERVATION.value,
        InterpretationItem: SemanticClass.INTERPRETATION.value,
        HypothesisItem: SemanticClass.HYPOTHESIS.value,
        ExplorationItem: SemanticClass.EXPLORATION.value,
    }.get(schema.model_type)
    if expected_literal is not None:
        semantic_field = schema.fields[0]
        if semantic_field.name != "semantic_class" or semantic_field.literal != expected_literal:
            raise AssertionError(f"{schema.name} semantic-class literal parity")


def assert_output_contract_parity(
    descriptor: OutputContractDescriptor = CANONICAL_OUTPUT_CONTRACT,
) -> None:
    """Assert descriptor alignment with the frozen typed models and enums."""
    if descriptor.contract_id != OUTPUT_CONTRACT_ID or descriptor.contract_version != OUTPUT_CONTRACT_VERSION:
        raise AssertionError("output contract identity parity")
    if descriptor.root is not _ROOT:
        _assert_object_parity(descriptor.root)
    else:
        _assert_object_parity(_ROOT)
    expected_enums: tuple[type[Enum], ...] = (
        LLMTaskDomain,
        InterpretationStatus,
        SemanticClass,
        ExplorationCapabilityClass,
    )
    if descriptor.enum_types != expected_enums:
        raise AssertionError("output contract enum type parity")
    semantic_literals = {
        _OBSERVATION.fields[0].literal,
        _INTERPRETATION.fields[0].literal,
        _HYPOTHESIS.fields[0].literal,
        _EXPLORATION.fields[0].literal,
    }
    if semantic_literals != {item.value for item in SemanticClass}:
        raise AssertionError("semantic-class literal parity")
