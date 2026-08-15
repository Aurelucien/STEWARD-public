"""Immutable, repository-free Observation Projection v0 model contract."""

from dataclasses import dataclass
from enum import Enum

from ..models import (
    FilesystemObjectType,
    FilesystemObservationStatus,
    PayloadObservationProvenance,
    RelationCertainty,
    SnapshotDiffChangeType,
    SnapshotEntryReference,
)


SCHEMA_NAME = "local_steward.observation_projection"
SCHEMA_VERSION = 0
ALGORITHM = "observation_projection"
ALGORITHM_VERSION = 0
DIGEST_DOMAIN = "local_steward.observation_projection.v0"


class ProjectionMode(str, Enum):
    SNAPSHOT_DIAGNOSTIC = "SNAPSHOT_DIAGNOSTIC"
    PAIR_TRACKING = "PAIR_TRACKING"


class SourcePlanState(str, Enum):
    NOT_REQUESTED = "NOT_REQUESTED"
    REQUESTED_AND_EMPTY = "REQUESTED_AND_EMPTY"
    REQUESTED_AND_PRESENT = "REQUESTED_AND_PRESENT"


class BudgetValue(str, Enum):
    REQUIRES_CALIBRATION = "REQUIRES_CALIBRATION"


class ResultKind(str, Enum):
    SNAPSHOT = "SNAPSHOT"
    STRUCTURE = "STRUCTURE"
    GROWTH = "GROWTH"
    DIFF = "DIFF"
    RELATION = "RELATION"
    DUPLICATE = "DUPLICATE"


