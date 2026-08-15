"""Stable, user-safe project exceptions."""


class StewardError(Exception):
    code = "INTERNAL_ERROR"
    exit_code = 8


class ConfigurationError(StewardError):
    code = "CONFIG_INVALID"
    exit_code = 2


class ConfigurationNotFoundError(ConfigurationError):
    code = "CONFIG_NOT_FOUND"


class ConfigurationSchemaError(ConfigurationError):
    code = "CONFIG_INVALID"


class ScopeValidationError(ConfigurationError):
    code = "SCOPE_INVALID"


class InitializationError(StewardError):
    code = "INIT_CONFLICT"
    exit_code = 2


class DoctorError(StewardError):
    code = "DOCTOR_REQUIRED_CAPABILITY_MISSING"
    exit_code = 3


class OutputSerializationError(StewardError):
    code = "OUTPUT_SERIALIZATION_ERROR"


class StorageError(StewardError):
    code = "STORAGE_ERROR"
    exit_code = 3


class StorageNotInitializedError(StorageError):
    code = "STORAGE_NOT_INITIALIZED"


class StorageSchemaError(StorageError):
    code = "STORAGE_SCHEMA_INVALID"


class StorageSchemaTooNewError(StorageSchemaError):
    code = "STORAGE_SCHEMA_TOO_NEW"


class StorageMigrationRequiredError(StorageSchemaError):
    code = "STORAGE_MIGRATION_REQUIRED"


class StorageCorruptionError(StorageError):
    code = "STORAGE_CORRUPT"


class StorageConflictError(StorageError):
    code = "STORAGE_CONFLICT"


class StorageBusyError(StorageError):
    code = "STORAGE_BUSY"
    exit_code = 8


class EvidenceError(StewardError):
    code = "EVIDENCE_INVALID"
    exit_code = 7


class EvidenceConflictError(EvidenceError):
    code = "EVIDENCE_CONFLICT"


class RunNotFoundError(StewardError):
    code = "RUN_NOT_FOUND"
    exit_code = 2


class RunKindError(StewardError):
    code = "RUN_KIND_INVALID"
    exit_code = 2


class InvalidRunTransitionError(StewardError):
    code = "RUN_TRANSITION_INVALID"
    exit_code = 2


class RebuildConfirmationError(StewardError):
    code = "REBUILD_CONFIRMATION_REQUIRED"
    exit_code = 2


class StorageMigrationConfirmationError(StorageError):
    code = "STORAGE_MIGRATION_CONFIRMATION_REQUIRED"
    exit_code = 2


class StorageMigrationError(StorageError):
    code = "STORAGE_MIGRATION_FAILED"


class SnapshotError(StewardError):
    code = "SNAPSHOT_INVALID"
    exit_code = 2


class SnapshotScopeError(SnapshotError):
    code = "SNAPSHOT_SCOPE_INVALID"


class SnapshotBudgetError(SnapshotError):
    code = "SNAPSHOT_BUDGET_INVALID"


class SnapshotNotFoundError(SnapshotError):
    code = "SNAPSHOT_NOT_FOUND"


class SnapshotAcquisitionConfirmationError(SnapshotError):
    code = "SNAPSHOT_ACQUISITION_CONFIRMATION_REQUIRED"


class SnapshotAcquisitionNotGovernedError(SnapshotError):
    code = "SNAPSHOT_ACQUISITION_NOT_GOVERNED"


class SnapshotAcquisitionRecoveryRequiredError(SnapshotError):
    code = "SNAPSHOT_ACQUISITION_RECOVERY_REQUIRED"
    exit_code = 6


class SnapshotAcquisitionIntegrityError(EvidenceError):
    code = "SNAPSHOT_ACQUISITION_INTEGRITY_FAILED"


class SnapshotAcquisitionCancelledError(SnapshotError):
    code = "SNAPSHOT_ACQUISITION_CANCELLED"
    exit_code = 9


class SnapshotRefreshError(SnapshotError):
    code = "SNAPSHOT_REFRESH_INVALID"


class SnapshotRefreshBaseError(SnapshotRefreshError):
    code = "SNAPSHOT_REFRESH_BASE_INVALID"


class SnapshotChangeReviewError(SnapshotError):
    code = "SNAPSHOT_CHANGE_REVIEW_INVALID"


class SnapshotChangeReviewResourceError(SnapshotChangeReviewError):
    code = "SNAPSHOT_CHANGE_REVIEW_RESOURCE_LIMIT"


class DiffError(StewardError):
    code = "DIFF_INVALID"
    exit_code = 2


class DiffNotFoundError(DiffError):
    code = "DIFF_NOT_FOUND"


class RelationError(StewardError):
    """A requested cross-Snapshot relation pair violates the frozen protocol."""

    code = "RELATION_INVALID"
    exit_code = 2


class CodeExecutionError(StewardError):
    """A bounded code-workspace grounding request could not be admitted."""

    code = "CODE_EXECUTION_INVALID"
    exit_code = 2


class CodeExecutionRepositoryError(CodeExecutionError):
    code = "CODE_EXECUTION_REPOSITORY_INVALID"


class CodeExecutionBaselineError(CodeExecutionError):
    code = "CODE_EXECUTION_BASELINE_INVALID"


class CodeExecutionResourceError(CodeExecutionError):
    code = "CODE_EXECUTION_RESOURCE_LIMIT"


class DuplicateAnalysisError(StewardError):
    """A requested exact-payload duplicate analysis is not eligible."""

    code = "DUPLICATE_INVALID"
    exit_code = 2


class StructureError(StewardError):
    """A requested storage structure analysis violates its frozen boundary."""

    code = "STRUCTURE_INVALID"
    exit_code = 2


class GrowthError(StewardError):
    """A requested pairwise storage-growth analysis is not eligible."""

    code = "GROWTH_INVALID"
    exit_code = 2


class ResourceObservationError(StewardError):
    code = "RESOURCE_OBSERVATION_INVALID"
    exit_code = 2


class ResourceCollectionError(StewardError):
    code = "RESOURCE_OBSERVATION_FAILED"
    exit_code = 3


class DocumentInspectionError(StewardError):
    """A public current-document inspection request is not admissible."""

    code = "DOCUMENT_INSPECTION_INVALID"
    exit_code = 2


class DocumentInspectionConfirmationError(DocumentInspectionError):
    code = "DOCUMENT_INSPECTION_CONFIRMATION_REQUIRED"


class DocumentInspectionScopeError(DocumentInspectionError):
    code = "DOCUMENT_INSPECTION_SCOPE_INVALID"


class DocumentInspectionInputError(DocumentInspectionError):
    code = "DOCUMENT_INSPECTION_INPUT_INVALID"


class DocumentInspectionSourceChangedError(DocumentInspectionError):
    code = "DOCUMENT_INSPECTION_SOURCE_CHANGED"


class DocumentInspectionUnavailableError(StewardError):
    """The product boundary failed without publishing extracted content."""

    code = "DOCUMENT_INSPECTION_FAILED"
    exit_code = 4
