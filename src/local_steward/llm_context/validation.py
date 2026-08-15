"""Small deterministic contract validation for Context packets and model output."""

import re

from .canonical import packet_digest
from .models import (
    CONTEXT_PROTOCOL_VERSION,
    EvidenceReferenceKind,
    ExplorationCapabilityClass,
    LLMContextPacket,
    LLMInterpretationResult,
    LLMTaskDomain,
    LLMValidationResult,
    REQUEST_CONSTRAINTS_VERSION,
    RequestConstraints,
    ValidationStatus,
    ValidationViolation,
)


_SUPPORTED_DOMAINS = frozenset({LLMTaskDomain.STATIC_SNAPSHOT, LLMTaskDomain.STATIC_PAIR_COMPARISON})
_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SAFE_DELETE_CLAIM", re.compile(r"\bsafe to delete\b|可以安全删除", re.IGNORECASE)),
    ("PHYSICAL_RECLAIMABLE_CLAIM", re.compile(r"\bphysical reclaimable\b|物理可回收空间", re.IGNORECASE)),
    ("CANDIDATE_MOVE_CLAIM", re.compile(r"\bcandidate\b.*\b(is|was|means)\b.*\b(move|rename)\b|候选.*(确定|确认).*(移动|重命名)", re.IGNORECASE)),
    ("SINGLE_SNAPSHOT_TREND_CLAIM", re.compile(r"\b(single snapshot|this snapshot)\b.*\b(trend|growth)\b|单个快照.*趋势", re.IGNORECASE)),
    ("SINGLE_PAIR_TREND_CLAIM", re.compile(r"\b(single pair|this pair)\b.*\b(long.?term trend|periodicity|lifecycle)\b|单个快照对.*(长期趋势|周期|生命周期)", re.IGNORECASE)),
    ("UNKNOWN_CERTAINTY_CLAIM", re.compile(r"\bunknown\b.*\b(is|equals)\b.*\b0\b|未知.*(就是|等于).*0", re.IGNORECASE)),
    ("ACTION_CLAIM", re.compile(r"\b(i |we )?(deleted|moved|renamed|archived)\b|已(删除|移动|重命名|归档)", re.IGNORECASE)),
    ("UNAVAILABLE_OBSERVATION_CLAIM", re.compile(r"\b(i |we )?(scanned|hashed|created a snapshot|read the live filesystem)\b|已(扫描|哈希|创建快照|读取实时文件系统)", re.IGNORECASE)),
)


def _violation(values: list[ValidationViolation], code: str) -> None:
    if code not in {item.code for item in values}:
        values.append(ValidationViolation(code))


def _all_text(result: LLMInterpretationResult) -> tuple[str, ...]:
    values = [result.summary, *result.unknowns, *result.limitations]
    values.extend(item.statement for item in result.observations)
    values.extend(item.statement for item in result.interpretations)
    values.extend(item.statement for item in result.hypotheses)
    values.extend(item.question for item in result.explorations)
    values.extend(item.target for item in result.explorations)
    values.extend(item.expected_value for item in result.explorations)
    return tuple(values)


def validate_context_packet(packet: LLMContextPacket) -> tuple[ValidationViolation, ...]:
    """Validate packet identity and its packet-local typed reference wrappers."""
    violations: list[ValidationViolation] = []
    if packet.protocol_version != CONTEXT_PROTOCOL_VERSION:
        _violation(violations, "PACKET_PROTOCOL_VERSION_INVALID")
    if packet.task_domain not in _SUPPORTED_DOMAINS:
        _violation(violations, "TASK_DOMAIN_UNSUPPORTED")
    if not packet.projection_digest:
        _violation(violations, "PROJECTION_DIGEST_MISSING")
    if packet.packet_digest != packet_digest(packet):
        _violation(violations, "PACKET_DIGEST_INVALID")
    tokens = [item.token for item in packet.evidence_references]
    if len(tokens) != len(set(tokens)) or tuple(tokens) != tuple(sorted(tokens)):
        _violation(violations, "PACKET_REFERENCE_SET_INVALID")
    for reference in packet.evidence_references:
        payload_count = sum(
            item is not None
            for item in (
                reference.entry_reference,
                reference.result_reference,
                reference.accounting_domain,
                reference.expansion_descriptor,
                reference.source_identity,
            )
        )
        if payload_count != 1:
            _violation(violations, "PACKET_REFERENCE_PAYLOAD_INVALID")
            continue
        expected = {
            EvidenceReferenceKind.ENTRY: reference.entry_reference is not None,
            EvidenceReferenceKind.RESULT_LOCAL: reference.result_reference is not None,
            EvidenceReferenceKind.ACCOUNTING: reference.accounting_domain is not None,
            EvidenceReferenceKind.EXPANSION: reference.expansion_descriptor is not None,
            EvidenceReferenceKind.PROJECTION_SOURCE: reference.source_identity is not None,
        }
        if not expected[reference.kind]:
            _violation(violations, "PACKET_REFERENCE_KIND_INVALID")
    return tuple(violations)


