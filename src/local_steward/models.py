"""Immutable models shared by commands and protocol output."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class ScopeRole(str, Enum):
    MANAGED_ROOT = "managed_root"
    REFERENCE_ROOT = "reference_root"
    EXCLUDED_ROOT = "excluded_root"


class CapabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    MISCONFIGURED = "MISCONFIGURED"
    STALE = "STALE"
    DEGRADED = "DEGRADED"


class CheckSeverity(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class OutputStatus(str, Enum):
    OK = "OK"
    ERROR = "ERROR"


class RunStatus(str, Enum):
    CREATED = "created"
    SCANNING = "scanning"
    SCANNED = "scanned"
    PLANNING = "planning"
    PLANNED = "planned"
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class FilesystemObjectType(str, Enum):
    REGULAR_FILE = "regular_file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    FIFO = "fifo"
    SOCKET = "socket"
    CHARACTER_DEVICE = "character_device"
    BLOCK_DEVICE = "block_device"
    UNKNOWN = "unknown"


class FilesystemObservationStatus(str, Enum):
    OBSERVED = "observed"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    CHANGED_DURING_SCAN = "changed_during_scan"
    IO_ERROR = "io_error"
    UNSUPPORTED = "unsupported"


class PayloadObservationStatus(str, Enum):
    HASHED = "HASHED"
    EMPTY_FILE_HASHED = "EMPTY_FILE_HASHED"
    NOT_REGULAR_FILE = "NOT_REGULAR_FILE"
    NOT_LOCAL = "NOT_LOCAL"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOTAL_BYTE_BUDGET_EXHAUSTED = "TOTAL_BYTE_BUDGET_EXHAUSTED"
    TIME_BUDGET_EXHAUSTED = "TIME_BUDGET_EXHAUSTED"
    CHANGED_DURING_READ = "CHANGED_DURING_READ"
    NOT_FOUND_DURING_READ = "NOT_FOUND_DURING_READ"
    IO_ERROR = "IO_ERROR"
    UNSUPPORTED = "UNSUPPORTED"


class PayloadObservationProvenance(str, Enum):
    DIRECT_READ = "DIRECT_READ"
    REUSED_FROM_VERIFIED_SNAPSHOT = "REUSED_FROM_VERIFIED_SNAPSHOT"


class SnapshotStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class SnapshotConsistency(str, Enum):
    BEST_EFFORT_POINT_IN_TIME = "best_effort_point_in_time"


class SnapshotStorageIntegrityStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"


class SnapshotReplayStatus(str, Enum):
    READY = "READY"
    FAILED = "FAILED"


class EvidenceIntegrityStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class SemanticConsistencyStatus(str, Enum):
    CONSISTENT = "CONSISTENT"
    INCONSISTENT = "INCONSISTENT"
    UNKNOWN = "UNKNOWN"


class ReplayEligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"


class SnapshotReplacementStatus(str, Enum):
    READY = "READY"
    REPLACED = "REPLACED"
    FAILED = "FAILED"


class SnapshotBackupStatus(str, Enum):
    READY = "READY"
    FAILED = "FAILED"


class SnapshotRollbackStatus(str, Enum):
    READY = "READY"
    RESTORED = "RESTORED"
    FAILED = "FAILED"


class SnapshotDiffChangeType(str, Enum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"
    UNCHANGED = "UNCHANGED"


class ChangeEventType(str, Enum):
    FILE_CREATED = "FILE_CREATED"
    FILE_DELETED = "FILE_DELETED"
    FILE_MODIFIED = "FILE_MODIFIED"


class RelationCertainty(str, Enum):
    FACT = "FACT"
    CANDIDATE = "CANDIDATE"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"


class RelationKind(str, Enum):
    SAME_LOCATION_CONTINUITY = "SAME_LOCATION_CONTINUITY"
    SAME_LOCATION_METADATA_CHANGED = "SAME_LOCATION_METADATA_CHANGED"
    SAME_LOCATION_CONTENT_CHANGED = "SAME_LOCATION_CONTENT_CHANGED"
    SAME_LOCATION_CONTENT_UNKNOWN = "SAME_LOCATION_CONTENT_UNKNOWN"
    SAME_LOCATION_OBJECT_HINT_CHANGED = "SAME_LOCATION_OBJECT_HINT_CHANGED"
    SAME_LOCATION_TYPE_CHANGED = "SAME_LOCATION_TYPE_CHANGED"
    SAME_LOCATION_SYMLINK_TARGET_CHANGED = "SAME_LOCATION_SYMLINK_TARGET_CHANGED"
    RENAME_CANDIDATE = "RENAME_CANDIDATE"
    CROSS_SCOPE_TRANSITION_CANDIDATE = "CROSS_SCOPE_TRANSITION_CANDIDATE"
    AMBIGUOUS_LOCATION_TRANSITION = "AMBIGUOUS_LOCATION_TRANSITION"


class DuplicateStorageKnowledgeStatus(str, Enum):
    """The only physical-storage conclusion available in duplicate analysis v0.1."""

    UNKNOWN = "UNKNOWN"


class ResourceProcessSort(str, Enum):
    CPU = "cpu"
    MEMORY = "memory"


@dataclass(frozen=True, slots=True)
class FaultInjectionReport:
    scenario: str
    injected_failure: str
    operation: str
    result: str
    official_before_digest: str
    official_after_digest: str
    backup_digest: str
    rollback_digest: str
    issues: tuple[dict[str, str], ...]


class DiffStatus(str, Enum):
    COMPLETE = "complete"


class FilesystemChangeType(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    TYPE_CHANGED = "type_changed"
    METADATA_CHANGED = "metadata_changed"
    OBSERVATION_CHANGED = "observation_changed"


@dataclass(frozen=True, slots=True)
class PathConfig:
    data_dir: Path
    cache_dir: Path
    evidence_dir: Path
    quarantine_dir: Path


@dataclass(frozen=True, slots=True)
class ScopeConfig:
    scope_id: str
    role: ScopeRole
    raw_path: str
    normalized_path: Path
    enabled: bool
    follow_directory_symlinks: bool
    allow_cross_mount: bool


NormalizedScope = ScopeConfig


@dataclass(frozen=True, slots=True)
class StewardConfig:
    schema_version: int
    project_name: str
    paths: PathConfig
    scopes: tuple[ScopeConfig, ...]
    project_root: Path
    source_path: Path
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    check_id: str
    category: str
    required: bool
    status: CapabilityStatus
    message: str
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DoctorSummary:
    status: CapabilityStatus
    checks: tuple[DoctorCheck, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    schema_version: int
    command: str
    status: str
    run_id: str
    result: dict[str, Any]
    errors: list[dict[str, str]]
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    run_kind: str
    status: RunStatus
    created_at: str
    updated_at: str
    config_digest: str
    metadata: dict[str, Any]
    last_sequence: int
    last_evidence_digest: str | None
    terminal: bool


@dataclass(frozen=True, slots=True)
class RunTransition:
    run_id: str
    from_status: RunStatus
    to_status: RunStatus
    reason: str


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    run_id: str
    sequence: int
    evidence_type: str
    created_at: str
    relative_path: str
    previous_evidence_digest: str | None
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class EvidenceVerificationResult:
    run_id: str | None
    status: str
    ledger_valid: bool
    index_consistent: bool
    errors: tuple[str, ...]
    evidence_count: int


@dataclass(frozen=True, slots=True)
class SnapshotEvidenceVerificationItem:
    evidence_id: str | None
    snapshot_id: str | None
    persistent_run_id: str | None
    evidence_type: str
    schema_valid: bool
    digest_valid: bool
    payload_valid: bool
    snapshot_valid: bool
    run_present: bool
    run_kind_valid: bool
    run_status_valid: bool
    valid: bool
    evidence_relative_path: str | None
    errors: tuple[dict[str, str], ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotEvidenceVerificationReport:
    evidence_count: int
    valid_count: int
    invalid_count: int
    duplicate_snapshot_id_count: int
    duplicate_run_count: int
    run_missing_count: int
    run_invalid_count: int
    items: tuple[SnapshotEvidenceVerificationItem, ...]
    issues: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class EvidenceVerificationReport:
    verifications: tuple[EvidenceVerificationResult, ...]
    snapshot_evidence: SnapshotEvidenceVerificationReport


@dataclass(frozen=True, slots=True)
class StorageStatus:
    storage_status: str
    database_exists: bool
    schema_valid: bool
    run_count: int
    evidence_count: int
    ledger_run_count: int
    ledger_evidence_count: int
    orphaned_evidence_count: int
    missing_indexed_files_count: int
    temporary_files_count: int
    errors: tuple[str, ...]
    snapshot_integrity: "SnapshotStorageIntegrityReport | None" = None
    issues: tuple[dict[str, str], ...] = ()
    historical_evidence_diagnostics: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ScanBudget:
    max_entries: int = 1_000_000
    max_total_stat_bytes: int | None = None
    max_duration_seconds: float = 600.0
    max_depth: int | None = None


@dataclass(frozen=True, slots=True)
class FilesystemEntry:
    entry_id: str
    snapshot_id: str
    scope_id: str
    relative_path: str
    object_type: FilesystemObjectType
    device_id: int | None
    inode: int | None
    mode: int | None
    uid: int | None
    gid: int | None
    size_bytes: int | None
    mtime_ns: int | None
    ctime_ns: int | None
    birthtime_ns: int | None
    link_count: int | None
    symlink_target_raw: str | None
    readable: bool
    writable: bool
    executable: bool
    observation_status: FilesystemObservationStatus
    error_code: str | None
    error_message: str | None
    excluded: bool = False


@dataclass(frozen=True, slots=True)
class FilesystemSnapshotSummary:
    snapshot_id: str
    run_id: str
    status: SnapshotStatus
    created_at: str
    scope_ids: tuple[str, ...]
    entry_count: int
    observed_count: int
    error_count: int
    snapshot_digest: str


@dataclass(frozen=True, slots=True)
class FilesystemSnapshot:
    snapshot_id: str
    run_id: str
    created_at: str
    started_at: str
    completed_at: str
    status: SnapshotStatus
    consistency: SnapshotConsistency
    config_digest: str
    scope_ids: tuple[str, ...]
    budget: ScanBudget
    entry_count: int
    observed_count: int
    error_count: int
    excluded_count: int
    total_regular_file_bytes: int
    max_depth_observed: int
    entries_digest: str
    snapshot_digest: str
    evidence_id: str | None
    evidence_relative_path: str | None
    entries: tuple[FilesystemEntry, ...]


@dataclass(frozen=True, slots=True)
class PayloadObservation:
    status: PayloadObservationStatus
    algorithm: str | None
    algorithm_version: int | None
    digest: str | None
    bytes_hashed: int | None
    provenance: PayloadObservationProvenance | None
    reused_from_snapshot_id: str | None
    failure_code: str | None
    os_error_code: int | None


@dataclass(frozen=True, slots=True)
class PayloadHashPolicy:
    algorithm: str
    algorithm_version: int
    max_hash_file_bytes: int | None
    max_total_hash_bytes: int | None
    max_hash_duration_seconds: float | int | None
    hash_chunk_size: int
    allow_non_local_content: bool
    allow_verified_reuse: bool


@dataclass(frozen=True, slots=True)
class PayloadObservationCount:
    status: PayloadObservationStatus
    count: int


@dataclass(frozen=True, slots=True)
class FilesystemEntryV2:
    entry_id: str
    snapshot_id: str
    scope_id: str
    relative_path: str
    object_type: FilesystemObjectType
    device_id: int | None
    inode: int | None
    mode: int | None
    uid: int | None
    gid: int | None
    size_bytes: int | None
    mtime_ns: int | None
    ctime_ns: int | None
    birthtime_ns: int | None
    link_count: int | None
    symlink_target_raw: str | None
    readable: bool
    writable: bool
    executable: bool
    observation_status: FilesystemObservationStatus
    error_code: str | None
    error_message: str | None
    excluded: bool
    allocated_size_bytes: int | None
    payload_observation: PayloadObservation


@dataclass(frozen=True, slots=True)
class FilesystemSnapshotV2:
    snapshot_schema_version: int
    snapshot_id: str
    run_id: str
    created_at: str
    started_at: str
    completed_at: str
    status: SnapshotStatus
    consistency: SnapshotConsistency
    config_digest: str
    scope_ids: tuple[str, ...]
    budget: ScanBudget
    entry_count: int
    observed_count: int
    error_count: int
    excluded_count: int
    total_regular_file_bytes: int
    max_depth_observed: int
    hash_policy: PayloadHashPolicy
    allocated_regular_file_bytes_known_sum: int
    allocated_regular_file_unknown_count: int
    payload_observation_summary: tuple[PayloadObservationCount, ...]
    entries_digest: str
    snapshot_digest: str
    evidence_id: str | None
    evidence_relative_path: str | None
    entries: tuple[FilesystemEntryV2, ...]


@dataclass(frozen=True, slots=True)
class SnapshotEntryPage:
    snapshot_id: str
    entries: tuple[FilesystemEntry | FilesystemEntryV2, ...]
    returned_count: int
    limit: int
    offset: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class SnapshotEvidenceValidationResult:
    evidence_id: str | None
    snapshot_id: str | None
    valid: bool
    envelope_valid: bool
    evidence_digest_valid: bool
    evidence_type_valid: bool
    payload_schema_valid: bool
    run_id_consistent: bool
    snapshot_schema_valid: bool
    entries_schema_valid: bool
    entry_order_valid: bool
    entry_keys_unique: bool
    entry_ids_valid: bool
    scope_membership_valid: bool
    paths_valid: bool
    summary_valid: bool
    entries_digest_valid: bool
    snapshot_digest_valid: bool
    errors: tuple[dict[str, str], ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotInventoryItem:
    snapshot_id: str | None
    evidence_id: str | None
    persistent_run_id: str | None
    evidence_present: bool
    index_present: bool
    run_present: bool
    indexed_entry_count: int
    evidence_relative_path: str | None
    issue_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SnapshotInventory:
    snapshot_evidence_records: int
    indexed_snapshots: int
    indexed_entry_groups: int
    runs: int
    items: tuple[SnapshotInventoryItem, ...]
    issues: tuple[dict[str, str], ...]
    indexed_entry_count: int = 0


@dataclass(frozen=True, slots=True)
class SnapshotStorageIntegrityItem:
    snapshot_id: str | None
    evidence_id: str | None
    persistent_run_id: str | None
    status: SnapshotStorageIntegrityStatus
    evidence_present: bool
    index_present: bool
    run_present: bool
    indexed_entry_count: int
    issue_codes: tuple[str, ...]
    evidence_relative_path: str | None


@dataclass(frozen=True, slots=True)
class SnapshotStorageIntegrityReport:
    status: SnapshotStorageIntegrityStatus
    snapshot_evidence_count: int
    indexed_snapshot_count: int
    indexed_entry_group_count: int
    indexed_entry_count: int
    run_count: int
    healthy_snapshot_count: int
    degraded_snapshot_count: int
    invalid_snapshot_count: int
    orphan_evidence_count: int
    missing_evidence_count: int
    duplicate_snapshot_id_count: int
    duplicate_run_snapshot_count: int
    duplicate_evidence_index_count: int
    orphan_entry_count: int
    cross_reference_entry_count: int
    items: tuple[SnapshotStorageIntegrityItem, ...]
    issues: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SnapshotReplayItem:
    snapshot_id: str | None
    evidence_id: str | None
    persistent_run_id: str | None
    replayable: bool
    replayed: bool
    entry_count: int
    issue_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunEvidenceAccountingItem:
    run_id: str
    evidence_count: int
    evidence_integrity: EvidenceIntegrityStatus
    run_kind: str | None
    final_status: str | None
    last_evidence_digest: str | None
    evidence_ids: tuple[str, ...]
    evidence_digests: tuple[str, ...]
    issue_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClassifiedSnapshotEntryAccountingItem:
    entry_id: str
    snapshot_id: str
    scope_id: str
    relative_path: str
    evidence_id: str | None
    evidence_relative_path: str | None
    evidence_digest: str | None
    replay_eligibility: ReplayEligibility | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClassifiedSnapshotReplayItem:
    snapshot_id: str | None
    evidence_id: str | None
    persistent_run_id: str | None
    evidence_relative_path: str | None
    evidence_digest: str | None
    entry_count: int
    evidence_integrity: EvidenceIntegrityStatus
    semantic_consistency: SemanticConsistencyStatus
    replay_eligibility: ReplayEligibility | None
    replayed: bool
    reason_codes: tuple[str, ...]
    entries: tuple[ClassifiedSnapshotEntryAccountingItem, ...]


@dataclass(frozen=True, slots=True)
class ClassifiedReplayPlan:
    classification_complete: bool
    total_run_count: int
    total_evidence_count: int
    total_snapshot_count: int
    total_snapshot_entry_count: int
    eligible_snapshot_count: int
    eligible_entry_count: int
    ineligible_snapshot_count: int
    ineligible_entry_count: int
    non_snapshot_run_count: int
    non_snapshot_evidence_count: int
    accounting_digest: str
    runs: tuple[RunEvidenceAccountingItem, ...]
    snapshots: tuple[ClassifiedSnapshotReplayItem, ...]
    issues: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class ClassifiedOperationalReplayReport:
    status: SnapshotReplayStatus
    candidate_ready: bool
    operational_storage_status: str
    historical_diagnostics_present: bool
    destination_database: str
    destination_schema_version: int
    integrity_check: str
    foreign_key_check: str
    expected_run_count: int
    actual_run_count: int
    expected_evidence_count: int
    actual_evidence_count: int
    expected_snapshot_count: int
    actual_snapshot_count: int
    expected_entry_count: int
    actual_entry_count: int
    excluded_snapshot_count: int
    excluded_entry_count: int
    source_snapshot_digest: str
    destination_snapshot_digest: str
    accounting_digest: str
    plan: ClassifiedReplayPlan
    issues: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]

    @property
    def replacement_ready(self) -> bool:
        """Compatibility adapter for the shared guarded replacement gate."""
        return self.candidate_ready

    @property
    def replayed_snapshot_count(self) -> int:
        return self.actual_snapshot_count

    @property
    def replayed_entry_count(self) -> int:
        return self.actual_entry_count


@dataclass(frozen=True, slots=True)
class SnapshotReplayReport:
    status: SnapshotReplayStatus
    replacement_ready: bool
    destination_database: str
    snapshot_evidence_count: int
    replayable_evidence_count: int
    rejected_evidence_count: int
    replayed_snapshot_count: int
    replayed_entry_count: int
    source_snapshot_digest: str
    destination_snapshot_digest: str
    destination_schema_version: int
    items: tuple[SnapshotReplayItem, ...]
    issues: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SnapshotReplacementReport:
    status: SnapshotReplacementStatus
    candidate_database: str
    official_database: str
    replacement_ready: bool
    old_database_digest: str
    new_database_digest: str
    snapshot_count: int
    entry_count: int
    schema_version: int
    validation_result: tuple[dict[str, str], ...]
    issues: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class SnapshotBackupManifest:
    source_database_digest: str
    source_schema_version: int
    source_snapshot_digest: str
    source_entry_digest: str
    source_snapshot_evidence_schema_versions: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotBackupReport:
    status: SnapshotBackupStatus
    official_database: str
    backup_database: str
    schema_version: int
    source_digest: str
    backup_digest: str
    snapshot_count: int
    entry_count: int
    integrity_check: str
    manifest: SnapshotBackupManifest | None
    issues: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SnapshotRollbackReport:
    status: SnapshotRollbackStatus
    backup_database: str
    official_database: str
    backup_digest: str
    restored_digest: str
    snapshot_count: int
    entry_count: int
    validation_result: tuple[dict[str, str], ...]
    issues: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SnapshotVerificationResult:
    snapshot_id: str
    status: str
    evidence_id: str | None
    persistent_run_id: str | None
    evidence_present: bool
    evidence_valid: bool
    index_present: bool
    index_consistent: bool
    run_present: bool
    run_consistent: bool
    snapshot_row_consistent: bool
    entry_count_consistent: bool
    entry_content_consistent: bool
    entry_order_consistent: bool
    errors: tuple[dict[str, str], ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotDiffItem:
    scope_id: str
    relative_path: str
    change_type: SnapshotDiffChangeType
    changed_fields: tuple[str, ...]
    left_entry: FilesystemEntry | None
    right_entry: FilesystemEntry | None


@dataclass(frozen=True, slots=True)
class SnapshotDiffSummary:
    added_count: int
    removed_count: int
    modified_count: int
    unchanged_count: int
    item_count: int


@dataclass(frozen=True, slots=True)
class SnapshotDiff:
    left_snapshot_id: str
    right_snapshot_id: str
    items: tuple[SnapshotDiffItem, ...]
    summary: SnapshotDiffSummary


@dataclass(frozen=True, slots=True)
class SnapshotEntryReference:
    snapshot_id: str
    scope_id: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class RelationAmbiguityGroup:
    ambiguity_group_id: str
    source_entries: tuple[SnapshotEntryReference, ...]
    target_entries: tuple[SnapshotEntryReference, ...]


@dataclass(frozen=True, slots=True)
class RelationItem:
    relation_id: str
    kind: RelationKind
    certainty: RelationCertainty
    reason_codes: tuple[str, ...]
    ambiguity_group_id: str | None
    source_entries: tuple[SnapshotEntryReference, ...]
    target_entries: tuple[SnapshotEntryReference, ...]


@dataclass(frozen=True, slots=True)
class RelationSet:
    relation_schema_version: int
    algorithm: str
    algorithm_version: int
    base_snapshot_id: str
    target_snapshot_id: str
    relations: tuple[RelationItem, ...]
    ambiguity_groups: tuple[RelationAmbiguityGroup, ...]
    relation_set_digest: str


@dataclass(frozen=True, slots=True)
class RelationQueryResult:
    relation_schema_version: int
    algorithm: str
    algorithm_version: int
    base_snapshot_id: str
    target_snapshot_id: str
    relation_set_digest: str
    relation_item_count: int
    filtered_relation_item_count: int
    returned_relation_item_count: int
    kind_filter: RelationKind | None
    relation_items: tuple[RelationItem, ...]
    ambiguity_groups: tuple[RelationAmbiguityGroup, ...]
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class PayloadUnknownReasonCount:
    code: str
    count: int


@dataclass(frozen=True, slots=True)
class HardLinkAliasSet:
    alias_set_id: str
    device_id: int
    inode: int
    member_entries: tuple[SnapshotEntryReference, ...]


@dataclass(frozen=True, slots=True)
class StorageUnit:
    """An ephemeral current-Snapshot storage accounting unit, never an identity."""

    device_id: int | None
    inode: int | None
    member_entries: tuple[SnapshotEntryReference, ...]
    membership_known: bool
    integrity_conflicted: bool
    logical_size_bytes: int | None
    allocated_size_bytes: int | None


@dataclass(frozen=True, slots=True)
class PayloadEqualityGroup:
    payload_group_id: str
    algorithm: str
    algorithm_version: int
    digest: str
    logical_size_bytes: int
    member_entries: tuple[SnapshotEntryReference, ...]
    alias_set_ids: tuple[str, ...]
    storage_units: tuple[StorageUnit, ...]
    known_storage_unit_count: int
    unknown_storage_unit_count: int
    is_exact_duplicate: bool
    path_logical_bytes: int
    known_unit_logical_bytes: int
    logical_redundant_bytes: int | None


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    total_entry_count: int
    total_regular_entry_count: int
    payload_analyzable_regular_entry_count: int
    payload_unknown_regular_entry_count: int
    analyzable_logical_bytes: int
    unknown_logical_bytes: int
    payload_status_counts: tuple[PayloadUnknownReasonCount, ...]
    payload_unknown_reason_counts: tuple[PayloadUnknownReasonCount, ...]
    alias_path_count: int
    known_storage_unit_count: int
    unknown_storage_unit_membership_count: int


@dataclass(frozen=True, slots=True)
class PhysicalStorageSummary:
    allocated_size_bytes_known_sum: int
    allocated_size_known_unit_count: int
    allocated_size_unknown_unit_count: int
    allocation_status: DuplicateStorageKnowledgeStatus
    physical_block_sharing_status: DuplicateStorageKnowledgeStatus
    reclaimable_bytes: None
    reclaimable_status: DuplicateStorageKnowledgeStatus


@dataclass(frozen=True, slots=True)
class IntegrityConflict:
    code: str
    entries: tuple[SnapshotEntryReference, ...]


@dataclass(frozen=True, slots=True)
class DuplicateAnalysisResult:
    analysis_schema_version: int
    algorithm: str
    algorithm_version: int
    snapshot_id: str
    payload_equality_groups: tuple[PayloadEqualityGroup, ...]
    hard_link_alias_sets: tuple[HardLinkAliasSet, ...]
    coverage: CoverageSummary
    physical_storage: PhysicalStorageSummary
    integrity_conflicts: tuple[IntegrityConflict, ...]
    analysis_digest: str


@dataclass(frozen=True, slots=True)
class DuplicateAnalysisQueryResult:
    analysis_schema_version: int
    algorithm: str
    algorithm_version: int
    snapshot_id: str
    analysis_digest: str
    payload_equality_group_count: int
    filtered_payload_equality_group_count: int
    returned_payload_equality_group_count: int
    only_exact: bool
    payload_equality_groups: tuple[PayloadEqualityGroup, ...]
    hard_link_alias_sets: tuple[HardLinkAliasSet, ...]
    coverage: CoverageSummary
    physical_storage: PhysicalStorageSummary
    integrity_conflicts: tuple[IntegrityConflict, ...]
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class PathAggregateNode:
    path_node_id: str
    snapshot_id: str
    scope_id: str
    relative_directory_path: str
    observed_directory_entry: bool
    direct_regular_file_count: int
    recursive_regular_file_count: int
    direct_known_logical_bytes: int
    recursive_known_logical_bytes: int
    direct_unknown_size_regular_count: int
    recursive_unknown_size_regular_count: int
    direct_directory_count: int
    recursive_directory_count: int
    direct_symlink_count: int
    recursive_symlink_count: int
    direct_special_object_count: int
    recursive_special_object_count: int


@dataclass(frozen=True, slots=True)
class ScopeStructureSummary:
    snapshot_id: str
    scope_id: str
    root_node_id: str
    recursive_regular_file_count: int
    recursive_known_logical_bytes: int
    recursive_unknown_size_regular_count: int
    recursive_directory_count: int
    recursive_symlink_count: int
    recursive_special_object_count: int


@dataclass(frozen=True, slots=True)
class StructureCoverageSummary:
    total_entry_count: int
    regular_file_entry_count: int
    known_size_regular_file_count: int
    unknown_size_regular_file_count: int
    known_logical_bytes: int
    directory_entry_count: int
    symlink_entry_count: int
    special_object_entry_count: int
    excluded_entry_count: int
    metadata_failed_entry_count: int
    scope_overlap_object_hint_count: int
    repeated_known_object_hint_path_count: int
    object_hint_unavailable_entry_count: int
    complete: bool


@dataclass(frozen=True, slots=True)
class StructureLimitation:
    code: str
    entries: tuple[SnapshotEntryReference, ...]


@dataclass(frozen=True, slots=True)
class StructurePhysicalBoundary:
    allocation_status: DuplicateStorageKnowledgeStatus
    physical_block_sharing_status: DuplicateStorageKnowledgeStatus
    reclaimable_bytes: None
    reclaimable_status: DuplicateStorageKnowledgeStatus
    object_aware_capacity_status: DuplicateStorageKnowledgeStatus


@dataclass(frozen=True, slots=True)
class StorageStructureResult:
    structure_schema_version: int
    algorithm: str
    algorithm_version: int
    snapshot_id: str
    scope_summaries: tuple[ScopeStructureSummary, ...]
    path_nodes: tuple[PathAggregateNode, ...]
    coverage: StructureCoverageSummary
    limitations: tuple[StructureLimitation, ...]
    physical_boundary: StructurePhysicalBoundary
    structure_digest: str


class GrowthContributionKind(str, Enum):
    """The exhaustive Path View regular-location growth taxonomy."""

    ADDED_LOCATION = "ADDED_LOCATION"
    REMOVED_LOCATION = "REMOVED_LOCATION"
    SAME_LOCATION_SIZE_INCREASE = "SAME_LOCATION_SIZE_INCREASE"
    SAME_LOCATION_SIZE_DECREASE = "SAME_LOCATION_SIZE_DECREASE"
    SAME_LOCATION_SIZE_UNCHANGED = "SAME_LOCATION_SIZE_UNCHANGED"
    SIZE_UNKNOWN = "SIZE_UNKNOWN"


@dataclass(frozen=True, slots=True)
class GrowthContribution:
    growth_contribution_id: str
    kind: GrowthContributionKind
    entry_references: tuple[SnapshotEntryReference, ...]
    known_byte_delta: int | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PathGrowthNode:
    growth_node_id: str
    base_snapshot_id: str
    target_snapshot_id: str
    scope_id: str
    relative_directory_path: str
    direct_base_known_logical_bytes: int
    recursive_base_known_logical_bytes: int
    direct_target_known_logical_bytes: int
    recursive_target_known_logical_bytes: int
    direct_known_net_logical_delta: int
    recursive_known_net_logical_delta: int
    direct_added_logical_bytes: int
    recursive_added_logical_bytes: int
    direct_added_location_count: int
    recursive_added_location_count: int
    direct_removed_logical_bytes: int
    recursive_removed_logical_bytes: int
    direct_removed_location_count: int
    recursive_removed_location_count: int
    direct_same_location_increase_bytes: int
    recursive_same_location_increase_bytes: int
    direct_same_location_increase_count: int
    recursive_same_location_increase_count: int
    direct_same_location_decrease_bytes: int
    recursive_same_location_decrease_bytes: int
    direct_same_location_decrease_count: int
    recursive_same_location_decrease_count: int
    direct_same_location_unchanged_count: int
    recursive_same_location_unchanged_count: int
    direct_unknown_size_contribution_count: int
    recursive_unknown_size_contribution_count: int
    decomposition_complete: bool


@dataclass(frozen=True, slots=True)
class ScopeGrowthSummary:
    base_snapshot_id: str
    target_snapshot_id: str
    scope_id: str
    root_node_id: str
    base_recursive_known_logical_bytes: int
    target_recursive_known_logical_bytes: int
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
class GrowthCoverageSummary:
    base_total_entry_count: int
    target_total_entry_count: int
    base_known_size_regular_file_count: int
    target_known_size_regular_file_count: int
    base_unknown_size_regular_file_count: int
    target_unknown_size_regular_file_count: int
    co_present_comparable_regular_location_count: int
    added_known_size_location_count: int
    added_unknown_size_location_count: int
    removed_known_size_location_count: int
    removed_unknown_size_location_count: int
    same_location_known_size_comparable_count: int
    same_location_unknown_size_count: int
    unknown_size_contribution_count: int
    base_scope_overlap_object_hint_count: int
    target_scope_overlap_object_hint_count: int
    known_net_logical_delta: int
    decomposition_complete: bool


@dataclass(frozen=True, slots=True)
class StorageGrowthResult:
    growth_schema_version: int
    algorithm: str
    algorithm_version: int
    base_snapshot_id: str
    target_snapshot_id: str
    scope_summaries: tuple[ScopeGrowthSummary, ...]
    path_nodes: tuple[PathGrowthNode, ...]
    contributions: tuple[GrowthContribution, ...]
    coverage: GrowthCoverageSummary
    physical_boundary: StructurePhysicalBoundary
    growth_digest: str


class StructureRank(str, Enum):
    RECURSIVE_LOGICAL_BYTES = "recursive-logical-bytes"


class GrowthRank(str, Enum):
    NET_GROWTH = "net-growth"
    NET_SHRINK = "net-shrink"
    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class PathViewRoot:
    scope_id: str
    relative_directory_path: str


@dataclass(frozen=True, slots=True)
class StructureQueryResult:
    structure_schema_version: int
    algorithm: str
    algorithm_version: int
    snapshot_id: str
    structure_digest: str
    full_path_node_count: int
    selected_path_node_count: int
    returned_path_node_count: int
    scope_filter: str | None
    path_prefix_filter: str | None
    depth: int | None
    rank: StructureRank | None
    min_bytes: int | None
    effective_view_roots: tuple[PathViewRoot, ...]
    path_nodes: tuple[PathAggregateNode, ...]
    scope_summaries: tuple[ScopeStructureSummary, ...]
    coverage: StructureCoverageSummary
    limitations: tuple[StructureLimitation, ...]
    physical_boundary: StructurePhysicalBoundary
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class GrowthQueryResult:
    growth_schema_version: int
    algorithm: str
    algorithm_version: int
    base_snapshot_id: str
    target_snapshot_id: str
    growth_digest: str
    full_path_node_count: int
    selected_path_node_count: int
    returned_path_node_count: int
    scope_filter: str | None
    path_prefix_filter: str | None
    depth: int | None
    rank: GrowthRank | None
    min_bytes: int | None
    effective_view_roots: tuple[PathViewRoot, ...]
    path_nodes: tuple[PathGrowthNode, ...]
    scope_summaries: tuple[ScopeGrowthSummary, ...]
    contributions: tuple[GrowthContribution, ...]
    coverage: GrowthCoverageSummary
    physical_boundary: StructurePhysicalBoundary
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    scope_id: str
    relative_path: str
    event_type: ChangeEventType
    left_entry: FilesystemEntry | None
    right_entry: FilesystemEntry | None
    size_delta: int | None
    hash_changed: bool | None
    metadata_changed: bool


@dataclass(frozen=True, slots=True)
class ChangeEventSummary:
    created_count: int
    deleted_count: int
    modified_count: int
    event_count: int


@dataclass(frozen=True, slots=True)
class CpuObservation:
    logical_cpu_count: int
    physical_cpu_count: int | None
    total_percent: float
    user_percent: float
    system_percent: float
    idle_percent: float
    per_cpu_percent: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class MemoryObservation:
    total_bytes: int
    available_bytes: int
    used_bytes: int
    percent: float
    active_bytes: int | None
    inactive_bytes: int | None
    wired_bytes: int | None
    swap_total_bytes: int | None
    swap_used_bytes: int | None
    swap_free_bytes: int | None
    swap_percent: float | None
    swap_in_delta: int | None
    swap_out_delta: int | None


@dataclass(frozen=True, slots=True)
class DiskObservation:
    mount_path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float
    read_bytes_delta: int | None
    write_bytes_delta: int | None


@dataclass(frozen=True, slots=True)
class NetworkObservation:
    bytes_sent_delta: int | None
    bytes_received_delta: int | None


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    pid: int
    name: str
    cpu_percent: float
    rss_bytes: int
    memory_percent: float
    thread_count: int
    status: str


@dataclass(frozen=True, slots=True)
class ProcessObservationSummary:
    examined_count: int
    returned_count: int
    unavailable_count: int
    sort: ResourceProcessSort
    top_limit: int


@dataclass(frozen=True, slots=True)
class ResourceObservation:
    sample_seconds: float
    cpu: CpuObservation
    memory: MemoryObservation
    disk: DiskObservation
    network: NetworkObservation
    processes: tuple[ProcessObservation, ...]
    process_summary: ProcessObservationSummary
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SystemStatusEvidenceHealth:
    status: str
    verifications: tuple[EvidenceVerificationResult, ...]
    snapshot_evidence: SnapshotEvidenceVerificationReport


@dataclass(frozen=True, slots=True)
class SystemStatusRecentChanges:
    status: str
    left_snapshot_id: str | None
    right_snapshot_id: str | None
    snapshot_diff_summary: SnapshotDiffSummary | None
    change_events: tuple[ChangeEvent, ...]
    change_event_summary: ChangeEventSummary | None
    limitation: str | None


@dataclass(frozen=True, slots=True)
class SystemStatusReview:
    resources: ResourceObservation
    evidence_health: SystemStatusEvidenceHealth
    storage_health: StorageStatus
    recent_changes: SystemStatusRecentChanges
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FilesystemEntryChange:
    scope_id: str
    relative_path: str
    change_type: FilesystemChangeType
    changed_fields: tuple[str, ...]
    before_entry: dict[str, Any] | None
    after_entry: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class FilesystemSnapshotDiff:
    diff_id: str
    run_id: str
    base_snapshot_id: str
    target_snapshot_id: str
    created_at: str
    status: DiffStatus
    changes_digest: str
    diff_digest: str
    changes: tuple[FilesystemEntryChange, ...]
