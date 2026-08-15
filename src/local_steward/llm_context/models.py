"""Immutable, provider-neutral LLM Context Layer foundation models."""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..models import SnapshotEntryReference
from ..observation_projection.models import (
    Accounting,
    AccountingDomain,
    DiagnosticState,
    ExpansionDescriptor,
    ExplicitEntryAnchor,
    HierarchyItem,
    OverlayItem,
    PairTrackingGrowthHierarchyItem,
    ProjectionMode,
    ProjectionPolicy,
    ProjectionSourceIdentity,
    ResultLocalReference,
    SourcePlanState,
    SourcePlanItem,
    SourceResultIdentity,
    TrackingItem,
)


CONTEXT_PROTOCOL_VERSION = 0
CONTEXT_DIGEST_DOMAIN = "local_steward.llm_context_packet.v0"
REQUEST_CONSTRAINTS_VERSION = 1
REQUEST_CONSTRAINTS_DIGEST_DOMAIN = "local_steward.llm_request_constraints.v1"


class LLMTaskDomain(str, Enum):
    STATIC_SNAPSHOT = "STATIC_SNAPSHOT"
    STATIC_PAIR_COMPARISON = "STATIC_PAIR_COMPARISON"
    TEMPORAL_SEQUENCE = "TEMPORAL_SEQUENCE"
    TEMPORAL_WINDOW = "TEMPORAL_WINDOW"
    LONGITUDINAL_TREND = "LONGITUDINAL_TREND"
    PERIODICITY = "PERIODICITY"
    LIFECYCLE_STATE = "LIFECYCLE_STATE"


class ContextOmissionCategory(str, Enum):
    EXPLICIT_ENTRY_ANCHORS = "EXPLICIT_ENTRY_ANCHORS"
    TRACKING_ITEMS = "TRACKING_ITEMS"
    HIERARCHY_ITEMS = "HIERARCHY_ITEMS"
    OVERLAYS = "OVERLAYS"
    EXPANSION_DESCRIPTORS = "EXPANSION_DESCRIPTORS"


class ContextSourceRepresentation(str, Enum):
    EXPLICIT = "EXPLICIT"
    AGGREGATE_ACCOUNTED = "AGGREGATE_ACCOUNTED"


class EvidenceReferenceKind(str, Enum):
    ENTRY = "ENTRY"
    RESULT_LOCAL = "RESULT_LOCAL"
    ACCOUNTING = "ACCOUNTING"
    EXPANSION = "EXPANSION"
    PROJECTION_SOURCE = "PROJECTION_SOURCE"


class InterpretationStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    NEEDS_MORE_OBSERVATION = "NEEDS_MORE_OBSERVATION"
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"
    UNSUPPORTED_TASK = "UNSUPPORTED_TASK"
    INVALID_MODEL_OUTPUT = "INVALID_MODEL_OUTPUT"


class SemanticClass(str, Enum):
    OBSERVATION = "OBSERVATION"
    INTERPRETATION = "INTERPRETATION"
    HYPOTHESIS = "HYPOTHESIS"
    EXPLORATION = "EXPLORATION"


class ExplorationCapabilityClass(str, Enum):
    CURRENT_PROJECTION_EXPANSION = "CURRENT_PROJECTION_EXPANSION"
    CURRENT_SNAPSHOT_REPROJECTION = "CURRENT_SNAPSHOT_REPROJECTION"
    NEW_OBSERVATION_REQUIRED = "NEW_OBSERVATION_REQUIRED"
    FUTURE_TEMPORAL_CAPABILITY = "FUTURE_TEMPORAL_CAPABILITY"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class ValidationStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ContextBudget:
    max_explicit_facts: int
    max_hierarchy_items: int
    max_overlays: int
    max_expansion_descriptors: int


@dataclass(frozen=True, slots=True)
class UserIntentContext:
    question: str
    scope_emphasis: str | None = None
    user_provided_context: str | None = None


@dataclass(frozen=True, slots=True)
class ContextRequestScope:
    scope: str | None
    path_prefix: str | None


@dataclass(frozen=True, slots=True)
class ContextOmission:
    category: ContextOmissionCategory
    omitted_count: int
    source_representation: ContextSourceRepresentation
    expansion_descriptor: ExpansionDescriptor | None = None


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """Packet-local wrapper around an existing typed Projection reference."""

    kind: EvidenceReferenceKind
    token: str
    entry_reference: SnapshotEntryReference | None = None
    result_reference: ResultLocalReference | None = None
    accounting_domain: AccountingDomain | None = None
    expansion_descriptor: ExpansionDescriptor | None = None
    source_identity: ProjectionSourceIdentity | None = None


