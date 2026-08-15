"""Unified session admission and deterministic Agent-side object resolution."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import hmac
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any
from uuid import UUID

from ..config import load_config
from ..database import SCHEMA_VERSION as DATABASE_SCHEMA_VERSION
from ..database import database_path, open_readonly_initialized
from ..evidence import canonical_json, compute_config_digest
from ..errors import SnapshotNotFoundError
from ..file_agent.runtime.failures import RuntimeFailure
from ..file_agent.runtime.scope_binding import ScopeBinding
from ..models import (
    FilesystemSnapshot,
    FilesystemSnapshotSummary,
    FilesystemSnapshotV2,
    ScopeConfig,
    ScopeRole,
    SnapshotVerificationResult,
    StewardConfig,
)
from ..paths import canonicalize_host_absolute_path, contains
from ..scopes import validate_scopes
from ..snapshots import (
    _snapshot_inventory_with_verification,
    _verified_snapshot_detail,
    _verified_snapshot_entries,
    entry_id,
)
from .errors import (
    StewardAuthorityDomainError,
    StewardPathResolutionError,
    StewardScopeResolutionError,
    StewardSelectionAmbiguousError,
    StewardSelectionError,
    StewardSelectionNotFoundError,
    StewardSelectionResourceError,
    StewardSessionConfigurationError,
    StewardTaskReferenceError,
)
from .models import (
    MAX_RESOLUTION_CANDIDATES,
    SESSION_SCHEMA_NAME,
    SESSION_SCHEMA_VERSION,
    PathInputKind,
    ResolvedScope,
    ResolvedScopedPath,
    ResolvedSnapshot,
    ScopeSelectionRequest,
    SelectionPolicy,
    SnapshotSelectionRequest,
    StewardSession,
    StewardSessionIdentity,
    TaskObjectKind,
    TaskObjectReference,
)


_ACTIONABLE_ROLES = frozenset({ScopeRole.MANAGED_ROOT, ScopeRole.REFERENCE_ROOT})
_DOMAIN_DIGEST_LABEL = "local_steward.authority_domain.v1"
_TASK_REFERENCE_DIGEST_LABEL = "local_steward.task_object_reference.v1"
_MAX_PATH_BYTES = 16_384
_MAX_TASK_IDENTITY_BYTES = 1_024


def _digest(value: dict[str, Any]) -> str:
    return sha256(canonical_json(value)).hexdigest()


def _bounded_utf8(value: str, maximum: int) -> bool:
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


def _validate_evidence_root(config: StewardConfig) -> None:
    root = config.paths.evidence_dir
    try:
        state = root.lstat()
    except OSError as error:
        raise StewardSessionConfigurationError("configured Evidence root is unavailable") from error
    if (
        stat.S_ISLNK(state.st_mode)
        or not stat.S_ISDIR(state.st_mode)
        or not os.access(root, os.R_OK)
    ):
        raise StewardSessionConfigurationError(
            "configured Evidence root must be a readable non-symlink directory"
        )
    if not contains(config.paths.data_dir, root):
        raise StewardSessionConfigurationError(
            "configured Evidence root is outside the session data authority"
        )


def create_steward_session(config: StewardConfig) -> StewardSession:
    """Admit one already-loaded configuration without publishing host paths."""
    if not isinstance(config, StewardConfig):
        raise StewardSessionConfigurationError("STEWARD configuration is invalid")
    validate_scopes(
        config.scopes,
        (
            config.paths.data_dir,
            config.paths.cache_dir,
            config.paths.evidence_dir,
            config.paths.quarantine_dir,
        ),
    )
    _validate_evidence_root(config)
    expected_database = config.paths.data_dir / "state.db"
    if database_path(config) != expected_database:
        raise StewardSessionConfigurationError("database authority is not configuration-derived")
    with open_readonly_initialized(config):
        pass
    configuration_digest = compute_config_digest(config)
    authority_domain_digest = _digest(
        {
            "domain": _DOMAIN_DIGEST_LABEL,
            "configuration_digest": configuration_digest,
            "database_schema_version": DATABASE_SCHEMA_VERSION,
            "database_role": "DERIVED_INDEX",
            "evidence_role": "AUTHORITATIVE_ROOT",
        }
    )
    identity = StewardSessionIdentity(
        SESSION_SCHEMA_NAME,
        SESSION_SCHEMA_VERSION,
        config.project_name,
        configuration_digest,
        authority_domain_digest,
    )
    return StewardSession(identity, config)


def load_steward_session(
    explicit_path: Path | None = None, *, project_root: Path | None = None
) -> StewardSession:
    """Select, validate and bind exactly one product-supported configuration."""
    return create_steward_session(load_config(explicit_path, project_root=project_root))


def require_authority_domain(session: StewardSession, config: StewardConfig) -> StewardConfig:
    """Reject split Core/Context configuration before any business operation."""
    if not isinstance(session, StewardSession) or not isinstance(config, StewardConfig):
        raise StewardAuthorityDomainError("STEWARD authority domain is invalid")
    supplied = compute_config_digest(config)
    if not hmac.compare_digest(supplied, session.identity.configuration_digest):
        raise StewardAuthorityDomainError("STEWARD configuration authority does not match session")
    return session.config


def _summary(
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
) -> FilesystemSnapshotSummary:
    return FilesystemSnapshotSummary(
        snapshot.snapshot_id,
        snapshot.run_id,
        snapshot.status,
        snapshot.created_at,
        snapshot.scope_ids,
        snapshot.entry_count,
        snapshot.observed_count,
        snapshot.error_count,
        snapshot.snapshot_digest,
    )


def _exact_valid_snapshot(session: StewardSession, snapshot_id: str) -> ResolvedSnapshot:
    _validate_uuid(snapshot_id, "Snapshot")
    try:
        verification, snapshot = _verified_snapshot_detail(session.config, snapshot_id)
    except SnapshotNotFoundError as error:
        raise StewardSelectionNotFoundError("selected Snapshot does not exist") from error
    if verification.status != "VALID":
        raise StewardSelectionError("selected Snapshot is not authoritatively VALID")
    return ResolvedSnapshot(SelectionPolicy.EXACT_ID, _summary(snapshot), verification, None)


def _validate_uuid(value: str, label: str) -> None:
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise StewardSelectionError(f"{label} identity is invalid")


def _valid_candidates(
    session: StewardSession, scope_id: str | None
) -> tuple[tuple[FilesystemSnapshotSummary, SnapshotVerificationResult], ...]:
    rows = _snapshot_inventory_with_verification(session.config, MAX_RESOLUTION_CANDIDATES + 1)
    if len(rows) > MAX_RESOLUTION_CANDIDATES:
        raise StewardSelectionResourceError("Snapshot selection candidate limit was exceeded")
    return tuple(
        (summary, verification)
        for summary, verification in rows
        if verification.status == "VALID" and (scope_id is None or scope_id in summary.scope_ids)
    )


def _single_candidate(
    candidates: tuple[tuple[FilesystemSnapshotSummary, SnapshotVerificationResult], ...],
    policy: SelectionPolicy,
    scope_id: str | None,
) -> ResolvedSnapshot:
    if not candidates:
        raise StewardSelectionNotFoundError("no compatible authoritatively VALID Snapshot exists")
    if len(candidates) != 1:
        raise StewardSelectionAmbiguousError(
            "Snapshot selection is ambiguous and requires one focused clarification"
        )
    summary, verification = candidates[0]
    return ResolvedSnapshot(policy, summary, verification, scope_id)


def _newest_candidate(
    candidates: tuple[tuple[FilesystemSnapshotSummary, SnapshotVerificationResult], ...],
    policy: SelectionPolicy,
    scope_id: str | None,
) -> ResolvedSnapshot:
    if not candidates:
        raise StewardSelectionNotFoundError("no compatible authoritatively VALID Snapshot exists")
    newest_time = max(summary.created_at for summary, _verification in candidates)
    newest = tuple(item for item in candidates if item[0].created_at == newest_time)
    if len(newest) != 1:
        raise StewardSelectionAmbiguousError(
            "newest compatible Snapshots are semantically tied; clarification is required"
        )
    summary, verification = newest[0]
    return ResolvedSnapshot(policy, summary, verification, scope_id)


def resolve_snapshot(
    session: StewardSession,
    request: SnapshotSelectionRequest,
    *,
    task_memory: TaskObjectMemory | None = None,
) -> ResolvedSnapshot:
    """Resolve one verified Snapshot through an explicit deterministic policy."""
    if not isinstance(session, StewardSession) or not isinstance(request, SnapshotSelectionRequest):
        raise StewardSelectionError("Snapshot selection request is invalid")
    policy = request.policy
    if policy == SelectionPolicy.EXACT_ID:
        if request.exact_snapshot_id is None or any(
            value is not None for value in (request.task_reference, request.anchor_snapshot_id)
        ):
            raise StewardSelectionError("EXACT_ID requires exactly one Snapshot identity")
        resolved = _exact_valid_snapshot(session, request.exact_snapshot_id)
        if request.scope_id is not None and request.scope_id not in resolved.snapshot.scope_ids:
            raise StewardSelectionNotFoundError("selected Snapshot is incompatible with Scope")
        return ResolvedSnapshot(policy, resolved.snapshot, resolved.verification, request.scope_id)
    if policy == SelectionPolicy.TASK_CREATED:
        if (
            task_memory is None
            or request.task_reference is None
            or any(
                value is not None
                for value in (request.exact_snapshot_id, request.anchor_snapshot_id)
            )
        ):
            raise StewardTaskReferenceError(
                "TASK_CREATED requires one host-owned task Snapshot reference"
            )
        reference = task_memory.require(request.task_reference, TaskObjectKind.SNAPSHOT)
        resolved = _exact_valid_snapshot(session, reference.object_id)
        if request.scope_id is not None and request.scope_id not in resolved.snapshot.scope_ids:
            raise StewardSelectionNotFoundError("task-created Snapshot is incompatible with Scope")
        return ResolvedSnapshot(policy, resolved.snapshot, resolved.verification, request.scope_id)
    if policy == SelectionPolicy.ONLY_COMPATIBLE:
        if any(
            value is not None
            for value in (
                request.exact_snapshot_id,
                request.task_reference,
                request.anchor_snapshot_id,
            )
        ):
            raise StewardSelectionError("ONLY_COMPATIBLE does not accept an object identity")
        return _single_candidate(
            _valid_candidates(session, request.scope_id), policy, request.scope_id
        )
    if policy == SelectionPolicy.LATEST_VALID:
        if any(
            value is not None
            for value in (
                request.exact_snapshot_id,
                request.task_reference,
                request.anchor_snapshot_id,
            )
        ):
            raise StewardSelectionError("LATEST_VALID does not accept an object identity")
        return _newest_candidate(
            _valid_candidates(session, request.scope_id), policy, request.scope_id
        )
    if policy == SelectionPolicy.PREVIOUS_VALID:
        if request.anchor_snapshot_id is None or any(
            value is not None for value in (request.exact_snapshot_id, request.task_reference)
        ):
            raise StewardSelectionError("PREVIOUS_VALID requires one exact anchor Snapshot")
        anchor = _exact_valid_snapshot(session, request.anchor_snapshot_id)
        scope_id = request.scope_id
        if scope_id is not None and scope_id not in anchor.snapshot.scope_ids:
            raise StewardSelectionNotFoundError("anchor Snapshot is incompatible with Scope")
        candidates = tuple(
            item
            for item in _valid_candidates(session, scope_id)
            if item[0].snapshot_id != anchor.snapshot.snapshot_id
            and item[0].created_at < anchor.snapshot.created_at
            and (scope_id is not None or item[0].scope_ids == anchor.snapshot.scope_ids)
        )
        return _newest_candidate(candidates, policy, scope_id)
    raise StewardSelectionError("Snapshot selection policy is unsupported")


def _actionable_scopes(session: StewardSession) -> tuple[ScopeConfig, ...]:
    return tuple(
        scope
        for scope in session.config.scopes
        if scope.enabled and scope.role in _ACTIONABLE_ROLES
    )


def _scope_by_id(session: StewardSession, scope_id: str) -> ScopeConfig:
    candidates = tuple(scope for scope in _actionable_scopes(session) if scope.scope_id == scope_id)
    if not candidates:
        raise StewardScopeResolutionError("enabled actionable Scope does not exist")
    return candidates[0]


def resolve_scope(
    session: StewardSession,
    request: ScopeSelectionRequest,
    *,
    task_memory: TaskObjectMemory | None = None,
) -> ResolvedScope:
    """Resolve an actionable Scope without fuzzy names or hidden ordering."""
    if not isinstance(session, StewardSession) or not isinstance(request, ScopeSelectionRequest):
        raise StewardScopeResolutionError("Scope selection request is invalid")
    if request.policy == SelectionPolicy.EXACT_ID:
        if request.exact_scope_id is None or request.task_reference is not None:
            raise StewardScopeResolutionError("EXACT_ID requires exactly one Scope identity")
        return ResolvedScope(request.policy, _scope_by_id(session, request.exact_scope_id))
    if request.policy == SelectionPolicy.TASK_CREATED:
        if (
            task_memory is None
            or request.task_reference is None
            or request.exact_scope_id is not None
        ):
            raise StewardTaskReferenceError("TASK_CREATED requires one host-owned Scope reference")
        reference = task_memory.require(request.task_reference, TaskObjectKind.SCOPE)
        return ResolvedScope(request.policy, _scope_by_id(session, reference.object_id))
    if request.policy == SelectionPolicy.ONLY_COMPATIBLE:
        if request.exact_scope_id is not None or request.task_reference is not None:
            raise StewardScopeResolutionError("ONLY_COMPATIBLE does not accept a Scope identity")
        candidates = _actionable_scopes(session)
        if not candidates:
            raise StewardSelectionNotFoundError("no enabled actionable Scope exists")
        if len(candidates) != 1:
            raise StewardSelectionAmbiguousError(
                "Scope selection is ambiguous and requires one focused clarification"
            )
        return ResolvedScope(request.policy, candidates[0])
    raise StewardScopeResolutionError("selection policy is not valid for Scope resolution")


def _validate_relative_path(relative_path: str) -> None:
    if not isinstance(relative_path, str) or not _bounded_utf8(relative_path, _MAX_PATH_BYTES):
        raise StewardPathResolutionError("Scope-relative path is invalid")
    try:
        ScopeBinding("resolved", Path("/")).resolve_relative_path(relative_path)
    except RuntimeFailure as error:
        raise StewardPathResolutionError("Scope-relative path is invalid") from error


def _admit_current_path(session: StewardSession, scope: ScopeConfig, relative_path: str) -> None:
    _validate_relative_path(relative_path)
    if scope.follow_directory_symlinks:
        raise StewardPathResolutionError("Scope permits directory symlinks and is not path-safe")
    candidate = scope.normalized_path.joinpath(*PurePosixPath(relative_path).parts)
    if any(
        item.role == ScopeRole.EXCLUDED_ROOT and contains(item.normalized_path, candidate)
        for item in session.config.scopes
    ):
        raise StewardPathResolutionError("path is inside a configured exclusion")
    try:
        configured_state = Path(scope.raw_path).expanduser().lstat()
        root_state = scope.normalized_path.lstat()
        if (
            stat.S_ISLNK(configured_state.st_mode)
            or stat.S_ISLNK(root_state.st_mode)
            or not stat.S_ISDIR(root_state.st_mode)
        ):
            raise StewardPathResolutionError("Scope root is not a non-symlink directory")
        current = scope.normalized_path
        for component in PurePosixPath(relative_path).parts:
            current = current / component
            state = current.lstat()
            if stat.S_ISLNK(state.st_mode):
                raise StewardPathResolutionError("path contains a symbolic link")
        if not scope.allow_cross_mount and current.lstat().st_dev != root_state.st_dev:
            raise StewardPathResolutionError("path crosses a disallowed mount boundary")
    except StewardPathResolutionError:
        raise
    except OSError as error:
        raise StewardPathResolutionError("path is unavailable") from error


def resolve_scoped_path(
    session: StewardSession, scope_id: str, relative_path: str
) -> ResolvedScopedPath:
    scope = _scope_by_id(session, scope_id)
    _admit_current_path(session, scope, relative_path)
    return ResolvedScopedPath(
        SelectionPolicy.EXACT_ID,
        PathInputKind.SCOPED_RELATIVE,
        scope.scope_id,
        relative_path,
    )


def resolve_user_absolute_path(session: StewardSession, absolute_path: str) -> ResolvedScopedPath:
    """Map one user-named absolute path to a unique internal Scope identity."""
    if (
        not isinstance(absolute_path, str)
        or not absolute_path
        or "\x00" in absolute_path
        or not _bounded_utf8(absolute_path, _MAX_PATH_BYTES)
        or any(ord(character) < 32 for character in absolute_path)
    ):
        raise StewardPathResolutionError("absolute path is invalid")
    candidate = Path(absolute_path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise StewardPathResolutionError("absolute path must not contain traversal")
    candidate = canonicalize_host_absolute_path(candidate)
    candidates = tuple(
        scope for scope in _actionable_scopes(session) if contains(scope.normalized_path, candidate)
    )
    if not candidates:
        raise StewardSelectionNotFoundError("absolute path is outside enabled actionable Scopes")
    if len(candidates) != 1:
        raise StewardSelectionAmbiguousError("absolute path maps to more than one Scope")
    scope = candidates[0]
    relative = candidate.relative_to(scope.normalized_path).as_posix()
    if relative == ".":
        raise StewardPathResolutionError("absolute path names a Scope root, not an object")
    _admit_current_path(session, scope, relative)
    return ResolvedScopedPath(
        SelectionPolicy.ONLY_COMPATIBLE,
        PathInputKind.USER_ABSOLUTE,
        scope.scope_id,
        relative,
    )


def resolve_user_absolute_scope(session: StewardSession, absolute_path: str) -> ResolvedScope:
    """Map one user-named absolute Scope root to its unique configured identity."""
    if (
        not isinstance(absolute_path, str)
        or not absolute_path
        or "\x00" in absolute_path
        or not _bounded_utf8(absolute_path, _MAX_PATH_BYTES)
        or any(ord(character) < 32 for character in absolute_path)
    ):
        raise StewardScopeResolutionError("absolute Scope path is invalid")
    candidate = Path(absolute_path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise StewardScopeResolutionError("absolute Scope path must not contain traversal")
    candidate = canonicalize_host_absolute_path(candidate)
    candidates = tuple(
        scope for scope in _actionable_scopes(session) if candidate == scope.normalized_path
    )
    if not candidates:
        raise StewardSelectionNotFoundError(
            "absolute path does not name an enabled actionable Scope root"
        )
    if len(candidates) != 1:
        raise StewardSelectionAmbiguousError("absolute path maps to more than one Scope")
    return ResolvedScope(SelectionPolicy.ONLY_COMPATIBLE, candidates[0])


class TaskObjectMemory:
    """Host-owned task memory; references are selection aids, never authority grants."""

    def __init__(self, session: StewardSession, task_identity: str) -> None:
        if not isinstance(session, StewardSession):
            raise StewardTaskReferenceError("task memory requires a STEWARD session")
        if (
            not isinstance(task_identity, str)
            or not task_identity
            or not _bounded_utf8(task_identity, _MAX_TASK_IDENTITY_BYTES)
            or any(ord(character) < 32 for character in task_identity)
        ):
            raise StewardTaskReferenceError("task identity is invalid")
        self._session = session
        self._task_digest = _digest(
            {
                "domain": "local_steward.task_identity.v1",
                "task_identity": task_identity,
                "authority_domain_digest": session.identity.authority_domain_digest,
            }
        )
        self._references: dict[str, TaskObjectReference] = {}

    def _record(
        self,
        kind: TaskObjectKind,
        object_id: str,
        *,
        snapshot_id: str | None = None,
        scope_id: str | None = None,
        relative_path: str | None = None,
    ) -> TaskObjectReference:
        payload = {
            "domain": _TASK_REFERENCE_DIGEST_LABEL,
            "task_digest": self._task_digest,
            "authority_domain_digest": self._session.identity.authority_domain_digest,
            "kind": kind.value,
            "object_id": object_id,
            "snapshot_id": snapshot_id,
            "scope_id": scope_id,
            "relative_path": relative_path,
        }
        reference = TaskObjectReference(
            _digest(payload),
            self._session.identity.authority_domain_digest,
            kind,
            object_id,
            snapshot_id,
            scope_id,
            relative_path,
        )
        self._references[reference.reference_id] = reference
        return reference

    def remember_snapshot(self, snapshot_id: str) -> TaskObjectReference:
        resolved = _exact_valid_snapshot(self._session, snapshot_id)
        return self._record(
            TaskObjectKind.SNAPSHOT,
            resolved.snapshot.snapshot_id,
            snapshot_id=resolved.snapshot.snapshot_id,
        )

    def remember_run_from_snapshot(self, snapshot_id: str) -> TaskObjectReference:
        resolved = _exact_valid_snapshot(self._session, snapshot_id)
        return self._record(
            TaskObjectKind.RUN,
            resolved.snapshot.run_id,
            snapshot_id=resolved.snapshot.snapshot_id,
        )

    def remember_run(self, run_id: str) -> TaskObjectReference:
        """Record a Run identity returned by a product operation in this task."""
        _validate_uuid(run_id, "Run")
        return self._record(TaskObjectKind.RUN, run_id)

    def remember_scope(self, scope_id: str) -> TaskObjectReference:
        scope = _scope_by_id(self._session, scope_id)
        return self._record(TaskObjectKind.SCOPE, scope.scope_id, scope_id=scope.scope_id)

    def remember_entry(
        self, snapshot_id: str, scope_id: str, relative_path: str
    ) -> TaskObjectReference:
        _validate_relative_path(relative_path)
        verification, snapshot, page = _verified_snapshot_entries(
            self._session.config,
            snapshot_id,
            scope_id=scope_id,
            path_prefix=relative_path,
            limit=1,
        )
        if verification.status != "VALID":
            raise StewardTaskReferenceError("Entry Snapshot is not authoritatively VALID")
        exact = next(
            (
                item
                for item in page.entries
                if item.scope_id == scope_id and item.relative_path == relative_path
            ),
            None,
        )
        if exact is None or scope_id not in snapshot.scope_ids:
            raise StewardSelectionNotFoundError("task Entry does not exist")
        return self._record(
            TaskObjectKind.ENTRY,
            entry_id(snapshot_id, scope_id, relative_path),
            snapshot_id=snapshot_id,
            scope_id=scope_id,
            relative_path=relative_path,
        )

    def require(self, reference: TaskObjectReference, kind: TaskObjectKind) -> TaskObjectReference:
        if not isinstance(reference, TaskObjectReference) or reference.kind != kind:
            raise StewardTaskReferenceError("task object reference has the wrong type")
        if not hmac.compare_digest(
            reference.authority_domain_digest,
            self._session.identity.authority_domain_digest,
        ):
            raise StewardAuthorityDomainError("task object belongs to another authority domain")
        recorded = self._references.get(reference.reference_id)
        if recorded != reference:
            raise StewardTaskReferenceError("task object was not recorded by this task")
        return recorded

    def reference(self, reference_id: str, kind: TaskObjectKind) -> TaskObjectReference:
        """Resolve one opaque task reference without accepting a caller-fabricated object."""
        if not isinstance(reference_id, str) or not reference_id:
            raise StewardTaskReferenceError("task object reference identity is invalid")
        recorded = self._references.get(reference_id)
        if recorded is None:
            raise StewardTaskReferenceError("task object was not recorded by this task")
        return self.require(recorded, kind)

    def resolve_entry_path(self, reference: TaskObjectReference) -> ResolvedScopedPath:
        recorded = self.require(reference, TaskObjectKind.ENTRY)
        if recorded.scope_id is None or recorded.relative_path is None:
            raise StewardTaskReferenceError("task Entry reference is incomplete")
        resolved = resolve_scoped_path(self._session, recorded.scope_id, recorded.relative_path)
        return ResolvedScopedPath(
            SelectionPolicy.TASK_CREATED,
            PathInputKind.TASK_CREATED_ENTRY,
            resolved.scope_id,
            resolved.relative_path,
        )

    @property
    def reference_count(self) -> int:
        return len(self._references)


def safe_session_identity_payload(session: StewardSession) -> dict[str, str | int]:
    """Return the entire publishable identity; host paths are intentionally absent."""
    if not isinstance(session, StewardSession):
        raise StewardSessionConfigurationError("STEWARD session is invalid")
    return asdict(session.identity)