def validate_interpretation_result(
    packet: LLMContextPacket, result: LLMInterpretationResult
) -> LLMValidationResult:
    """Check packet-local citations and only the explicitly frozen hard boundaries."""
    violations = list(validate_context_packet(packet))
    if result.protocol_version != CONTEXT_PROTOCOL_VERSION:
        _violation(violations, "RESULT_PROTOCOL_VERSION_INVALID")
    if result.task_domain != packet.task_domain:
        _violation(violations, "RESULT_TASK_DOMAIN_MISMATCH")
    if result.task_domain not in _SUPPORTED_DOMAINS:
        _violation(violations, "TASK_DOMAIN_UNSUPPORTED")
    available = {item.token: item for item in packet.evidence_references}
    declared = tuple(item.token for item in result.evidence_references)
    if len(declared) != len(set(declared)) or tuple(declared) != tuple(sorted(declared)):
        _violation(violations, "RESULT_REFERENCE_SET_INVALID")
    if any(token not in available for token in declared):
        _violation(violations, "EVIDENCE_REFERENCE_INVALID")
    used: set[str] = set()
    for observation in result.observations:
        if not observation.statement.strip():
            _violation(violations, "OBSERVATION_STATEMENT_INVALID")
        if not observation.evidence_references:
            _violation(violations, "OBSERVATION_EVIDENCE_REQUIRED")
        used.update(reference.token for reference in observation.evidence_references)
    for interpretation in result.interpretations:
        if not interpretation.statement.strip() or not interpretation.supporting_evidence_references:
            _violation(violations, "INTERPRETATION_EVIDENCE_REQUIRED")
        used.update(reference.token for reference in interpretation.supporting_evidence_references)
    for hypothesis in result.hypotheses:
        if not hypothesis.statement.strip() or not hypothesis.supporting_evidence_references:
            _violation(violations, "HYPOTHESIS_EVIDENCE_REQUIRED")
        if not hypothesis.missing_information:
            _violation(violations, "HYPOTHESIS_MISSING_INFORMATION_REQUIRED")
        used.update(reference.token for reference in hypothesis.supporting_evidence_references)
    for exploration in result.explorations:
        if not all(value.strip() for value in (exploration.question, exploration.target, exploration.expected_value)):
            _violation(violations, "EXPLORATION_FIELDS_INVALID")
        if not exploration.supporting_evidence_references or not exploration.missing_information:
            _violation(violations, "EXPLORATION_EVIDENCE_REQUIRED")
        used.update(reference.token for reference in exploration.supporting_evidence_references)
        if exploration.capability_class == ExplorationCapabilityClass.CURRENT_PROJECTION_EXPANSION:
            reference = available.get(exploration.target)
            if reference is None or reference.kind != EvidenceReferenceKind.EXPANSION:
                _violation(violations, "EXPANSION_DESCRIPTOR_REQUIRED")
    if any(token not in available for token in used):
        _violation(violations, "EVIDENCE_REFERENCE_INVALID")
    if set(declared) != used:
        _violation(violations, "RESULT_REFERENCE_SET_INCOMPLETE")
    for text in _all_text(result):
        for code, pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(text):
                _violation(violations, code)
    return LLMValidationResult(
        ValidationStatus.INVALID if violations else ValidationStatus.VALID,
        tuple(sorted(violations, key=lambda item: item.code)),
    )