@dataclass(frozen=True, slots=True)
class ContextGrowthHierarchy:
    source_result_identity: SourceResultIdentity | None
    state: SourcePlanState
    hierarchy_items: tuple[PairTrackingGrowthHierarchyItem, ...]


@dataclass(frozen=True, slots=True)
class LLMContextPacket:
    protocol_version: int
    task_domain: LLMTaskDomain
    user_intent: UserIntentContext
    context_budget: ContextBudget
    projection_mode: ProjectionMode
    projection_digest: str
    source_identity: ProjectionSourceIdentity
    normalized_request_scope: ContextRequestScope
    resolved_policy_summary: ProjectionPolicy
    source_plan: tuple[SourcePlanItem, ...]
    diagnostic_state: DiagnosticState
    explicit_entry_anchors: tuple[ExplicitEntryAnchor, ...]
    tracking_items: tuple[TrackingItem, ...]
    hierarchy_items: tuple[HierarchyItem, ...]
    growth_hierarchy: ContextGrowthHierarchy | None
    overlays: tuple[OverlayItem, ...]
    independent_accounting: tuple[Accounting, ...]
    expansion_descriptors: tuple[ExpansionDescriptor, ...]
    context_omissions: tuple[ContextOmission, ...]
    evidence_references: tuple[EvidenceReference, ...]
    packet_digest: str


@dataclass(frozen=True, slots=True)
class EvidenceReferenceUse:
    token: str


@dataclass(frozen=True, slots=True)
class ObservationItem:
    semantic_class: SemanticClass
    statement: str
    evidence_references: tuple[EvidenceReferenceUse, ...]


@dataclass(frozen=True, slots=True)
class InterpretationItem:
    semantic_class: SemanticClass
    statement: str
    supporting_evidence_references: tuple[EvidenceReferenceUse, ...]
    qualifications: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HypothesisItem:
    semantic_class: SemanticClass
    statement: str
    supporting_evidence_references: tuple[EvidenceReferenceUse, ...]
    missing_information: tuple[str, ...]
    competing_explanation: str | None
    discriminating_observation: str | None


@dataclass(frozen=True, slots=True)
class ExplorationItem:
    semantic_class: SemanticClass
    question: str
    target: str
    supporting_evidence_references: tuple[EvidenceReferenceUse, ...]
    missing_information: tuple[str, ...]
    expected_value: str
    capability_class: ExplorationCapabilityClass


@dataclass(frozen=True, slots=True)
class LLMInterpretationResult:
    protocol_version: int
    task_domain: LLMTaskDomain
    status: InterpretationStatus
    summary: str
    observations: tuple[ObservationItem, ...]
    interpretations: tuple[InterpretationItem, ...]
    hypotheses: tuple[HypothesisItem, ...]
    explorations: tuple[ExplorationItem, ...]
    unknowns: tuple[str, ...]
    limitations: tuple[str, ...]
    evidence_references: tuple[EvidenceReferenceUse, ...]


@dataclass(frozen=True, slots=True)
class ValidationViolation:
    code: str


@dataclass(frozen=True, slots=True)
class LLMValidationResult:
    status: ValidationStatus
    violations: tuple[ValidationViolation, ...]


@dataclass(frozen=True, slots=True)
class RequestConstraints:
    """Transient provider-neutral limits for one request's visible registry."""

    schema_version: int
    task_domain: LLMTaskDomain
    evidence_tokens: tuple[str, ...]
    capability_classes: tuple[ExplorationCapabilityClass, ...]
    expansion_target_tokens: tuple[str, ...]
    excluded_capability_classes: tuple[ExplorationCapabilityClass, ...] = ()
    evidence_token_wire_type: str = "string"
    evidence_array_unique: bool = True
    top_level_reference_rule: str = "SORTED_UNIQUE_ITEM_USE_UNION"
    token_order_rule: str = "LEXICOGRAPHIC_ASCENDING"
    empty_registry_rule: str = "EMPTY_ARRAY"


@dataclass(frozen=True, slots=True)
class LLMModelRequest:
    protocol_version: int
    task_domain: LLMTaskDomain
    instruction_contract: str
    context_packet_json: str
    required_output_schema: str
    request_constraints_json: str = ""
    output_contract_digest: str = ""
    request_constraints_digest: str = ""


class ModelCallable(Protocol):
    def __call__(self, request: LLMModelRequest) -> str: ...


@dataclass(frozen=True, slots=True)
class SandboxRunResult:
    packet: LLMContextPacket
    model_request: LLMModelRequest
    raw_response: str | None
    parsed_result: LLMInterpretationResult | None
    validation_result: LLMValidationResult | None
    failure_code: str | None
    failure_subtype: str | None = None
