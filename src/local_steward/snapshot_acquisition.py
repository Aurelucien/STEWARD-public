"""Supported single-scope Snapshot acquisition and explicit lifecycle recovery."""

from __future__ import annotations

import fcntl
import math
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database import connect, database_path, open_readonly_initialized
from .errors import (
    SnapshotAcquisitionConfirmationError,
    SnapshotAcquisitionCancelledError,
    SnapshotAcquisitionIntegrityError,
    SnapshotAcquisitionNotGovernedError,
    SnapshotAcquisitionRecoveryRequiredError,
    SnapshotBudgetError,
    SnapshotNotFoundError,
    SnapshotScopeError,
    RunNotFoundError,
    StewardError,
    StorageBusyError,
)
from .evidence import load_run_files, write_evidence
from .faults import FaultInjectionError, FaultInjector, checkpoint as fault_checkpoint
from .filesystem import select_scopes
from .models import (
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    RunRecord,
    RunStatus,
    ScanBudget,
    ScopeConfig,
    SnapshotReplacementStatus,
    SnapshotReplayStatus,
    SnapshotStatus,
    SnapshotVerificationResult,
    StewardConfig,
)
from .runs import _evidence, create_run, get_run, transition_run
from .snapshot_lifecycle import (
    SUPPORTED_ACQUISITION_VERSION,
    SUPPORTED_ACQUISITION_WORKFLOW,
    expected_run_status,
    is_supported_acquisition_run,
    scope_binding_digest,
)
from .snapshot_replacement import replace_snapshot_index
from .snapshot_replay import replay_classified_operational_index
from .snapshots import (
    _persist,
    _snapshot,
    snapshot_from_valid_evidence_versioned,
    validate_snapshot_evidence,
    verify_snapshot,
)
from .state_machine import is_terminal
from .storage import _verify_run


@dataclass(frozen=True, slots=True)
class SnapshotAcquisitionRequest:
    scope_id: str
    budget: ScanBudget = ScanBudget()
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class SnapshotAcquisitionReport:
    protocol_version: int
    disposition: str
    governed: bool
    run_id: str
    run_status: str | None
    run_terminal: bool
    snapshot_id: str | None
    snapshot_status: str | None
    evidence_id: str | None
    evidence_relative_path: str | None
    snapshot_digest: str | None
    entry_count: int
    scope_id: str | None
    binding_digest: str | None
    budget: ScanBudget | None
    verification: SnapshotVerificationResult | None
    internal_mutations: tuple[str, ...] = ()
    recovery_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _BoundScope:
    scope: ScopeConfig
    device_id: int
    inode: int
    binding_digest: str
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class _LedgerState:
    run_id: str
    files: tuple[tuple[Path, dict[str, Any]], ...]
    documents: tuple[dict[str, Any], ...]
    run_kind: str
    config_digest: str
    metadata: dict[str, Any]
    status: RunStatus
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2 | None
    snapshot_evidence_id: str | None
    snapshot_relative_path: str | None
    governed: bool


class _AcquisitionLease:
    """Crash-releasing process-independent lease for acquisition/recovery only."""

    def __init__(self, config: StewardConfig) -> None:
        self._path = config.paths.cache_dir / ".snapshot-acquisition.lock"
        self._descriptor: int | None = None

    def __enter__(self) -> "_AcquisitionLease":
        try:
            descriptor = os.open(
                self._path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(descriptor)
                raise
        except OSError as error:
            raise StorageBusyError("another Snapshot acquisition or recovery is active") from error
        self._descriptor = descriptor
        return self

    def __exit__(self, *_args: object) -> None:
        if self._descriptor is not None:
            try:
                fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self._descriptor)
                self._descriptor = None


def _validate_budget(budget: ScanBudget) -> ScanBudget:
    entries = budget.max_entries
    duration = budget.max_duration_seconds
    stat_bytes = budget.max_total_stat_bytes
    depth = budget.max_depth
    if type(entries) is not int or not 1 <= entries <= 1_000_000:
        raise SnapshotBudgetError("max_entries must be an integer between 1 and 1000000")
    if type(duration) not in {int, float} or isinstance(duration, bool):
        raise SnapshotBudgetError("max_duration_seconds must be a finite number")
    if not math.isfinite(float(duration)) or not 0 < float(duration) <= 600:
        raise SnapshotBudgetError("max_duration_seconds must be in (0, 600]")
    if stat_bytes is not None and (type(stat_bytes) is not int or stat_bytes < 0):
        raise SnapshotBudgetError("max_total_stat_bytes must be a nonnegative integer")
    if depth is not None and (type(depth) is not int or depth < 0):
        raise SnapshotBudgetError("max_depth must be a nonnegative integer")
    return ScanBudget(entries, stat_bytes, float(duration), depth)


