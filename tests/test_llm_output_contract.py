"""Offline parity checks for the frozen LLM interpretation output contract."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest

from local_steward.llm_context import (
    CANONICAL_OUTPUT_CONTRACT,
    OUTPUT_CONTRACT_DIGEST_DOMAIN,
    OUTPUT_CONTRACT_ID,
    OUTPUT_CONTRACT_VERSION,
    OutputContractStructuralError,
    assert_output_contract_parity,
    canonical_output_contract_manifest,
    output_contract_digest,
    output_contract_manifest,
    parse_interpretation_result,
    render_output_contract_manifest,
    validate_output_contract_structure,
)
from local_steward.llm_context.models import (
    ExplorationCapabilityClass,
    InterpretationStatus,
    LLMTaskDomain,
    SemanticClass,
)


CONTRACT_V1_DIGEST = "f9b542c41a92731e29be8a3587ddf57b603277419a8142f8bada00aee1a2544b"


def _wire_value(
    *,
    task_domain: str = "STATIC_SNAPSHOT",
    status: str = "COMPLETED",
    nullable_values: bool = True,
) -> dict[str, object]:
    return {
        "protocol_version": 0,
        "task_domain": task_domain,
        "status": status,
        "summary": "summary",
        "observations": [
            {
                "semantic_class": "OBSERVATION",
                "statement": "observation",
                "evidence_references": ["token-a", "token-b"],
            }
        ],
        "interpretations": [
            {
                "semantic_class": "INTERPRETATION",
                "statement": "interpretation",
                "supporting_evidence_references": ["token-a"],
                "qualifications": ["qualification"],
            }
        ],
        "hypotheses": [
            {
                "semantic_class": "HYPOTHESIS",
                "statement": "hypothesis",
                "supporting_evidence_references": ["token-b"],
                "missing_information": ["missing"],
                "competing_explanation": None if nullable_values else "alternative",
                "discriminating_observation": None if nullable_values else "test",
            }
        ],
        "explorations": [
            {
                "semantic_class": "EXPLORATION",
                "question": "question",
                "target": "target",
                "supporting_evidence_references": ["token-a"],
                "missing_information": ["missing"],
                "expected_value": "value",
                "capability_class": "OUT_OF_SCOPE",
            }
        ],
        "unknowns": [],
        "limitations": [],
        "evidence_references": ["token-a", "token-b"],
    }


def _raw(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _assert_same_acceptance(raw: str, expected: bool) -> None:
    if expected:
        validate_output_contract_structure(raw)
        parse_interpretation_result(raw)
    else:
        with pytest.raises(OutputContractStructuralError):
            validate_output_contract_structure(raw)
        with pytest.raises(Exception) as parser_error:
            parse_interpretation_result(raw)
        assert getattr(parser_error.value, "code", None) == "LLM_OUTPUT_PARSE_INVALID"


def _nested_schema(name: str):  # type: ignore[no-untyped-def]
    root_field = next(field for field in CANONICAL_OUTPUT_CONTRACT.root.fields if field.name == name)
    assert root_field.array_item_object is not None
    return root_field.array_item_object


def test_contract_identity_field_parity_and_fixed_literals() -> None:
    assert CANONICAL_OUTPUT_CONTRACT.contract_id == OUTPUT_CONTRACT_ID
    assert CANONICAL_OUTPUT_CONTRACT.contract_version == OUTPUT_CONTRACT_VERSION == 1
    assert_output_contract_parity()
    assert tuple(field.name for field in CANONICAL_OUTPUT_CONTRACT.root.fields) == (
        "protocol_version",
        "task_domain",
        "status",
        "summary",
        "observations",
        "interpretations",
        "hypotheses",
        "explorations",
        "unknowns",
        "limitations",
        "evidence_references",
    )
    assert tuple(field.name for field in _nested_schema("observations").fields) == (
        "semantic_class", "statement", "evidence_references"
    )
    assert tuple(field.name for field in _nested_schema("interpretations").fields) == (
        "semantic_class", "statement", "supporting_evidence_references", "qualifications"
    )
    assert tuple(field.name for field in _nested_schema("hypotheses").fields) == (
        "semantic_class", "statement", "supporting_evidence_references", "missing_information",
        "competing_explanation", "discriminating_observation",
    )
    assert tuple(field.name for field in _nested_schema("explorations").fields) == (
        "semantic_class", "question", "target", "supporting_evidence_references",
        "missing_information", "expected_value", "capability_class",
    )
    assert tuple(schema.fields[0].literal for schema in (
        _nested_schema("observations"), _nested_schema("interpretations"),
        _nested_schema("hypotheses"), _nested_schema("explorations"),
    )) == tuple(item.value for item in SemanticClass)


def test_descriptor_enums_are_the_actual_enum_values_and_manifest_is_complete() -> None:
    manifest = output_contract_manifest()
    assert manifest["enums"] == {
        "LLMTaskDomain": [item.value for item in LLMTaskDomain],
        "InterpretationStatus": [item.value for item in InterpretationStatus],
        "SemanticClass": [item.value for item in SemanticClass],
        "ExplorationCapabilityClass": [item.value for item in ExplorationCapabilityClass],
    }
    assert json.loads(render_output_contract_manifest()) == manifest
    root = manifest["root"]
    assert isinstance(root, dict)
    assert root["additional_fields_forbidden"] is True
    assert root["required_fields"] == [field.name for field in CANONICAL_OUTPUT_CONTRACT.root.fields]


def test_manifest_and_digest_are_canonical_domain_separated_and_frozen() -> None:
    first = canonical_output_contract_manifest()
    second = canonical_output_contract_manifest()
    assert first == second == render_output_contract_manifest().encode("utf-8")
    assert output_contract_digest() == CONTRACT_V1_DIGEST
    assert output_contract_digest() != sha256(first).hexdigest()
    assert output_contract_digest() == sha256(
        OUTPUT_CONTRACT_DIGEST_DOMAIN.encode("utf-8") + b"\0" + first
    ).hexdigest()


def test_contract_mutation_changes_digest_and_descriptor_drift_is_rejected() -> None:
    changed_version = replace(CANONICAL_OUTPUT_CONTRACT, contract_version=2)
    assert output_contract_digest(changed_version) != output_contract_digest()
    changed_type = replace(CANONICAL_OUTPUT_CONTRACT.root.fields[0], wire_type="string")
    changed_type_root = replace(
        CANONICAL_OUTPUT_CONTRACT.root,
        fields=(changed_type, *CANONICAL_OUTPUT_CONTRACT.root.fields[1:]),
    )
    assert output_contract_digest(replace(CANONICAL_OUTPUT_CONTRACT, root=changed_type_root)) != output_contract_digest()
    changed_exactness_root = replace(CANONICAL_OUTPUT_CONTRACT.root, additional_fields_forbidden=False)
    assert output_contract_digest(replace(CANONICAL_OUTPUT_CONTRACT, root=changed_exactness_root)) != output_contract_digest()
    hypothesis = _nested_schema("hypotheses")
    changed_nullability = replace(hypothesis.fields[-1], nullable=False)
    changed_hypothesis = replace(hypothesis, fields=(*hypothesis.fields[:-1], changed_nullability))
    root_hypotheses = next(field for field in CANONICAL_OUTPUT_CONTRACT.root.fields if field.name == "hypotheses")
    changed_root_hypotheses = replace(root_hypotheses, array_item_object=changed_hypothesis)
    changed_null_root = replace(
        CANONICAL_OUTPUT_CONTRACT.root,
        fields=tuple(
            changed_root_hypotheses if field.name == "hypotheses" else field
            for field in CANONICAL_OUTPUT_CONTRACT.root.fields
        ),
    )
    assert output_contract_digest(replace(CANONICAL_OUTPUT_CONTRACT, root=changed_null_root)) != output_contract_digest()
    missing_root_field = replace(
        CANONICAL_OUTPUT_CONTRACT.root,
        fields=CANONICAL_OUTPUT_CONTRACT.root.fields[:-1],
    )
    with pytest.raises(AssertionError, match="field parity"):
        assert_output_contract_parity(replace(CANONICAL_OUTPUT_CONTRACT, root=missing_root_field))
    with pytest.raises(AssertionError, match="enum type parity"):
        assert_output_contract_parity(replace(CANONICAL_OUTPUT_CONTRACT, enum_types=()))


@pytest.mark.parametrize("task_domain", [item.value for item in LLMTaskDomain])
@pytest.mark.parametrize("status", [item.value for item in InterpretationStatus])
def test_all_enum_values_are_structurally_legal_and_match_parser(task_domain: str, status: str) -> None:
    _assert_same_acceptance(_raw(_wire_value(task_domain=task_domain, status=status)), True)


@pytest.mark.parametrize("nullable_values", [True, False])
def test_all_nested_shapes_and_hypothesis_nullability_match_parser(nullable_values: bool) -> None:
    _assert_same_acceptance(_raw(_wire_value(nullable_values=nullable_values)), True)


@pytest.mark.parametrize("capability", [item.value for item in ExplorationCapabilityClass])
def test_all_capability_literals_are_structurally_legal_and_match_parser(capability: str) -> None:
    value = _wire_value()
    exploration = value["explorations"]
    assert isinstance(exploration, list) and isinstance(exploration[0], dict)
    exploration[0]["capability_class"] = capability
    _assert_same_acceptance(_raw(value), True)


def test_descriptor_explicitly_records_empty_and_duplicate_rules() -> None:
    all_fields = [
        *CANONICAL_OUTPUT_CONTRACT.root.fields,
        *(_nested_schema("observations").fields),
        *(_nested_schema("interpretations").fields),
        *(_nested_schema("hypotheses").fields),
        *(_nested_schema("explorations").fields),
    ]
    assert all(field.allows_empty for field in all_fields if field.wire_type in {"array", "string"})
    unique_names = [field.name for field in all_fields if field.unique_items]
    assert unique_names == [
        "evidence_references",
        "evidence_references",
        "supporting_evidence_references",
        "supporting_evidence_references",
        "supporting_evidence_references",
    ]


def test_structural_check_deliberately_skips_request_local_and_semantic_validation() -> None:
    value = _wire_value(task_domain="TEMPORAL_SEQUENCE")
    value["summary"] = "This duplicate is safe to delete."
    _assert_same_acceptance(_raw(value), True)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"protocol_version": "0"}),
        lambda value: value.update({"task_domain": "unknown"}),
        lambda value: value.update({"status": "completed"}),
        lambda value: value.update({"summary": None}),
        lambda value: value.pop("summary"),
        lambda value: value.update({"extra": True}),
        lambda value: value.update({"unknowns": [None]}),
        lambda value: value["observations"][0].pop("statement"),  # type: ignore[index]
        lambda value: value["interpretations"][0].update({"extra": True}),  # type: ignore[index]
        lambda value: value["hypotheses"][0].update({"semantic_class": "OBSERVATION"}),  # type: ignore[index]
        lambda value: value["hypotheses"][0].update({"competing_explanation": 3}),  # type: ignore[index]
        lambda value: value["explorations"][0].update({"question": None}),  # type: ignore[index]
        lambda value: value["explorations"][0].update({"capability_class": "outside_scope"}),  # type: ignore[index]
        lambda value: value["observations"][0].update({"evidence_references": "token-a"}),  # type: ignore[index]
        lambda value: value["observations"][0].update({"evidence_references": ["token-a", 1]}),  # type: ignore[index]
        lambda value: value["observations"][0].update({"evidence_references": ["token-a", "token-a"]}),  # type: ignore[index]
    ],
)
def test_invalid_static_shapes_match_parser(mutate) -> None:  # type: ignore[no-untyped-def]
    value = deepcopy(_wire_value())
    mutate(value)
    _assert_same_acceptance(_raw(value), False)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '"string"',
        '{"protocol_version":0,"protocol_version":0}',
        '{"protocol_version":0} trailing',
    ],
)
def test_invalid_json_or_duplicate_keys_match_parser(raw: str) -> None:
    _assert_same_acceptance(raw, False)


def test_manifest_contains_no_request_or_provider_data() -> None:
    rendered = render_output_contract_manifest()
    for forbidden in ("token-a", "prompt", "context", "Authorization", "api_key", "DeepSeek"):
        assert forbidden not in rendered
