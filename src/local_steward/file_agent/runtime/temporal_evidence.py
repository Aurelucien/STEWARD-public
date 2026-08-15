"""Deterministic one-Entry historical/current evidence relation.

The relation owns orchestration across verified Snapshot Evidence and the
project-owned current-filesystem observation boundary.  It never accepts
caller-supplied facts and never turns location or digest equality into object
identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
from enum import Enum
from hashlib import sha256
import os
import stat
from time import monotonic_ns
from typing import Any, Protocol

from ...evidence import canonical_json
from ...errors import SnapshotNotFoundError
from ...models import (
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemObjectType,
    FilesystemObservationStatus,
    FilesystemSnapshotV2,
    PayloadObservationProvenance,
    PayloadObservationStatus,
    SnapshotEntryReference,
)
from ...snapshots import get_snapshot, verify_snapshot
from ..models import AgentToolError, ToolExecutionContext
from .bounded_content import MAX_CONTENT_BYTES_PER_READ, ProjectOwnedBoundedTextMcp
from .failures import RuntimeFailure
from .runtime import RuntimeTool, RuntimeToolResult, SourceFamily, ToolRegistry
from .scope_binding import ScopeBindings


CURRENT_FILESYSTEM_METADATA = "CURRENT_FILESYSTEM_METADATA"
CURRENT_FILESYSTEM_CONTENT = "CURRENT_FILESYSTEM_CONTENT"
HISTORICAL_SNAPSHOT_METADATA = "HISTORICAL_SNAPSHOT_METADATA"
HISTORICAL_CURRENT_RELATION = "HISTORICAL_CURRENT_RELATION"
SAME_LOGICAL_LOCATION = "SAME_LOGICAL_LOCATION"
_DIGEST_DOMAIN = "local_steward.temporal_evidence_relation.v1"

_FIELDS = (
    "object_type",
    "size_bytes",
    "mtime_ns",
    "ctime_ns",
    "mode",
    "uid",
    "gid",
    "link_count",
    "symlink_target_raw",
)


class CurrentState(str, Enum):
    PRESENT = "CURRENT_PRESENT"
    ABSENT = "CURRENT_ABSENT"
    UNAVAILABLE = "CURRENT_UNAVAILABLE"
    CHANGED_DURING_OBSERVATION = "CURRENT_CHANGED_DURING_OBSERVATION"


class ComparisonOutcome(str, Enum):
    SAME = "SAME"
    DIFFERENT = "DIFFERENT"
    UNKNOWN = "UNKNOWN"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class EvidenceValueState(str, Enum):
    OBSERVED = "OBSERVED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class EvidenceValue:
    state: EvidenceValueState
    value: int | str | None = None

    def payload(self) -> dict[str, int | str]:
        result: dict[str, int | str] = {"state": self.state.value}
        if self.state == EvidenceValueState.OBSERVED:
            assert self.value is not None
            result["value"] = self.value
        return result


@dataclass(frozen=True, slots=True)
class FieldComparison:
    field: str
    historical: EvidenceValue
    current: EvidenceValue
    outcome: ComparisonOutcome

    def payload(self) -> dict[str, object]:
        return {
            "field": self.field,
            "historical": self.historical.payload(),
            "current": self.current.payload(),
            "outcome": self.outcome.value,
        }


@dataclass(frozen=True, slots=True)
class DigestEvidence:
    source_kind: str
    algorithm: str
    algorithm_version: int
    coverage: str
    digest: str
    bytes_observed_or_hashed: int
    provenance: str

    def payload(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind,
            "algorithm": self.algorithm,
            "algorithm_version": self.algorithm_version,
            "coverage": self.coverage,
            "digest": self.digest,
            "bytes_observed_or_hashed": self.bytes_observed_or_hashed,
            "provenance": self.provenance,
        }


@dataclass(frozen=True, slots=True)
class PayloadComparison:
    outcome: ComparisonOutcome
    historical: DigestEvidence | None
    current: DigestEvidence | None
    reason_code: str

    def payload(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "historical": None if self.historical is None else self.historical.payload(),
            "current": None if self.current is None else self.current.payload(),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class HistoricalProvenance:
    reference: SnapshotEntryReference
    snapshot_schema_version: int
    snapshot_digest: str
    evidence_id: str | None
    started_at: str
    completed_at: str

    def payload(self) -> dict[str, object]:
        return {
            "source_kind": HISTORICAL_SNAPSHOT_METADATA,
            "reference": _reference_payload(self.reference),
            "snapshot_schema_version": self.snapshot_schema_version,
            "snapshot_digest": self.snapshot_digest,
            "evidence_id": self.evidence_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "repository_verification": "VALID",
        }


@dataclass(frozen=True, slots=True)
class ResolvedHistoricalEntry:
    entry: FilesystemEntry | FilesystemEntryV2
    provenance: HistoricalProvenance


class HistoricalEntryResolver(Protocol):
    def resolve(self, reference: SnapshotEntryReference) -> ResolvedHistoricalEntry: ...


@dataclass(slots=True)
class VerifiedSnapshotEntryResolver:
    """Resolve one Entry only after repository-aware Snapshot verification."""

    context: ToolExecutionContext

    def resolve(self, reference: SnapshotEntryReference) -> ResolvedHistoricalEntry:
        try:
            verification = verify_snapshot(self.context.config, reference.snapshot_id)
            if verification.status != "VALID":
                raise RuntimeFailure("STEWARD_TOOL_FAILED", "historical Snapshot is not valid")
            snapshot = get_snapshot(self.context.config, reference.snapshot_id)
        except RuntimeFailure:
            raise
        except SnapshotNotFoundError as error:
            raise RuntimeFailure("STEWARD_TOOL_FAILED", "historical Snapshot was not found") from error
        except Exception as error:
            raise RuntimeFailure("STEWARD_TOOL_FAILED", "historical Evidence is unavailable") from error
        if reference.scope_id not in snapshot.scope_ids:
            raise RuntimeFailure("TOOL_ARGUMENT_INVALID", "scope is not present in historical Snapshot")
        matches = tuple(
            entry
            for entry in snapshot.entries
            if entry.scope_id == reference.scope_id and entry.relative_path == reference.relative_path
        )
        if len(matches) != 1:
            raise RuntimeFailure("STEWARD_TOOL_FAILED", "historical Entry was not found")
        schema_version = snapshot.snapshot_schema_version if isinstance(snapshot, FilesystemSnapshotV2) else 1
        return ResolvedHistoricalEntry(
            matches[0],
            HistoricalProvenance(
                reference,
                schema_version,
                snapshot.snapshot_digest,
                snapshot.evidence_id,
                snapshot.started_at,
                snapshot.completed_at,
            ),
        )


@dataclass(frozen=True, slots=True)
class CurrentMetadataObservation:
    state: CurrentState
    scope_id: str
    relative_path: str
    object_type: FilesystemObjectType | None = None
    size_bytes: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    link_count: int | None = None
    symlink_target_raw: str | None = None
    reason_code: str | None = None
    _coherence_token: tuple[int, int, int, int, int, int] | None = None


class CurrentMetadataPort(Protocol):
    def observe(self, scope_id: str, relative_path: str) -> CurrentMetadataObservation: ...


@dataclass(slots=True)
class ProjectOwnedCurrentMetadataObserver:
    """No-follow typed metadata observation under an existing ScopeBinding."""

    bindings: ScopeBindings

    def observe(self, scope_id: str, relative_path: str) -> CurrentMetadataObservation:
        binding = self.bindings.require(scope_id)
        binding.resolve_relative_path(relative_path)
        try:
            root = binding.allowed_root.resolve(strict=True)
        except OSError:
            return _current_failure(CurrentState.UNAVAILABLE, scope_id, relative_path, "CURRENT_IO_UNAVAILABLE")
        root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            return _current_failure(CurrentState.UNAVAILABLE, scope_id, relative_path, "CURRENT_IO_UNAVAILABLE")
        opened: list[int] = []
        try:
            root_fd = os.open(root, root_flags)
            opened.append(root_fd)
            parent_fd = root_fd
            components = relative_path.split("/")
            for component in components[:-1]:
                parent_fd = os.open(component, root_flags | nofollow, dir_fd=parent_fd)
                opened.append(parent_fd)
            name = components[-1]
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            object_type = _object_type(before.st_mode)
            link_target = os.readlink(name, dir_fd=parent_fd) if object_type == FilesystemObjectType.SYMLINK else None
            after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _state_token(before, object_type) != _state_token(after, _object_type(after.st_mode)):
                return _current_failure(
                    CurrentState.CHANGED_DURING_OBSERVATION,
                    scope_id,
                    relative_path,
                    "CURRENT_STATE_CHANGED",
                )
            return CurrentMetadataObservation(
                CurrentState.PRESENT,
                scope_id,
                relative_path,
                object_type,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_mode,
                before.st_uid,
                before.st_gid,
                before.st_nlink,
                link_target,
                None,
                _state_token(before, object_type),
            )
        except OSError as error:
            if error.errno in {errno.ENOENT, errno.ENOTDIR}:
                return _current_failure(CurrentState.ABSENT, scope_id, relative_path, "CURRENT_NOT_FOUND")
            reason = "CURRENT_PERMISSION_DENIED" if error.errno in {errno.EACCES, errno.EPERM} else "CURRENT_IO_UNAVAILABLE"
            return _current_failure(CurrentState.UNAVAILABLE, scope_id, relative_path, reason)
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)


@dataclass(frozen=True, slots=True)
class TemporalEvidenceRelation:
    historical_reference: SnapshotEntryReference
    historical_provenance: HistoricalProvenance
    current_state: CurrentState
    field_comparisons: tuple[FieldComparison, ...]
    payload_comparison: PayloadComparison
    observation_facts: tuple[str, ...]
    warnings: tuple[str, ...]
    current_source_kinds: tuple[str, ...]
    _content_bytes_observed: int = 0

    def _without_digest(self) -> dict[str, object]:
        return {
            "relation_schema_version": 1,
            "source_kind": HISTORICAL_CURRENT_RELATION,
            "observation_tool": "compare_historical_current",
            "historical_reference": _reference_payload(self.historical_reference),
            "historical_provenance": self.historical_provenance.payload(),
            "current_source_provenance": {
                "source_kinds": list(self.current_source_kinds),
                "scope_id": self.historical_reference.scope_id,
                "relative_path": self.historical_reference.relative_path,
            },
            "correlation_basis": SAME_LOGICAL_LOCATION,
            "current_state": self.current_state.value,
            "field_comparisons": [item.payload() for item in self.field_comparisons],
            "payload_comparison": self.payload_comparison.payload(),
            "observation_facts": list(self.observation_facts),
            "warnings": list(self.warnings),
        }

    @property
    def result_digest(self) -> str:
        return sha256(canonical_json({"domain": _DIGEST_DOMAIN, "relation": self._without_digest()})).hexdigest()

    @property
    def content_bytes_observed(self) -> int:
        return self._content_bytes_observed

    def payload(self) -> dict[str, object]:
        result = self._without_digest()
        result["result_digest"] = self.result_digest
        return result


@dataclass(slots=True)
class TemporalEvidenceRelationService:
    """Atomic one-Entry relation orchestration over existing authorities."""

    context: ToolExecutionContext
    bindings: ScopeBindings
    content_primitive: ProjectOwnedBoundedTextMcp
    historical_resolver: HistoricalEntryResolver | None = None
    current_metadata: CurrentMetadataPort | None = None

    def __post_init__(self) -> None:
        if self.historical_resolver is None:
            self.historical_resolver = VerifiedSnapshotEntryResolver(self.context)
        if self.current_metadata is None:
            self.current_metadata = ProjectOwnedCurrentMetadataObserver(self.bindings)

    def preflight(self, arguments: dict[str, object]) -> None:
        snapshot_id = arguments.get("snapshot_id")
        scope_id = arguments.get("scope_id")
        relative_path = arguments.get("relative_path")
        include_payload = arguments.get("include_payload_comparison", False)
        if not all(isinstance(value, str) and value for value in (snapshot_id, scope_id, relative_path)):
            raise RuntimeFailure("TOOL_ARGUMENT_INVALID", "historical reference is invalid")
        if not isinstance(include_payload, bool):
            raise RuntimeFailure("TOOL_ARGUMENT_INVALID", "payload comparison selector is invalid")
        assert isinstance(scope_id, str) and isinstance(relative_path, str)
        self.bindings.require(scope_id).resolve_relative_path(relative_path)

    def content_reservation(self, arguments: dict[str, object]) -> int:
        return MAX_CONTENT_BYTES_PER_READ if arguments.get("include_payload_comparison", False) is True else 0

    def compare(self, arguments: dict[str, object]) -> TemporalEvidenceRelation:
        self.preflight(arguments)
        snapshot_id = str(arguments["snapshot_id"])
        scope_id = str(arguments["scope_id"])
        relative_path = str(arguments["relative_path"])
        include_payload = arguments.get("include_payload_comparison", False) is True
        reference = SnapshotEntryReference(snapshot_id, scope_id, relative_path)
        try:
            self.context.budget.effective_limit(1)
            self.context.budget.begin_call()
        except AgentToolError as error:
            raise RuntimeFailure("BUDGET_EXHAUSTED", "historical shared budget is exhausted") from error
        started = monotonic_ns()
        assert self.historical_resolver is not None and self.current_metadata is not None
        historical = self.historical_resolver.resolve(reference)
        current = self.current_metadata.observe(scope_id, relative_path)
        payload = PayloadComparison(ComparisonOutcome.UNKNOWN, None, None, "PAYLOAD_NOT_REQUESTED")
        source_kinds = [CURRENT_FILESYSTEM_METADATA]
        observed_bytes = 0
        if include_payload:
            payload, current, observed_bytes, attempted = self._compare_payload(historical, current)
            if attempted:
                source_kinds.append(CURRENT_FILESYSTEM_CONTENT)
        comparisons = _field_comparisons(historical.entry, current)
        facts = (current.reason_code,) if current.reason_code else ("CURRENT_STATE_STABLE",)
        relation = TemporalEvidenceRelation(
            reference,
            historical.provenance,
            current.state,
            comparisons,
            payload,
            facts,
            (),
            tuple(source_kinds),
            observed_bytes,
        )
        serialized_bytes = len(canonical_json(relation.payload()))
        elapsed_ms = (monotonic_ns() - started) // 1_000_000
        try:
            self.context.budget.consume_result(items=1, serialized_bytes=serialized_bytes, elapsed_ms=elapsed_ms)
        except AgentToolError as error:
            raise RuntimeFailure("BUDGET_EXHAUSTED", "historical shared budget is exhausted") from error
        return relation

    def _compare_payload(
        self,
        historical: ResolvedHistoricalEntry,
        current: CurrentMetadataObservation,
    ) -> tuple[PayloadComparison, CurrentMetadataObservation, int, bool]:
        historical_digest = _historical_digest(historical.entry, historical.provenance.snapshot_schema_version)
        if historical_digest is None:
            return PayloadComparison(ComparisonOutcome.UNKNOWN, None, None, "HISTORICAL_PAYLOAD_UNKNOWN"), current, 0, False
        if historical_digest.algorithm != "sha256" or historical_digest.algorithm_version != 1 or historical_digest.coverage != "COMPLETE_PRIMARY_STREAM":
            return PayloadComparison(ComparisonOutcome.NOT_COMPARABLE, historical_digest, None, "PAYLOAD_INCOMPATIBLE"), current, 0, False
        if current.state != CurrentState.PRESENT or current.object_type != FilesystemObjectType.REGULAR_FILE:
            return PayloadComparison(ComparisonOutcome.UNKNOWN, historical_digest, None, "CURRENT_PAYLOAD_UNKNOWN"), current, 0, False
        if current.size_bytes is None or current.size_bytes > MAX_CONTENT_BYTES_PER_READ:
            return PayloadComparison(ComparisonOutcome.UNKNOWN, historical_digest, None, "CURRENT_PAYLOAD_UNKNOWN"), current, 0, False
        content = self.content_primitive.read_bounded_utf8_file(
            {"scope_id": current.scope_id, "relative_path": current.relative_path}
        )
        assert self.current_metadata is not None
        after = self.current_metadata.observe(current.scope_id, current.relative_path)
        if after.state != CurrentState.PRESENT or current._coherence_token != after._coherence_token:
            changed = _current_failure(
                CurrentState.CHANGED_DURING_OBSERVATION,
                current.scope_id,
                current.relative_path,
                "CURRENT_STATE_CHANGED",
            )
            return PayloadComparison(ComparisonOutcome.UNKNOWN, historical_digest, None, "CURRENT_STATE_CHANGED"), changed, 0, True
        if content.status in {"COMPLETE", "EMPTY"} and (
            content.source_size_bytes != current.size_bytes
            or content.content_bytes_observed != current.size_bytes
        ):
            changed = _current_failure(
                CurrentState.CHANGED_DURING_OBSERVATION,
                current.scope_id,
                current.relative_path,
                "CURRENT_STATE_CHANGED",
            )
            return PayloadComparison(ComparisonOutcome.UNKNOWN, historical_digest, None, "CURRENT_STATE_CHANGED"), changed, 0, True
        if content.status not in {"COMPLETE", "EMPTY"} or content.observed_content_sha256 is None:
            return PayloadComparison(ComparisonOutcome.UNKNOWN, historical_digest, None, "CURRENT_PAYLOAD_UNKNOWN"), after, 0, True
        current_digest = DigestEvidence(
            CURRENT_FILESYSTEM_CONTENT,
            "sha256",
            1,
            "COMPLETE_PRIMARY_STREAM",
            content.observed_content_sha256,
            content.content_bytes_observed,
            "DIRECT_BOUNDED_READ",
        )
        outcome = (
            ComparisonOutcome.SAME
            if historical_digest.digest == current_digest.digest
            else ComparisonOutcome.DIFFERENT
        )
        reason = "PAYLOAD_DIGEST_EQUAL" if outcome == ComparisonOutcome.SAME else "PAYLOAD_DIGEST_DIFFERENT"
        return PayloadComparison(outcome, historical_digest, current_digest, reason), after, content.content_bytes_observed, True


_TEMPORAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "snapshot_id": {"type": "string", "minLength": 1},
        "scope_id": {"type": "string", "minLength": 1},
        "relative_path": {"type": "string", "minLength": 1},
        "include_payload_comparison": {"type": "boolean", "default": False},
    },
    "required": ["snapshot_id", "scope_id", "relative_path"],
    "additionalProperties": False,
}


def register_temporal_evidence_tool(
    registry: ToolRegistry, service: TemporalEvidenceRelationService
) -> None:
    """Register only the generic relation capability; facts remain engine-owned."""

    def dispatch(arguments: dict[str, Any]) -> RuntimeToolResult:
        relation = service.compare(arguments)
        payload = relation.payload()
        return RuntimeToolResult(
            SourceFamily.TEMPORAL_RELATION,
            payload,
            relation.result_digest,
            1,
            len(canonical_json(payload)),
            status="COMPLETE",
            content_bytes_observed=relation.content_bytes_observed,
        )

    registry.register(
        RuntimeTool(
            "compare_historical_current",
            "Compare one verified historical Snapshot Entry with its current scoped logical location. "
            "The result is deterministic untrusted evidence and never permanent object identity.",
            _TEMPORAL_SCHEMA,
            SourceFamily.TEMPORAL_RELATION,
            dispatch,
            preflight=service.preflight,
            content_reservation=service.content_reservation,
        )
    )


def _reference_payload(reference: SnapshotEntryReference) -> dict[str, str]:
    return {
        "snapshot_id": reference.snapshot_id,
        "scope_id": reference.scope_id,
        "relative_path": reference.relative_path,
    }


def _current_failure(
    state: CurrentState, scope_id: str, relative_path: str, reason: str
) -> CurrentMetadataObservation:
    return CurrentMetadataObservation(state, scope_id, relative_path, reason_code=reason)


def _object_type(mode: int) -> FilesystemObjectType:
    if stat.S_ISREG(mode):
        return FilesystemObjectType.REGULAR_FILE
    if stat.S_ISDIR(mode):
        return FilesystemObjectType.DIRECTORY
    if stat.S_ISLNK(mode):
        return FilesystemObjectType.SYMLINK
    if stat.S_ISFIFO(mode):
        return FilesystemObjectType.FIFO
    if stat.S_ISSOCK(mode):
        return FilesystemObjectType.SOCKET
    if stat.S_ISCHR(mode):
        return FilesystemObjectType.CHARACTER_DEVICE
    if stat.S_ISBLK(mode):
        return FilesystemObjectType.BLOCK_DEVICE
    return FilesystemObjectType.UNKNOWN


def _state_token(value: os.stat_result, object_type: FilesystemObjectType) -> tuple[int, int, int, int, int, int]:
    return (
        list(FilesystemObjectType).index(object_type),
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _historical_values(entry: FilesystemEntry | FilesystemEntryV2) -> dict[str, EvidenceValue]:
    if entry.observation_status != FilesystemObservationStatus.OBSERVED:
        return {field: EvidenceValue(EvidenceValueState.UNKNOWN) for field in _FIELDS}
    values: dict[str, EvidenceValue] = {
        "object_type": _observed(entry.object_type.value) if entry.object_type != FilesystemObjectType.UNKNOWN else EvidenceValue(EvidenceValueState.UNKNOWN),
        "size_bytes": _observed(entry.size_bytes) if entry.object_type == FilesystemObjectType.REGULAR_FILE else EvidenceValue(EvidenceValueState.NOT_APPLICABLE),
        "mtime_ns": _observed(entry.mtime_ns),
        "ctime_ns": _observed(entry.ctime_ns),
        "mode": _observed(entry.mode),
        "uid": _observed(entry.uid),
        "gid": _observed(entry.gid),
        "link_count": _observed(entry.link_count),
        "symlink_target_raw": (
            _observed(entry.symlink_target_raw)
            if entry.object_type == FilesystemObjectType.SYMLINK
            else EvidenceValue(EvidenceValueState.NOT_APPLICABLE)
        ),
    }
    return values


def _current_values(current: CurrentMetadataObservation) -> dict[str, EvidenceValue]:
    if current.state == CurrentState.ABSENT:
        return {field: EvidenceValue(EvidenceValueState.NOT_APPLICABLE) for field in _FIELDS}
    if current.state != CurrentState.PRESENT:
        return {field: EvidenceValue(EvidenceValueState.UNKNOWN) for field in _FIELDS}
    values: dict[str, EvidenceValue] = {
        "object_type": (
            _observed(current.object_type.value)
            if current.object_type is not None and current.object_type != FilesystemObjectType.UNKNOWN
            else EvidenceValue(EvidenceValueState.UNKNOWN)
        ),
        "size_bytes": _observed(current.size_bytes) if current.object_type == FilesystemObjectType.REGULAR_FILE else EvidenceValue(EvidenceValueState.NOT_APPLICABLE),
        "mtime_ns": _observed(current.mtime_ns),
        "ctime_ns": _observed(current.ctime_ns),
        "mode": _observed(current.mode),
        "uid": _observed(current.uid),
        "gid": _observed(current.gid),
        "link_count": _observed(current.link_count),
        "symlink_target_raw": (
            _observed(current.symlink_target_raw)
            if current.object_type == FilesystemObjectType.SYMLINK
            else EvidenceValue(EvidenceValueState.NOT_APPLICABLE)
        ),
    }
    return values


def _observed(value: int | str | None) -> EvidenceValue:
    return EvidenceValue(EvidenceValueState.UNKNOWN) if value is None else EvidenceValue(EvidenceValueState.OBSERVED, value)


def _field_comparisons(
    historical: FilesystemEntry | FilesystemEntryV2,
    current: CurrentMetadataObservation,
) -> tuple[FieldComparison, ...]:
    historical_values = _historical_values(historical)
    current_values = _current_values(current)
    result: list[FieldComparison] = []
    for field in _FIELDS:
        left = historical_values[field]
        right = current_values[field]
        if current.state == CurrentState.ABSENT:
            outcome = ComparisonOutcome.NOT_COMPARABLE
        elif EvidenceValueState.UNKNOWN in {left.state, right.state}:
            outcome = ComparisonOutcome.UNKNOWN
        elif EvidenceValueState.NOT_APPLICABLE in {left.state, right.state}:
            outcome = ComparisonOutcome.NOT_COMPARABLE
        else:
            outcome = ComparisonOutcome.SAME if left.value == right.value else ComparisonOutcome.DIFFERENT
        result.append(FieldComparison(field, left, right, outcome))
    return tuple(result)


def _historical_digest(
    entry: FilesystemEntry | FilesystemEntryV2, schema_version: int
) -> DigestEvidence | None:
    if schema_version != 2 or not isinstance(entry, FilesystemEntryV2):
        return None
    observation = entry.payload_observation
    if (
        entry.object_type != FilesystemObjectType.REGULAR_FILE
        or observation.status not in {PayloadObservationStatus.HASHED, PayloadObservationStatus.EMPTY_FILE_HASHED}
        or observation.algorithm is None
        or observation.algorithm_version is None
        or observation.digest is None
        or observation.bytes_hashed is None
        or observation.provenance not in {
            PayloadObservationProvenance.DIRECT_READ,
            PayloadObservationProvenance.REUSED_FROM_VERIFIED_SNAPSHOT,
        }
    ):
        return None
    return DigestEvidence(
        HISTORICAL_SNAPSHOT_METADATA,
        observation.algorithm,
        observation.algorithm_version,
        "COMPLETE_PRIMARY_STREAM",
        observation.digest,
        observation.bytes_hashed,
        observation.provenance.value,
    )