def _bind_scope(config: StewardConfig, scope_id: str, budget: ScanBudget) -> _BoundScope:
    if not isinstance(scope_id, str) or not scope_id:
        raise SnapshotScopeError("exactly one non-empty scope_id is required")
    scope = select_scopes(config, (scope_id,))[0]
    if scope.allow_cross_mount:
        raise SnapshotScopeError(f"scope cross-mount policy unsupported: {scope.scope_id}")
    root = scope.normalized_path
    try:
        info = root.lstat()
    except OSError as error:
        raise SnapshotScopeError(f"scope root unavailable: {scope.scope_id}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SnapshotScopeError(f"scope root must be a non-symlink directory: {scope.scope_id}")
    if not os.access(root, os.R_OK | os.X_OK):
        raise SnapshotScopeError(f"scope root is not readable: {scope.scope_id}")
    config_digest = _config_digest_for_binding(config)
    binding: dict[str, Any] = {
        "scope_id": scope.scope_id,
        "role": scope.role.value,
        "device_id": info.st_dev,
        "inode": info.st_ino,
        "follow_directory_symlinks": False,
        "allow_cross_mount": False,
    }
    binding["binding_digest"] = scope_binding_digest(config_digest, binding)
    metadata: dict[str, object] = {
        "workflow": SUPPORTED_ACQUISITION_WORKFLOW,
        "workflow_version": SUPPORTED_ACQUISITION_VERSION,
        "scope_binding": binding,
        "scan_budget": {
            "max_entries": budget.max_entries,
            "max_total_stat_bytes": budget.max_total_stat_bytes,
            "max_duration_seconds": budget.max_duration_seconds,
            "max_depth": budget.max_depth,
        },
        "payload_hash_policy": None,
    }
    return _BoundScope(scope, info.st_dev, info.st_ino, binding["binding_digest"], metadata)


def _config_digest_for_binding(config: StewardConfig) -> str:
    # Local import avoids making the public module a second configuration authority.
    from .evidence import compute_config_digest

    return compute_config_digest(config)


def _root_identity_matches(binding: _BoundScope) -> bool:
    try:
        info = binding.scope.normalized_path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(info.st_mode)
        and stat.S_ISDIR(info.st_mode)
        and info.st_dev == binding.device_id
        and info.st_ino == binding.inode
    )


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _writer_handoff(config: StewardConfig) -> None:
    """Leave one self-contained schema-v3 database after a quiescent writer phase."""
    path = database_path(config)
    connection = connect(path)
    try:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise StorageBusyError("Snapshot acquisition WAL checkpoint did not complete")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise SnapshotAcquisitionIntegrityError("derived index integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SnapshotAcquisitionIntegrityError("derived index foreign-key check failed")
    finally:
        connection.close()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.is_symlink():
            raise StorageBusyError("unsafe SQLite sidecar appeared during writer handoff")
        if sidecar.exists():
            sidecar.unlink()
    _fsync_file(path)
    _fsync_directory(path.parent)


def _safe_writer_handoff(config: StewardConfig) -> None:
    try:
        _writer_handoff(config)
    except StewardError:
        pass
    except OSError:
        pass


def _ledger_state(config: StewardConfig, run_id: str) -> _LedgerState:
    try:
        parsed = uuid.UUID(run_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise RunNotFoundError(f"invalid acquisition Run ID: {run_id}") from error
    if parsed.version != 4 or str(parsed) != run_id:
        raise RunNotFoundError(f"invalid acquisition Run ID: {run_id}")
    run_directory = config.paths.evidence_dir / "runs" / run_id
    if not run_directory.is_dir() or run_directory.is_symlink():
        raise RunNotFoundError(f"acquisition Run not found: {run_id}")
    verification = _verify_run(config, run_id, index=False)
    files, failures = load_run_files(config.paths.evidence_dir, run_id)
    if failures or not verification.ledger_valid or not files:
        raise SnapshotAcquisitionIntegrityError(
            f"acquisition Run Evidence is invalid: run_id={run_id}"
        )
    documents = tuple(document for _path, document in files)
    first = documents[0]
    payload = first.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
        raise SnapshotAcquisitionIntegrityError(
            f"acquisition Run metadata is invalid: run_id={run_id}"
        )
    status = RunStatus.CREATED
    snapshots: list[tuple[FilesystemSnapshot | FilesystemSnapshotV2, str, str]] = []
    for path, document in files[1:]:
        evidence_type = document.get("evidence_type")
        if evidence_type == "run.state_transition":
            transition = document.get("payload")
            if not isinstance(transition, dict):
                raise SnapshotAcquisitionIntegrityError(
                    f"acquisition transition is invalid: run_id={run_id}"
                )
            status = RunStatus(transition["to_status"])
        elif evidence_type == "filesystem.snapshot":
            intrinsic = validate_snapshot_evidence(document)
            if not intrinsic.valid:
                raise SnapshotAcquisitionIntegrityError(
                    f"acquisition Snapshot Evidence is invalid: run_id={run_id}"
                )
            evidence_relative = str(path.relative_to(config.paths.evidence_dir))
            snapshots.append(
                (
                    snapshot_from_valid_evidence_versioned(document, evidence_relative),
                    str(document["evidence_id"]),
                    evidence_relative,
                )
            )
    if len(snapshots) > 1:
        raise SnapshotAcquisitionIntegrityError(
            f"acquisition Run contains multiple Snapshots: run_id={run_id}"
        )
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2 | None
    evidence_id: str | None
    relative: str | None
    if snapshots:
        snapshot, evidence_id, relative = snapshots[0]
    else:
        snapshot = evidence_id = relative = None
    return _LedgerState(
        run_id,
        tuple(files),
        documents,
        str(payload["run_kind"]),
        str(first["config_digest"]),
        payload["metadata"],
        status,
        snapshot,
        evidence_id,
        relative,
        is_supported_acquisition_run(documents),
    )


def _state_disposition(state: _LedgerState) -> str:
    if not state.governed:
        return "NOT_GOVERNED"
    if state.status == RunStatus.CREATED:
        return "RECOVERY_REQUIRED_PRE_SCAN"
    if state.status == RunStatus.SCANNING:
        return (
            "RECOVERY_REQUIRED_CLOSE_SNAPSHOT"
            if state.snapshot is not None
            else "RECOVERY_REQUIRED_NO_SNAPSHOT"
        )
    if state.status in {RunStatus.SCANNED, RunStatus.PARTIAL, RunStatus.VERIFYING}:
        return "RECOVERY_REQUIRED_VERIFY"
    if state.status == RunStatus.VERIFIED and state.snapshot is not None:
        return "PARTIAL" if state.snapshot.status == SnapshotStatus.PARTIAL else "COMPLETE"
    if state.status == RunStatus.FAILED:
        return "FAILED"
    if state.status == RunStatus.CANCELLED:
        return "CANCELLED"
    return "INVALID"


def _run_index_matches(config: StewardConfig, state: _LedgerState) -> bool:
    with open_readonly_initialized(config) as connection:
        row = connection.execute(
            "SELECT status,last_sequence,last_evidence_digest FROM runs WHERE run_id=?",
            (state.run_id,),
        ).fetchone()
        return bool(
            row is not None
            and row["status"] == state.status.value
            and row["last_sequence"] == len(state.documents)
            and row["last_evidence_digest"] == state.documents[-1]["evidence_digest"]
        )


def _verification(config: StewardConfig, state: _LedgerState) -> SnapshotVerificationResult | None:
    if state.snapshot is None:
        return None
    try:
        if not _run_index_matches(config, state):
            return None
        return verify_snapshot(config, state.snapshot.snapshot_id)
    except SnapshotNotFoundError:
        return None


def _report(
    config: StewardConfig,
    state: _LedgerState,
    *,
    mutations: tuple[str, ...] = (),
    recovery_actions: tuple[str, ...] = (),
) -> SnapshotAcquisitionReport:
    binding = state.metadata.get("scope_binding") if isinstance(state.metadata, dict) else None
    budget_data = state.metadata.get("scan_budget") if isinstance(state.metadata, dict) else None
    budget = (
        ScanBudget(
            budget_data["max_entries"],
            budget_data["max_total_stat_bytes"],
            budget_data["max_duration_seconds"],
            budget_data["max_depth"],
        )
        if state.governed and isinstance(budget_data, dict)
        else None
    )
    snapshot = state.snapshot
    verification = _verification(config, state)
    disposition = _state_disposition(state)
    terminal_index_mismatch = state.status in {
        RunStatus.VERIFIED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    } and not _run_index_matches(config, state)
    if terminal_index_mismatch or (
        state.status == RunStatus.VERIFIED
        and (verification is None or verification.status != "VALID")
    ):
        disposition = "RECOVERY_REQUIRED_INDEX"
    return SnapshotAcquisitionReport(
        SUPPORTED_ACQUISITION_VERSION,
        disposition,
        state.governed,
        state.run_id,
        state.status.value,
        is_terminal(state.status),
        snapshot.snapshot_id if snapshot is not None else None,
        snapshot.status.value if snapshot is not None else None,
        state.snapshot_evidence_id,
        state.snapshot_relative_path,
        snapshot.snapshot_digest if snapshot is not None else None,
        snapshot.entry_count if snapshot is not None else 0,
        str(binding["scope_id"]) if state.governed and isinstance(binding, dict) else None,
        str(binding["binding_digest"])
        if state.governed and isinstance(binding, dict)
        else None,
        budget,
        verification,
        mutations,
        recovery_actions,
    )


def snapshot_acquisition_status(
    config: StewardConfig, run_id: str
) -> SnapshotAcquisitionReport:
    """Classify one acquisition Run without repair or current-scope access."""
    state = _ledger_state(config, run_id)
    report = _report(config, state)
    confirmed = _ledger_state(config, run_id)
    if tuple(item.get("evidence_digest") for item in state.documents) != tuple(
        item.get("evidence_digest") for item in confirmed.documents
    ):
        raise StorageBusyError("acquisition Evidence changed during status read")
    return report


def _append_transition_from_ledger(
    config: StewardConfig,
    state: _LedgerState,
    target: RunStatus,
    reason: str,
    *,
    fault_injector: FaultInjector | None,
) -> None:
    run = RunRecord(
        state.run_id,
        state.run_kind,
        state.status,
        str(state.documents[0]["created_at"]),
        str(state.documents[-1]["created_at"]),
        state.config_digest,
        state.metadata,
        len(state.documents),
        str(state.documents[-1]["evidence_digest"]),
        is_terminal(state.status),
    )
    operation = f"acquisition.recovery.append.{target.value}"
    item = _evidence(
        run,
        "run.state_transition",
        {
            "from_status": state.status.value,
            "to_status": target.value,
            "reason": reason,
        },
    )
    fault_checkpoint(fault_injector, operation, "before_evidence_publish")
    write_evidence(config.paths.evidence_dir, item)
    fault_checkpoint(fault_injector, operation, "after_evidence_publish")


def _remove_candidate(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        try:
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink()
        except OSError:
            pass


def _rebuild_index(
    config: StewardConfig, *, fault_injector: FaultInjector | None
) -> tuple[str, ...]:
    candidate = config.paths.cache_dir / f".snapshot-acquisition-replay-{uuid.uuid4()}.db"
    fault_checkpoint(fault_injector, "acquisition.recovery.rebuild", "before_candidate")
    replay = replay_classified_operational_index(config, candidate)
    if replay.status != SnapshotReplayStatus.READY or not replay.candidate_ready:
        _remove_candidate(candidate)
        raise SnapshotAcquisitionIntegrityError(
            "classified acquisition recovery candidate was not ready"
        )
    fault_checkpoint(
        fault_injector, "acquisition.recovery.rebuild", "after_candidate_validation"
    )
    replacement = replace_snapshot_index(config, replay, fault_injector=fault_injector)
    if replacement.status != SnapshotReplacementStatus.REPLACED:
        _remove_candidate(candidate)
        _safe_writer_handoff(config)
        raise SnapshotAcquisitionIntegrityError(
            "classified acquisition recovery replacement failed"
        )
    fault_checkpoint(fault_injector, "acquisition.recovery.rebuild", "after_replace")
    _writer_handoff(config)
    return ("derived_index_backup", "classified_candidate", "derived_index_replace")


def _ensure_verified_projection(
    config: StewardConfig,
    state: _LedgerState,
    *,
    fault_injector: FaultInjector | None,
) -> tuple[SnapshotVerificationResult, tuple[str, ...]]:
    if state.snapshot is None:
        raise SnapshotAcquisitionIntegrityError(
            f"acquisition Snapshot is unavailable: run_id={state.run_id}"
        )
    verification = _verification(config, state)
    if verification is not None and verification.status == "VALID":
        return verification, ()
    mutations = _rebuild_index(config, fault_injector=fault_injector)
    refreshed = _ledger_state(config, state.run_id)
    verification = _verification(config, refreshed)
    if verification is None or verification.status != "VALID":
        raise SnapshotAcquisitionIntegrityError(
            f"acquisition Snapshot verification failed: run_id={state.run_id}"
        )
    return verification, mutations


def _acquire_snapshot(
    config: StewardConfig,
    request: SnapshotAcquisitionRequest,
    *,
    fault_injector: FaultInjector | None,
) -> SnapshotAcquisitionReport:
    if request.confirmed is not True:
        raise SnapshotAcquisitionConfirmationError(
            "Snapshot acquisition requires explicit confirmation"
        )
    budget = _validate_budget(request.budget)
    binding = _bind_scope(config, request.scope_id, budget)
    run_id = str(uuid.uuid4())
    snapshot_id: str | None = None
    mutations: list[str] = []
    with _AcquisitionLease(config):
        try:
            fault_checkpoint(fault_injector, "acquisition", "before_run_create")
            create_run(
                config,
                "filesystem.snapshot",
                binding.metadata,
                _run_id=run_id,
                _fault_injector=fault_injector,
            )
            mutations.extend(("run.created Evidence", "Run index"))
            transition_run(
                config,
                run_id,
                RunStatus.SCANNING,
                "supported Snapshot acquisition started",
                _fault_injector=fault_injector,
            )
            mutations.extend(("scanning transition Evidence", "Run index"))
            fault_checkpoint(fault_injector, "acquisition", "before_scope_observation")
            current = get_run(config, run_id)
            snapshot = _snapshot(config, current, (binding.scope,), budget)
            snapshot_id = snapshot.snapshot_id
            fault_checkpoint(fault_injector, "acquisition", "after_scope_observation")
            if not _root_identity_matches(binding):
                raise SnapshotAcquisitionRecoveryRequiredError(
                    f"scope root identity changed; recovery required: run_id={run_id}"
                )
            _persist(config, snapshot, _fault_injector=fault_injector)
            mutations.extend(("Snapshot Evidence", "Snapshot/Entry index"))
            target = expected_run_status(snapshot.status)
            if target is None:
                raise SnapshotAcquisitionIntegrityError(
                    f"unsupported Snapshot status: run_id={run_id} snapshot_id={snapshot_id}"
                )
            transition_run(
                config,
                run_id,
                target,
                "supported Snapshot observation persisted",
                _fault_injector=fault_injector,
            )
            mutations.extend((f"{target.value} transition Evidence", "Run index"))
            _writer_handoff(config)
            fault_checkpoint(fault_injector, "acquisition", "before_authoritative_verify")
            verification = verify_snapshot(config, snapshot_id)
            if verification.status != "VALID":
                raise SnapshotAcquisitionIntegrityError(
                    f"authoritative Snapshot verification failed: run_id={run_id} snapshot_id={snapshot_id}"
                )
            fault_checkpoint(fault_injector, "acquisition", "after_authoritative_verify")
            transition_run(
                config,
                run_id,
                RunStatus.VERIFYING,
                "supported Snapshot verification started",
                _fault_injector=fault_injector,
            )
            mutations.extend(("verifying transition Evidence", "Run index"))
            transition_run(
                config,
                run_id,
                RunStatus.VERIFIED,
                "supported Snapshot verification passed",
                _fault_injector=fault_injector,
            )
            mutations.extend(("verified transition Evidence", "Run index"))
            _writer_handoff(config)
            fault_checkpoint(fault_injector, "acquisition", "before_result_publication")
            state = _ledger_state(config, run_id)
            report = _report(config, state, mutations=tuple(mutations))
            if report.disposition not in {"COMPLETE", "PARTIAL"} or (
                report.verification is None or report.verification.status != "VALID"
            ):
                raise SnapshotAcquisitionIntegrityError(
                    f"terminal acquisition publication gate failed: run_id={run_id} snapshot_id={snapshot_id}"
                )
            return report
        except FaultInjectionError as error:
            _safe_writer_handoff(config)
            if (config.paths.evidence_dir / "runs" / run_id).exists():
                raise SnapshotAcquisitionRecoveryRequiredError(
                    f"durable acquisition requires explicit recovery: run_id={run_id}"
                    + (f" snapshot_id={snapshot_id}" if snapshot_id else "")
                ) from error
            raise SnapshotAcquisitionRecoveryRequiredError(
                "acquisition was interrupted before durable Run publication"
            ) from error
        except KeyboardInterrupt as error:
            _safe_writer_handoff(config)
            run_directory = config.paths.evidence_dir / "runs" / run_id
            if not run_directory.is_dir():
                raise SnapshotAcquisitionCancelledError(
                    "Snapshot acquisition cancelled before durable Run publication"
                ) from error
            try:
                state = _ledger_state(config, run_id)
                if state.snapshot is None and state.status in {
                    RunStatus.CREATED,
                    RunStatus.SCANNING,
                }:
                    _append_transition_from_ledger(
                        config,
                        state,
                        RunStatus.CANCELLED,
                        "operator cancelled before durable Snapshot publication",
                        fault_injector=None,
                    )
                    _rebuild_index(config, fault_injector=None)
                    raise SnapshotAcquisitionCancelledError(
                        f"Snapshot acquisition cancelled: run_id={run_id}"
                    ) from error
            except SnapshotAcquisitionCancelledError:
                raise
            except StewardError:
                pass
            raise SnapshotAcquisitionRecoveryRequiredError(
                f"cancelled acquisition has durable state requiring recovery: run_id={run_id}"
                + (f" snapshot_id={snapshot_id}" if snapshot_id else "")
            ) from error
        except SnapshotAcquisitionIntegrityError:
            _safe_writer_handoff(config)
            raise
        except SnapshotAcquisitionRecoveryRequiredError:
            _safe_writer_handoff(config)
            raise
        except StewardError as error:
            _safe_writer_handoff(config)
            if (config.paths.evidence_dir / "runs" / run_id).exists():
                raise SnapshotAcquisitionRecoveryRequiredError(
                    f"durable acquisition requires explicit recovery: run_id={run_id}"
                    + (f" snapshot_id={snapshot_id}" if snapshot_id else "")
                ) from error
            raise
        except Exception as error:
            _safe_writer_handoff(config)
            if (config.paths.evidence_dir / "runs" / run_id).exists():
                raise SnapshotAcquisitionRecoveryRequiredError(
                    f"durable acquisition requires explicit recovery: run_id={run_id}"
                    + (f" snapshot_id={snapshot_id}" if snapshot_id else "")
                ) from error
            raise


def acquire_snapshot(
    config: StewardConfig, request: SnapshotAcquisitionRequest
) -> SnapshotAcquisitionReport:
    """Acquire one metadata-only Snapshot; no test/runtime injection is public."""
    return _acquire_snapshot(config, request, fault_injector=None)


def _recover_snapshot_acquisition(
    config: StewardConfig,
    run_id: str,
    *,
    confirmed: bool,
    fault_injector: FaultInjector | None,
) -> SnapshotAcquisitionReport:
    if confirmed is not True:
        raise SnapshotAcquisitionConfirmationError(
            "Snapshot acquisition recovery requires explicit confirmation"
        )
    preflight = _ledger_state(config, run_id)
    if not preflight.governed:
        raise SnapshotAcquisitionNotGovernedError(
            f"Run is not governed by supported acquisition v1: run_id={run_id}"
        )
    mutations: list[str] = []
    actions: list[str] = []
    with _AcquisitionLease(config):
        state = _ledger_state(config, run_id)
        if not state.governed:
            raise SnapshotAcquisitionIntegrityError(
                f"acquisition authority changed during recovery: run_id={run_id}"
            )
        try:
            if state.status == RunStatus.CREATED:
                _append_transition_from_ledger(
                    config,
                    state,
                    RunStatus.FAILED,
                    "recovery closed acquisition before scanning",
                    fault_injector=fault_injector,
                )
                mutations.append("failed transition Evidence")
                actions.append("close_pre_scan_as_failed")
                mutations.extend(_rebuild_index(config, fault_injector=fault_injector))
                return _report(
                    config,
                    _ledger_state(config, run_id),
                    mutations=tuple(mutations),
                    recovery_actions=tuple(actions),
                )
            if state.status == RunStatus.SCANNING and state.snapshot is None:
                _append_transition_from_ledger(
                    config,
                    state,
                    RunStatus.FAILED,
                    "recovery closed acquisition with no durable Snapshot",
                    fault_injector=fault_injector,
                )
                mutations.append("failed transition Evidence")
                actions.append("close_no_snapshot_as_failed")
                mutations.extend(_rebuild_index(config, fault_injector=fault_injector))
                return _report(
                    config,
                    _ledger_state(config, run_id),
                    mutations=tuple(mutations),
                    recovery_actions=tuple(actions),
                )
            if state.status == RunStatus.SCANNING and state.snapshot is not None:
                target = expected_run_status(state.snapshot.status)
                if target is None:
                    raise SnapshotAcquisitionIntegrityError(
                        f"recovery cannot infer Snapshot transition: run_id={run_id}"
                    )
                _append_transition_from_ledger(
                    config,
                    state,
                    target,
                    "recovery closed durable Snapshot observation",
                    fault_injector=fault_injector,
                )
                mutations.append(f"{target.value} transition Evidence")
                actions.append("close_durable_snapshot")
                state = _ledger_state(config, run_id)
            if state.status in {
                RunStatus.SCANNED,
                RunStatus.PARTIAL,
                RunStatus.VERIFYING,
                RunStatus.VERIFIED,
            }:
                verification, projection_mutations = _ensure_verified_projection(
                    config, state, fault_injector=fault_injector
                )
                mutations.extend(projection_mutations)
                if projection_mutations:
                    actions.append("rebuild_derived_index")
                if verification.status != "VALID":
                    raise SnapshotAcquisitionIntegrityError(
                        f"recovery verification failed: run_id={run_id}"
                    )
                state = _ledger_state(config, run_id)
                if state.status in {RunStatus.SCANNED, RunStatus.PARTIAL}:
                    transition_run(
                        config,
                        run_id,
                        RunStatus.VERIFYING,
                        "recovery verification started",
                        _fault_injector=fault_injector,
                    )
                    mutations.extend(("verifying transition Evidence", "Run index"))
                    actions.append("resume_verification")
                    state = _ledger_state(config, run_id)
                if state.status == RunStatus.VERIFYING:
                    transition_run(
                        config,
                        run_id,
                        RunStatus.VERIFIED,
                        "recovery verification passed",
                        _fault_injector=fault_injector,
                    )
                    mutations.extend(("verified transition Evidence", "Run index"))
                    actions.append("close_verified")
                _writer_handoff(config)
                final = _ledger_state(config, run_id)
                report = _report(
                    config,
                    final,
                    mutations=tuple(mutations),
                    recovery_actions=tuple(actions),
                )
                if report.disposition not in {"COMPLETE", "PARTIAL"} or (
                    report.verification is None or report.verification.status != "VALID"
                ):
                    raise SnapshotAcquisitionIntegrityError(
                        f"recovery terminal publication gate failed: run_id={run_id}"
                    )
                return report
            if state.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
                if not _run_index_matches(config, state):
                    mutations.extend(_rebuild_index(config, fault_injector=fault_injector))
                    actions.append("rebuild_derived_index")
                    state = _ledger_state(config, run_id)
                return _report(
                    config,
                    state,
                    mutations=tuple(mutations),
                    recovery_actions=tuple(actions),
                )
            raise SnapshotAcquisitionIntegrityError(
                f"acquisition recovery state is unsupported: run_id={run_id} status={state.status.value}"
            )
        except FaultInjectionError as error:
            _safe_writer_handoff(config)
            raise SnapshotAcquisitionRecoveryRequiredError(
                f"recovery was interrupted and remains explicit: run_id={run_id}"
            ) from error


def recover_snapshot_acquisition(
    config: StewardConfig, run_id: str, *, confirmed: bool = False
) -> SnapshotAcquisitionReport:
    """Explicitly close one governed durable prefix; never scans the user scope."""
    return _recover_snapshot_acquisition(
        config, run_id, confirmed=confirmed, fault_injector=None
    )