class CoverageAvailability(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class ProjectionSourceValidity(str, Enum):
    VALID = "VALID"


class PhysicalKnowledgeState(str, Enum):
    UNKNOWN = "UNKNOWN"


class EntrySourceSide(str, Enum):
    PRIMARY = "PRIMARY"
    BASE = "BASE"
    TARGET = "TARGET"


class EntrySizeState(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


class PayloadFactState(str, Enum):
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"
    VERIFIED = "VERIFIED"


class HierarchyPresentationState(str, Enum):
    FOLDED = "FOLDED"
    EXPANDED = "EXPANDED"


class ContentState(str, Enum):
    UNKNOWN = "UNKNOWN"
    VERIFIED_CHANGED = "VERIFIED_CHANGED"
    VERIFIED_UNCHANGED = "VERIFIED_UNCHANGED"


class AccountingDomain(str, Enum):
    SNAPSHOT_DIAGNOSTIC_ENTRY = "SNAPSHOT_DIAGNOSTIC_ENTRY"
    PAIR_TRACKING_LOCATION = "PAIR_TRACKING_LOCATION"
    GROWTH_REGULAR_LOCATION = "GROWTH_REGULAR_LOCATION"
    PAIR_TRACKING_GROWTH_HIERARCHY = "PAIR_TRACKING_GROWTH_HIERARCHY"
    DUPLICATE_OVERLAY = "DUPLICATE_OVERLAY"
    HARD_LINK_ALIAS_OVERLAY = "HARD_LINK_ALIAS_OVERLAY"
    RELATION_OVERLAY = "RELATION_OVERLAY"


class OverlayKind(str, Enum):
    DUPLICATE_GROUP = "DUPLICATE_GROUP"
    HARD_LINK_ALIAS_SET = "HARD_LINK_ALIAS_SET"
    RELATION_ITEM = "RELATION_ITEM"
    RELATION_AMBIGUITY_GROUP = "RELATION_AMBIGUITY_GROUP"


class SelectionReason(str, Enum):
    METADATA_FAILURE = "METADATA_FAILURE"
    OBSERVATION_FAILURE = "OBSERVATION_FAILURE"
    ACCESS_FAILURE = "ACCESS_FAILURE"
    UNKNOWN_SIZE = "UNKNOWN_SIZE"
    PAYLOAD_UNKNOWN = "PAYLOAD_UNKNOWN"
    EXCLUDED = "EXCLUDED"
    UNREADABLE = "UNREADABLE"
    NON_LOCAL = "NON_LOCAL"
    INTEGRITY_CONFLICT = "INTEGRITY_CONFLICT"
    REUSE_PROVENANCE = "REUSE_PROVENANCE"
    AMBIGUOUS_RELATION = "AMBIGUOUS_RELATION"
    USER_REQUESTED_LOCATION = "USER_REQUESTED_LOCATION"
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"
    SIZE_INCREASE = "SIZE_INCREASE"
    SIZE_DECREASE = "SIZE_DECREASE"
    CONTENT_CHANGED = "CONTENT_CHANGED"
    COVERAGE_CHANGED = "COVERAGE_CHANGED"
    TRANSITION_ENDPOINT = "TRANSITION_ENDPOINT"
    HARD_LINK_REPRESENTATIVE = "HARD_LINK_REPRESENTATIVE"
    DUPLICATE_REPRESENTATIVE = "DUPLICATE_REPRESENTATIVE"
    RELATION_COMPONENT_REPRESENTATIVE = "RELATION_COMPONENT_REPRESENTATIVE"
    SCOPE_BOUNDARY_REPRESENTATIVE = "SCOPE_BOUNDARY_REPRESENTATIVE"
    OBJECT_HINT_BOUNDARY_REPRESENTATIVE = "OBJECT_HINT_BOUNDARY_REPRESENTATIVE"
    LOGICAL_BYTE_CONTRIBUTOR = "LOGICAL_BYTE_CONTRIBUTOR"
    GROWTH_CONTRIBUTOR = "GROWTH_CONTRIBUTOR"
    STRUCTURE_ANCHOR = "STRUCTURE_ANCHOR"
    DIAGNOSTIC_NEIGHBOR = "DIAGNOSTIC_NEIGHBOR"


@dataclass(frozen=True, slots=True)
class ProjectionBudget:
    explicit_entry_total: int | BudgetValue
    hierarchy_node_total: int | BudgetValue
    tracking_item_total: int | BudgetValue
    relation_component_total: int | BudgetValue
    duplicate_alias_component_total: int | BudgetValue
    members_per_component: int | BudgetValue
    scope_minimum_guarantee: int | BudgetValue
    priority_quotas: tuple[tuple[str, int | BudgetValue], ...]
    serialized_bytes_soft: int | BudgetValue


@dataclass(frozen=True, slots=True)
class ProjectionPolicy:
    policy_schema_version: int
    ordering_reference: str
    budget: ProjectionBudget
    duplicate_overlay: bool = False
    relation_overlay: bool = False


@dataclass(frozen=True, slots=True)
class SnapshotDiagnosticRequest:
    primary_snapshot_id: str
    scope: str | None = None
    path_prefix: str | None = None
    hierarchy_requested: bool = True
    depth: int | None = None
    rank: str | None = None
    min_bytes: int | None = None
    duplicate_overlay: SourcePlanState = SourcePlanState.NOT_REQUESTED
    relation_context_pair: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class PairTrackingRequest:
    base_snapshot_id: str
    target_snapshot_id: str
    scope: str | None = None
    path_prefix: str | None = None
    growth: SourcePlanState = SourcePlanState.REQUESTED_AND_PRESENT
    diff: SourcePlanState = SourcePlanState.REQUESTED_AND_PRESENT
    relation: SourcePlanState = SourcePlanState.NOT_REQUESTED


@dataclass(frozen=True, slots=True)
class SnapshotSourceIdentity:
    snapshot_id: str
    schema_version: int
    snapshot_digest: str | None


@dataclass(frozen=True, slots=True)
class SnapshotPairSourceIdentity:
    base: SnapshotSourceIdentity
    target: SnapshotSourceIdentity


@dataclass(frozen=True, slots=True)
class SourceResultIdentity:
    result_kind: ResultKind
    source_identity: SnapshotSourceIdentity | SnapshotPairSourceIdentity
    algorithm: str | None = None
    algorithm_version: int | None = None
    result_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectionSourceIdentity:
    primary_snapshot: SnapshotSourceIdentity | None = None
    snapshot_pair: SnapshotPairSourceIdentity | None = None
    result_identities: tuple[SourceResultIdentity, ...] = ()


@dataclass(frozen=True, slots=True)
class ResultNamespace:
    result_kind: ResultKind
    source_identity: SnapshotSourceIdentity | SnapshotPairSourceIdentity
    source_result_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ResultLocalReference:
    namespace: ResultNamespace
    result_local_id: str


@dataclass(frozen=True, slots=True)
class ExpansionDescriptor:
    snapshot_ids: tuple[str, ...]
    result_kind: ResultKind
    namespace: ResultNamespace | None
    scope: str | None = None
    path_prefix: str | None = None
    depth: int | None = None
    rank: str | None = None
    min_bytes: int | None = None
    limit: int = 100
    offset: int = 0
    local_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticCount:
    code: str
    count: int


@dataclass(frozen=True, slots=True)
class DiagnosticBoundary:
    code: str
    result_reference: ResultLocalReference | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticState:
    source_validity: ProjectionSourceValidity
    source_identity: ProjectionSourceIdentity
    metadata_coverage: CoverageAvailability
    size_coverage: CoverageAvailability
    payload_coverage: CoverageAvailability
    conflicts: tuple[DiagnosticBoundary, ...]
    limitations: tuple[DiagnosticBoundary, ...]
    unknown_size_count: int
    metadata_failure_count: int
    access_failure_count: int
    excluded_count: int
    unreadable_count: int
    non_local_count: int
    relation_ambiguity_state: SourcePlanState
    known_logical_bytes: int | None
    known_logical_delta: int | None
    allocation_state: PhysicalKnowledgeState = PhysicalKnowledgeState.UNKNOWN
    physical_block_sharing_state: PhysicalKnowledgeState = PhysicalKnowledgeState.UNKNOWN
    reclaimable_space_state: PhysicalKnowledgeState = PhysicalKnowledgeState.UNKNOWN


@dataclass(frozen=True, slots=True)
class EntryMetadataFacts:
    observation_status: FilesystemObservationStatus
    mode: int | None
    uid: int | None
    gid: int | None
    mtime_ns: int | None
    ctime_ns: int | None
    birthtime_ns: int | None
    symlink_target_raw: str | None
    readable: bool
    writable: bool
    executable: bool
    excluded: bool
    error_code: str | None


@dataclass(frozen=True, slots=True)
class EntrySizeFacts:
    state: EntrySizeState
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class EntryPayloadFacts:
    state: PayloadFactState
    algorithm: str | None = None
    algorithm_version: int | None = None
    digest: str | None = None
    provenance: PayloadObservationProvenance | None = None
    reused_from_snapshot_id: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class EntryObjectHintFacts:
    device_id: int | None
    inode: int | None
    link_count: int | None


@dataclass(frozen=True, slots=True)
class ExplicitEntryAnchor:
    entry_reference: SnapshotEntryReference
    source_side: EntrySourceSide
    object_kind: FilesystemObjectType
    metadata_facts: EntryMetadataFacts | None
    size_facts: EntrySizeFacts
    payload_facts: EntryPayloadFacts | None
    object_hint_facts: EntryObjectHintFacts | None
    selection_reasons: tuple[SelectionReason, ...]
    result_references: tuple[ResultLocalReference, ...] = ()


@dataclass(frozen=True, slots=True)
class StructureMetrics:
    regular_file_count: int
    known_logical_bytes: int
    unknown_size_regular_count: int
    directory_count: int
    symlink_count: int
    special_object_count: int


@dataclass(frozen=True, slots=True)
class HierarchyItem:
    node_reference: ResultLocalReference
    scope_id: str
    relative_directory_path: str
    observed_directory_entry: bool
    direct_metrics: StructureMetrics
    recursive_metrics: StructureMetrics
    presentation: HierarchyPresentationState
    selection_reasons: tuple[SelectionReason, ...]
    anchor_references: tuple[SnapshotEntryReference, ...]
    boundary_references: tuple[ResultLocalReference, ...]
    expansion_descriptor: ExpansionDescriptor


@dataclass(frozen=True, slots=True)
class OverlayItem:
    kind: OverlayKind
    component_reference: ResultLocalReference
    total_member_count: int
    explicit_member_references: tuple[SnapshotEntryReference, ...]
    aggregate_member_count: int
    certainty: RelationCertainty | None
    expansion_descriptor: ExpansionDescriptor


@dataclass(frozen=True, slots=True)
class ObjectKindCount:
    object_kind: FilesystemObjectType
    count: int


@dataclass(frozen=True, slots=True)
class ChangeKindCount:
    change_kind: SnapshotDiffChangeType
    count: int


@dataclass(frozen=True, slots=True)
class Accounting:
    domain: AccountingDomain
    source_count: int
    explicit_count: int
    aggregate_accounted_count: int
    availability: CoverageAvailability = CoverageAvailability.COMPLETE
    known_source_bytes: int | None = None
    known_explicit_bytes: int | None = None
    known_aggregate_accounted_bytes: int | None = None
    unknown_size_count: int | None = None
    object_kind_counts: tuple[ObjectKindCount, ...] = ()
    change_kind_counts: tuple[ChangeKindCount, ...] = ()
    conflict_counts: tuple[DiagnosticCount, ...] = ()
    overlay_member_count: int | None = None


@dataclass(frozen=True, slots=True)
class SnapshotDiagnosticBody:
    hierarchy_items: tuple[HierarchyItem, ...]
    explicit_entry_anchors: tuple[ExplicitEntryAnchor, ...]
    duplicate_overlays: tuple[OverlayItem, ...] = ()
    hard_link_alias_overlays: tuple[OverlayItem, ...] = ()
    relation_overlays: tuple[OverlayItem, ...] = ()


@dataclass(frozen=True, slots=True)
class TrackingItem:
    scope_id: str
    relative_path: str
    base_entry_reference: SnapshotEntryReference | None
    target_entry_reference: SnapshotEntryReference | None
    diff_reference: ResultLocalReference
    growth_contribution_reference: ResultLocalReference | None
    relation_references: tuple[ResultLocalReference, ...]
    change_kind: SnapshotDiffChangeType
    content_state: ContentState
    known_size_delta: int | None
    selection_reasons: tuple[SelectionReason, ...]
    expansion_descriptor: ExpansionDescriptor


@dataclass(frozen=True, slots=True)
class PathGrowthMetrics:
    base_known_logical_bytes: int
    target_known_logical_bytes: int
    known_net_logical_delta: int
    added_logical_bytes: int
    added_location_count: int
    removed_logical_bytes: int
    removed_location_count: int
    same_location_increase_bytes: int
    same_location_increase_count: int
    same_location_decrease_bytes: int
    same_location_decrease_count: int
    same_location_unchanged_count: int
    unknown_size_contribution_count: int
    decomposition_complete: bool


@dataclass(frozen=True, slots=True)
class PairTrackingGrowthHierarchyItem:
    node_reference: ResultLocalReference
    scope_id: str
    relative_directory_path: str
    direct_metrics: PathGrowthMetrics
    recursive_metrics: PathGrowthMetrics
    presentation: HierarchyPresentationState
    selection_reasons: tuple[SelectionReason, ...]
    expansion_descriptor: ExpansionDescriptor


@dataclass(frozen=True, slots=True)
class PairTrackingGrowthHierarchyContext:
    state: SourcePlanState
    source_result_identity: SourceResultIdentity | None
    hierarchy_items: tuple[PairTrackingGrowthHierarchyItem, ...]


@dataclass(frozen=True, slots=True)
class PairTrackingBody:
    tracking_items: tuple[TrackingItem, ...]
    explicit_entry_anchors: tuple[ExplicitEntryAnchor, ...]
    growth_hierarchy: PairTrackingGrowthHierarchyContext
    relation_overlays: tuple[OverlayItem, ...] = ()


@dataclass(frozen=True, slots=True)
class SourcePlanItem:
    result_kind: ResultKind
    state: SourcePlanState
    source_identity: SourceResultIdentity | None = None


@dataclass(frozen=True, slots=True)
class ProjectionPreDigest:
    mode: ProjectionMode
    normalized_request: SnapshotDiagnosticRequest | PairTrackingRequest
    resolved_policy: ProjectionPolicy
    source_identity: ProjectionSourceIdentity
    source_plan: tuple[SourcePlanItem, ...]
    diagnostic_state: DiagnosticState
    accounting: tuple[Accounting, ...]
    expansion_descriptors: tuple[ExpansionDescriptor, ...]
    snapshot_diagnostic: SnapshotDiagnosticBody | None = None
    pair_tracking: PairTrackingBody | None = None


@dataclass(frozen=True, slots=True)
class ObservationProjection:
    facts: ProjectionPreDigest
    projection_digest: str
