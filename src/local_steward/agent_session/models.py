"""Path-safe immutable models for one unified STEWARD authority domain."""

from dataclasses import dataclass, field
from enum import Enum

from ..models import (
    FilesystemSnapshotSummary,
    ScopeConfig,
    SnapshotVerificationResult,
    StewardConfig,
)


SESSION_SCHEMA_NAME = "local_steward.steward_session"
SESSION_SCHEMA_VERSION = 1
MAX_RESOLUTION_CANDIDATES = 10_000


class SelectionPolicy(str, Enum):
    EXACT_ID = "EXACT_ID"
    TASK_CREATED = "TASK_CREATED"
    ONLY_COMPATIBLE = "ONLY_COMPATIBLE"
    LATEST_VALID = "LATEST_VALID"
    PREVIOUS_VALID = "PREVIOUS_VALID"


class TaskObjectKind(str, Enum):
    SNAPSHOT = "SNAPSHOT"
    RUN = "RUN"
    SCOPE = "SCOPE"
    ENTRY = "ENTRY"


class PathInputKind(str, Enum):
    SCOPED_RELATIVE = "SCOPED_RELATIVE"
    USER_ABSOLUTE = "USER_ABSOLUTE"
    TASK_CREATED_ENTRY = "TASK_CREATED_ENTRY"


@dataclass(frozen=True, slots=True)
class StewardSessionIdentity:
    schema_name: str
    schema_version: int
    project_name: str
    configuration_digest: str
    authority_domain_digest: str


@dataclass(frozen=True, slots=True)
class StewardSession:
    """One immutable product configuration shared by every Agent operation."""

    identity: StewardSessionIdentity
    config: StewardConfig = field(repr=False)


@dataclass(frozen=True, slots=True)
class TaskObjectReference:
    reference_id: str
    authority_domain_digest: str
    kind: TaskObjectKind
    object_id: str
    snapshot_id: str | None = None
    scope_id: str | None = None
    relative_path: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotSelectionRequest:
    policy: SelectionPolicy
    scope_id: str | None = None
    exact_snapshot_id: str | None = None
    task_reference: TaskObjectReference | None = None
    anchor_snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedSnapshot:
    policy: SelectionPolicy
    snapshot: FilesystemSnapshotSummary
    verification: SnapshotVerificationResult
    compatible_scope_id: str | None


@dataclass(frozen=True, slots=True)
class ScopeSelectionRequest:
    policy: SelectionPolicy
    exact_scope_id: str | None = None
    task_reference: TaskObjectReference | None = None


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    policy: SelectionPolicy
    scope: ScopeConfig = field(repr=False)

    @property
    def scope_id(self) -> str:
        return self.scope.scope_id


@dataclass(frozen=True, slots=True)
class ResolvedScopedPath:
    policy: SelectionPolicy
    input_kind: PathInputKind
    scope_id: str
    relative_path: str