def validate_request_constraints(
    constraints: RequestConstraints, result: LLMInterpretationResult
) -> LLMValidationResult:
    """Validate dynamic registry use without packet traversal or semantic review."""
    violations: list[ValidationViolation] = []
    tokens = constraints.evidence_tokens
    capabilities = constraints.capability_classes
    expansion_targets = constraints.expansion_target_tokens
    if constraints.schema_version != REQUEST_CONSTRAINTS_VERSION:
        _violation(violations, "REQUEST_CONSTRAINTS_SCHEMA_INVALID")
    if (
        tuple(sorted(tokens)) != tokens
        or len(tokens) != len(set(tokens))
        or any(not isinstance(token, str) for token in tokens)
    ):
        _violation(violations, "REQUEST_CONSTRAINTS_REGISTRY_INVALID")
    if tuple(sorted(item.value for item in capabilities)) != tuple(item.value for item in capabilities):
        _violation(violations, "REQUEST_CONSTRAINTS_CAPABILITY_INVALID")
    if len(capabilities) != len(set(capabilities)):
        _violation(violations, "REQUEST_CONSTRAINTS_CAPABILITY_INVALID")
    if tuple(sorted(item.value for item in constraints.excluded_capability_classes)) != tuple(
        item.value for item in constraints.excluded_capability_classes
    ):
        _violation(violations, "REQUEST_CONSTRAINTS_CAPABILITY_INVALID")
    if (
        set(capabilities).intersection(constraints.excluded_capability_classes)
        or set(capabilities).union(constraints.excluded_capability_classes) != set(ExplorationCapabilityClass)
    ):
        _violation(violations, "REQUEST_CONSTRAINTS_CAPABILITY_INVALID")
    if (
        constraints.evidence_token_wire_type != "string"
        or not constraints.evidence_array_unique
        or constraints.top_level_reference_rule != "SORTED_UNIQUE_ITEM_USE_UNION"
        or constraints.token_order_rule != "LEXICOGRAPHIC_ASCENDING"
        or constraints.empty_registry_rule != "EMPTY_ARRAY"
    ):
        _violation(violations, "REQUEST_CONSTRAINTS_SCHEMA_INVALID")
    if tuple(sorted(expansion_targets)) != expansion_targets or len(expansion_targets) != len(set(expansion_targets)):
        _violation(violations, "REQUEST_CONSTRAINTS_EXPANSION_INVALID")
    if any(token not in tokens for token in expansion_targets):
        _violation(violations, "REQUEST_CONSTRAINTS_EXPANSION_INVALID")
    if result.task_domain != constraints.task_domain:
        _violation(violations, "RESULT_TASK_DOMAIN_MISMATCH")
    declared = tuple(item.token for item in result.evidence_references)
    if len(declared) != len(set(declared)) or tuple(declared) != tuple(sorted(declared)):
        _violation(violations, "RESULT_REFERENCE_SET_INVALID")
    if any(token not in tokens for token in declared):
        _violation(violations, "EVIDENCE_REFERENCE_INVALID")
    used: set[str] = set()
    for observation in result.observations:
        used.update(reference.token for reference in observation.evidence_references)
    for interpretation in result.interpretations:
        used.update(reference.token for reference in interpretation.supporting_evidence_references)
    for hypothesis in result.hypotheses:
        used.update(reference.token for reference in hypothesis.supporting_evidence_references)
    for exploration in result.explorations:
        used.update(reference.token for reference in exploration.supporting_evidence_references)
        if exploration.capability_class not in capabilities:
            _violation(violations, "EXPLORATION_CAPABILITY_INVALID")
        if (
            exploration.capability_class == ExplorationCapabilityClass.CURRENT_PROJECTION_EXPANSION
            and exploration.target not in expansion_targets
        ):
            _violation(violations, "EXPANSION_DESCRIPTOR_REQUIRED")
    if any(token not in tokens for token in used):
        _violation(violations, "EVIDENCE_REFERENCE_INVALID")
    if set(declared) != used:
        _violation(violations, "RESULT_REFERENCE_SET_INCOMPLETE")
    return LLMValidationResult(
        ValidationStatus.INVALID if violations else ValidationStatus.VALID,
        tuple(sorted(violations, key=lambda item: item.code)),
    )
