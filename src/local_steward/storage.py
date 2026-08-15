"""Status, ledger verification, and derived-index rebuild operations."""

import hashlib
import json
import shutil
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

from . import __version__
from .database import (
    database_path,
    initialize,
    migrate_v1_to_v2,
    open_initialized,
    open_readonly_initialized,
)
from .errors import (
    EvidenceError,
    RunNotFoundError,
    StewardError,
    StorageCorruptionError,
    StorageError,
    StorageNotInitializedError,
)
from .evidence import canonical_json, digest, filename, load_run_files, utc_now
from .models import (
    ClassifiedReplayPlan,
    ClassifiedSnapshotEntryAccountingItem,
    ClassifiedSnapshotReplayItem,
    EvidenceIntegrityStatus,
    EvidenceVerificationResult,
    EvidenceVerificationReport,
    ReplayEligibility,
    RunEvidenceAccountingItem,
    RunStatus,
    SemanticConsistencyStatus,
    SnapshotEvidenceVerificationItem,
    SnapshotEvidenceVerificationReport,
    SnapshotStorageIntegrityReport,
    SnapshotStorageIntegrityStatus,
    SnapshotStatus,
    StewardConfig,
    StorageStatus,
)
from .snapshots import (
    _insert_snapshot_index_rows,
    _inspect_snapshot_inventory,
    classify_snapshot_inventory,
    snapshot_evidence_schema_version,
    snapshot_from_valid_evidence_versioned,
    snapshot_v2_from_valid_evidence,
    validate_snapshot_evidence,
    validate_snapshot_reuse_references,
)
from .runs import _get_run, get_run
from .state_machine import is_terminal, validate_transition
from .snapshot_lifecycle import is_supported_acquisition_run, snapshot_run_compatible


_STORAGE_STATUS_SEVERITY = {
    "HEALTHY": 0,
    "DEGRADED": 1,
    "INCONSISTENT": 2,
    "UNINITIALIZED": 3,
    "CORRUPT": 4,
}


def _compose_storage_status(*statuses: str) -> str:
    """Choose the highest existing storage severity; unknown inputs are unsafe."""
    return max(
        statuses,
        key=lambda status: _STORAGE_STATUS_SEVERITY.get(
            status, _STORAGE_STATUS_SEVERITY["INCONSISTENT"]
        ),
    )


def _snapshot_storage_status(report: SnapshotStorageIntegrityReport) -> str:
    if report.status == SnapshotStorageIntegrityStatus.HEALTHY:
        return "HEALTHY"
    if report.status == SnapshotStorageIntegrityStatus.DEGRADED:
        return "DEGRADED"
    return "INCONSISTENT"


def _snapshot_storage_issues(report: SnapshotStorageIntegrityReport) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "code": issue["code"],
            "message": f"Snapshot integrity issue: {issue['code']}",
            "severity": issue["severity"],
            "snapshot_id": issue["snapshot_id"],
            "evidence_id": issue["evidence_id"],
            "persistent_run_id": issue["persistent_run_id"],
            "path": issue["evidence_relative_path"],
        }
        for issue in report.issues
    )


def _operational_inventory_assessment(
    inventory: Any, plan: ClassifiedReplayPlan
) -> tuple[str, tuple[dict[str, str], ...], tuple[dict[str, str], ...]]:
    """Compare one derived index with the frozen classified eligible Snapshot set."""
    blocking: list[dict[str, str]] = []
    diagnostics: list[dict[str, str]] = []
    inventory_by_evidence = {
        item.evidence_id: item for item in inventory.items if item.evidence_id is not None
    }
    planned_ids = {item.evidence_id for item in plan.snapshots if item.evidence_id is not None}
    for item in plan.snapshots:
        identity = {
            "snapshot_id": item.snapshot_id or "",
            "evidence_id": item.evidence_id or "",
            "persistent_run_id": item.persistent_run_id or "",
            "path": item.evidence_relative_path or "",
        }
        indexed = inventory_by_evidence.get(item.evidence_id)
        if item.replay_eligibility == ReplayEligibility.INELIGIBLE:
            diagnostics.append(
                _classified_issue(
                    "SNAPSHOT_RUN_STATUS_INVALID",
                    "historical Snapshot is replay-ineligible under classified operational replay",
                    **identity,
                )
            )
            if indexed is not None and (indexed.index_present or indexed.indexed_entry_count):
                blocking.append(
                    _classified_issue(
                        "OPERATIONAL_REPLAY_INELIGIBLE_ROWS_PRESENT",
                        "replay-ineligible Snapshot or Entry rows exist in the derived index",
                        **identity,
                    )
                )
        elif item.replay_eligibility == ReplayEligibility.ELIGIBLE:
            if (
                indexed is None
                or not indexed.index_present
                or indexed.indexed_entry_count != item.entry_count
                or set(indexed.issue_codes)
            ):
                blocking.append(
                    _classified_issue(
                        "OPERATIONAL_REPLAY_ELIGIBLE_ROWS_INCOMPLETE",
                        "eligible Snapshot business rows are missing or inconsistent",
                        **identity,
                    )
                )
    for item in inventory.items:
        if item.evidence_id not in planned_ids and (item.index_present or item.indexed_entry_count):
            blocking.append(
                _classified_issue(
                    "OPERATIONAL_REPLAY_UNACCOUNTED_INDEX_ROWS",
                    "derived Snapshot rows are not represented in classified accounting",
                    snapshot_id=item.snapshot_id or "",
                    evidence_id=item.evidence_id or "",
                    persistent_run_id=item.persistent_run_id or "",
                    path=item.evidence_relative_path or "",
                )
            )
    if (
        inventory.indexed_snapshots != plan.eligible_snapshot_count
        or inventory.indexed_entry_count != plan.eligible_entry_count
    ):
        blocking.append(
            _classified_issue(
                "OPERATIONAL_REPLAY_INDEX_COUNT_MISMATCH",
                "derived Snapshot counts differ from the classified eligible set",
            )
        )
    ordered_blocking = tuple(
        sorted(
            blocking,
            key=lambda issue: (
                issue["code"],
                issue["snapshot_id"],
                issue["evidence_id"],
                issue["persistent_run_id"],
            ),
        )
    )
    ordered_diagnostics = tuple(
        sorted(
            diagnostics,
            key=lambda issue: (
                issue["code"], issue["snapshot_id"], issue["evidence_id"]
            ),
        )
    )
    return ("HEALTHY" if not ordered_blocking else "INCONSISTENT"), ordered_blocking, ordered_diagnostics


def initialize_storage(config: StewardConfig) -> None:
    for path in (config.paths.data_dir, config.paths.evidence_dir, config.paths.cache_dir):
        if not path.is_dir():
            raise StorageNotInitializedError(f"required directory is missing: {path}")
    initialize(database_path(config), __version__, utc_now())


def migrate_storage(config: StewardConfig) -> bool:
    """Back up and explicitly migrate only the derived SQLite v1 index."""
    source = database_path(config)
    if not source.is_file():
        raise StorageNotInitializedError("storage is not initialized")
    backup = config.paths.cache_dir / f"state-before-migration-{uuid.uuid4()}.db"
    shutil.copy2(source, backup)
    return migrate_v1_to_v2(source)


def _verify_run(
    config: StewardConfig,
    run_id: str,
    *,
    index: bool = True,
    connection: sqlite3.Connection | None = None,
) -> EvidenceVerificationResult:
    files, errors = load_run_files(config.paths.evidence_dir, run_id)
    previous: str | None = None
    status = RunStatus.CREATED
    seen_ids: set[str] = set()
    seen_digests: set[str] = set()
    if not errors:
        for expected, (path, item) in enumerate(files, 1):
            if (
                not isinstance(item, dict)
                or item.get("run_id") != run_id
                or item.get("sequence") != expected
                or path.name[:8] != f"{expected:08d}"
            ):
                errors.append(f"sequence/path mismatch: {path.name}")
                continue
            required = {
                "schema_version",
                "evidence_id",
                "evidence_type",
                "run_id",
                "sequence",
                "created_at",
                "tool_version",
                "config_digest",
                "policy_digest",
                "provider_versions",
                "previous_evidence_digest",
                "payload",
                "evidence_digest",
            }
            version = snapshot_evidence_schema_version(item)
            if set(item) != required or version is None:
                errors.append(f"invalid evidence schema: {path.name}")
                continue
            if version not in {1, 2}:
                errors.append(f"unsupported evidence schema version: {path.name}")
                continue
            if version == 2 and item.get("evidence_type") != "filesystem.snapshot":
                errors.append(f"invalid evidence schema: {path.name}")
                continue
            if item["evidence_type"] not in {
                "run.created",
                "run.state_transition",
                "filesystem.snapshot",
                "filesystem.snapshot_diff",
            }:
                errors.append(f"unknown evidence type: {path.name}")
                continue
            if path.name != filename(expected, item["evidence_type"]):
                errors.append(f"evidence type/path mismatch: {path.name}")
                continue
            try:
                parsed_time = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                evidence_uuid = uuid.UUID(item["evidence_id"])
            except (TypeError, ValueError):
                errors.append(f"invalid evidence identity/time: {path.name}")
                continue
            if parsed_time.tzinfo is None or evidence_uuid.version != 4:
                errors.append(f"invalid evidence identity/time: {path.name}")
                continue
            if not isinstance(item["payload"], dict) or not isinstance(
                item["provider_versions"], dict
            ):
                errors.append(f"invalid evidence payload: {path.name}")
                continue
            if item.get("previous_evidence_digest") != previous or item.get(
                "evidence_digest"
            ) != digest(item):
                errors.append(f"digest chain invalid: {path.name}")
            if item.get("evidence_id") in seen_ids or item.get("evidence_digest") in seen_digests:
                errors.append(f"duplicate evidence identity: {path.name}")
            seen_ids.add(str(item.get("evidence_id")))
            seen_digests.add(str(item.get("evidence_digest")))
            previous = item.get("evidence_digest")
            if expected == 1:
                if (
                    item.get("evidence_type") != "run.created"
                    or item.get("payload", {}).get("initial_status") != "created"
                ):
                    errors.append("invalid run.created record")
            elif item["evidence_type"] == "run.state_transition":
                payload = item.get("payload", {})
                if payload.get("from_status") != status.value:
                    errors.append(f"transition source mismatch: {path.name}")
                try:
                    validate_transition(status, RunStatus(payload.get("to_status")))
                except Exception:
                    errors.append(f"invalid transition: {path.name}")
                status = (
                    RunStatus(payload.get("to_status", status.value))
                    if payload.get("to_status") in RunStatus._value2member_map_
                    else status
                )
    ledger_valid = bool(files) and not errors
    index_consistent = True
    if index and ledger_valid:
        try:
            conn = connection or open_initialized(config)
            row = conn.execute(
                "SELECT status,last_sequence,last_evidence_digest FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if connection is None:
                conn.close()
            index_consistent = (
                row is not None
                and row["status"] == status.value
                and row["last_sequence"] == len(files)
                and row["last_evidence_digest"] == previous
            )
            if row is not None and row["last_sequence"] > len(files):
                errors.append("evidence chain is incomplete relative to indexed sequence")
                ledger_valid = False
        except Exception:
            index_consistent = False
    return EvidenceVerificationResult(
        run_id,
        "VALID"
        if ledger_valid and index_consistent
        else "INCOMPLETE"
        if ledger_valid
        else "INVALID",
        ledger_valid,
        index_consistent,
        tuple(errors),
        len(files),
    )


def _verification_run_ids(config: StewardConfig, run_id: str | None) -> list[str]:
    runs_dir = config.paths.evidence_dir / "runs"
    if run_id:
        return [run_id]
    if not runs_dir.exists():
        return []
    try:
        return sorted(
            path.name for path in runs_dir.iterdir() if path.is_dir() and not path.is_symlink()
        )
    except OSError as error:
        raise EvidenceError(f"unable to read evidence ledger: {error}") from error


def _snapshot_problem(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _append_snapshot_problem(record: dict[str, Any], code: str, message: str) -> None:
    if all(problem["code"] != code for problem in record["errors"]):
        record["errors"].append(_snapshot_problem(code, message))


def _snapshot_evidence_report(
    config: StewardConfig,
    run_ids: list[str],
    *,
    connection: sqlite3.Connection | None = None,
) -> SnapshotEvidenceVerificationReport:
    """Validate Snapshot Evidence and Run facts without reading Snapshot derived indices."""
    records: list[dict[str, Any]] = []
    run_cache: dict[str, Any] = {}
    for run_id in run_ids:
        try:
            files, failures = load_run_files(config.paths.evidence_dir, run_id)
        except OSError as error:
            raise EvidenceError(f"unable to read evidence ledger: {error}") from error
        for path, document in files:
            relative_path = str(path.relative_to(config.paths.evidence_dir))
            if not isinstance(document, dict):
                if path.name.endswith("_filesystem.snapshot.json"):
                    records.append(
                        {
                            "evidence_id": None,
                            "snapshot_id": None,
                            "run_id": run_id,
                            "relative_path": relative_path,
                            "schema_valid": False,
                            "digest_valid": False,
                            "payload_valid": False,
                            "snapshot_valid": False,
                            "run_present": False,
                            "run_kind_valid": False,
                            "run_status_valid": False,
                            "errors": [
                                _snapshot_problem(
                                    "SNAPSHOT_EVIDENCE_INVALID", "snapshot evidence is not a JSON object"
                                )
                            ],
                        }
                    )
                continue
            if document.get("evidence_type") != "filesystem.snapshot":
                continue
            intrinsic = validate_snapshot_evidence(document)
            errors = list(intrinsic.errors)
            if not intrinsic.valid:
                errors.append(
                    _snapshot_problem(
                        "SNAPSHOT_EVIDENCE_INVALID", "snapshot evidence failed intrinsic validation"
                    )
                )
            payload = document.get("payload")
            snapshot = payload.get("snapshot", {}) if isinstance(payload, dict) else {}
            snapshot_status = snapshot.get("status") if isinstance(snapshot, dict) else None
            persistent_run_id = document.get("run_id") if isinstance(document.get("run_id"), str) else run_id
            record: dict[str, Any] = {
                "evidence_id": intrinsic.evidence_id,
                "snapshot_id": intrinsic.snapshot_id,
                "run_id": persistent_run_id,
                "relative_path": relative_path,
                "schema_valid": intrinsic.envelope_valid,
                "digest_valid": intrinsic.evidence_digest_valid,
                "payload_valid": intrinsic.payload_schema_valid,
                "snapshot_valid": intrinsic.valid,
                "run_present": False,
                "run_kind_valid": False,
                "run_status_valid": False,
                "errors": errors,
                "evidence_digest": document.get("evidence_digest"),
            }
            if intrinsic.valid and snapshot_evidence_schema_version(document) == 2:
                reused = snapshot_v2_from_valid_evidence(document, relative_path)
                for issue in validate_snapshot_reuse_references(
                    config, reused, connection=connection
                ):
                    _append_snapshot_problem(record, issue["code"], issue["message"])
            if persistent_run_id:
                try:
                    run = run_cache.get(persistent_run_id)
                    if run is None:
                        run = (
                            _get_run(connection, persistent_run_id)
                            if connection is not None
                            else get_run(config, persistent_run_id)
                        )
                        run_cache[persistent_run_id] = run
                    record["run_present"] = True
                    record["run_kind_valid"] = run.run_kind == "filesystem.snapshot"
                    if not record["run_kind_valid"]:
                        _append_snapshot_problem(
                            record,
                            "SNAPSHOT_RUN_KIND_INVALID",
                            "Snapshot Evidence Run kind is not filesystem.snapshot",
                        )
                    record["run_status_valid"] = snapshot_run_compatible(
                        snapshot_status,
                        run.run_kind,
                        run.status,
                        (item for _item_path, item in files),
                    )
                    if not record["run_status_valid"]:
                        _append_snapshot_problem(
                            record,
                            "SNAPSHOT_RUN_STATUS_INVALID",
                            "Snapshot Evidence Run status is incompatible with Snapshot status",
                        )
                except RunNotFoundError:
                    _append_snapshot_problem(
                        record, "SNAPSHOT_RUN_MISSING", "Snapshot Evidence Run is missing"
                    )
                except (TypeError, ValueError):
                    _append_snapshot_problem(
                        record,
                        "SNAPSHOT_RUN_STATUS_INVALID",
                        "Snapshot Evidence Run status is invalid",
                    )
                except StorageError:
                    raise
            else:
                _append_snapshot_problem(
                    record, "SNAPSHOT_RUN_MISSING", "Snapshot Evidence Run is missing"
                )
            records.append(record)
        for failure in failures:
            name = failure.partition(": ")[2]
            if name.endswith("_filesystem.snapshot.json"):
                records.append(
                    {
                        "evidence_id": None,
                        "snapshot_id": None,
                        "run_id": run_id,
                        "relative_path": f"runs/{run_id}/{name}",
                        "schema_valid": False,
                        "digest_valid": False,
                        "payload_valid": False,
                        "snapshot_valid": False,
                        "run_present": False,
                        "run_kind_valid": False,
                        "run_status_valid": False,
                        "errors": [
                            _snapshot_problem(
                                "SNAPSHOT_EVIDENCE_INVALID", "snapshot evidence cannot be read"
                            )
                        ],
                    }
                )
    for key, code, message in (
        ("snapshot_id", "SNAPSHOT_ID_DUPLICATE", "multiple Snapshot Evidence share one Snapshot ID"),
        ("run_id", "SNAPSHOT_RUN_DUPLICATE", "multiple Snapshot Evidence share one Run"),
        ("evidence_id", "SNAPSHOT_EVIDENCE_ID_CONFLICT", "multiple Snapshot Evidence share one Evidence ID"),
    ):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            value = record.get(key)
            if isinstance(value, str) and value:
                grouped.setdefault(value, []).append(record)
        for grouped_records in grouped.values():
            if len(grouped_records) > 1:
                for record in grouped_records:
                    _append_snapshot_problem(record, code, message)
                if key == "evidence_id" and len(
                    {record.get("evidence_digest") for record in grouped_records}
                ) > 1:
                    for record in grouped_records:
                        _append_snapshot_problem(
                            record,
                            "SNAPSHOT_EVIDENCE_CONTENT_CONFLICT",
                            "one Evidence ID has conflicting Snapshot Evidence content",
                        )
    items: list[SnapshotEvidenceVerificationItem] = []
    issues: list[dict[str, str]] = []
    for record in records:
        record_errors: tuple[dict[str, str], ...] = tuple(
            sorted(record["errors"], key=lambda item: (item["code"], item["message"]))
        )
        valid = not record_errors
        item = SnapshotEvidenceVerificationItem(
            record.get("evidence_id"),
            record.get("snapshot_id"),
            record.get("run_id"),
            "filesystem.snapshot",
            bool(record["schema_valid"]),
            bool(record["digest_valid"]),
            bool(record["payload_valid"]),
            bool(record["snapshot_valid"]),
            bool(record["run_present"]),
            bool(record["run_kind_valid"]),
            bool(record["run_status_valid"]),
            valid,
            record.get("relative_path"),
            record_errors,
        )
        items.append(item)
        for problem in record_errors:
            issues.append(
                {
                    "code": problem["code"],
                    "message": problem["message"],
                    "evidence_id": item.evidence_id or "",
                    "snapshot_id": item.snapshot_id or "",
                    "persistent_run_id": item.persistent_run_id or "",
                    "evidence_relative_path": item.evidence_relative_path or "",
                }
            )
    ordered_items = tuple(
        sorted(
            items,
            key=lambda item: (
                item.snapshot_id is None,
                item.snapshot_id or "",
                item.evidence_id or "",
                item.persistent_run_id or "",
            ),
        )
    )
    ordered_issues = tuple(
        sorted(
            issues,
            key=lambda issue: (
                issue["code"],
                issue["snapshot_id"],
                issue["evidence_id"],
                issue["persistent_run_id"],
                issue["evidence_relative_path"],
            ),
        )
    )
    invalid_items = [item for item in ordered_items if not item.valid]
    return SnapshotEvidenceVerificationReport(
        len(ordered_items),
        len(ordered_items) - len(invalid_items),
        len(invalid_items),
        len({item.snapshot_id for item in ordered_items if "SNAPSHOT_ID_DUPLICATE" in {error["code"] for error in item.errors}}),
        len({item.persistent_run_id for item in ordered_items if "SNAPSHOT_RUN_DUPLICATE" in {error["code"] for error in item.errors}}),
        len({item.persistent_run_id for item in ordered_items if "SNAPSHOT_RUN_MISSING" in {error["code"] for error in item.errors}}),
        len({item.persistent_run_id for item in ordered_items if {error["code"] for error in item.errors} & {"SNAPSHOT_RUN_KIND_INVALID", "SNAPSHOT_RUN_STATUS_INVALID"}}),
        ordered_items,
        ordered_issues,
    )


def verify_evidence_report(
    config: StewardConfig,
    run_id: str | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> EvidenceVerificationReport:
    """Verify generic ledger rules plus Snapshot Evidence and persistent Run facts."""
    run_ids = _verification_run_ids(config, run_id)
    results = [_verify_run(config, value, connection=connection) for value in run_ids]
    snapshot_evidence = _snapshot_evidence_report(config, run_ids, connection=connection)
    invalid_runs = {
        item.persistent_run_id for item in snapshot_evidence.items if not item.valid and item.persistent_run_id
    }
    adjusted = tuple(
        replace(
            result,
            status="INVALID",
            ledger_valid=False,
            errors=tuple(sorted(set(result.errors) | {"snapshot evidence validation failed"})),
        )
        if result.run_id in invalid_runs
        else result
        for result in results
    )
    return EvidenceVerificationReport(adjusted, snapshot_evidence)


def verify_evidence(
    config: StewardConfig,
    run_id: str | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> list[EvidenceVerificationResult]:
    """Compatibility view of global Evidence verification results by persistent Run."""
    return list(verify_evidence_report(config, run_id, connection=connection).verifications)


def _classified_issue(
    code: str,
    message: str,
    *,
    snapshot_id: str = "",
    evidence_id: str = "",
    persistent_run_id: str = "",
    path: str = "",
) -> dict[str, str]:
    return {
        "code": code,
        "message": message,
        "snapshot_id": snapshot_id,
        "evidence_id": evidence_id,
        "persistent_run_id": persistent_run_id,
        "path": path,
    }


def _run_integrity_code(errors: tuple[str, ...]) -> str:
    joined = " ".join(errors).lower()
    if "unsupported evidence schema version" in joined:
        return "EVIDENCE_SCHEMA_VERSION_UNSUPPORTED"
    if "digest" in joined:
        return "EVIDENCE_DIGEST_INVALID"
    if "invalid json" in joined or "schema" in joined:
        return "EVIDENCE_INTEGRITY_INVALID"
    return "EVIDENCE_INTEGRITY_UNKNOWN"


def _classified_accounting_digest(
    runs: tuple[RunEvidenceAccountingItem, ...],
    snapshots: tuple[ClassifiedSnapshotReplayItem, ...],
    counts: tuple[int, ...],
) -> str:
    value = {
        "domain": "local_steward.classified_operational_replay.accounting.v1",
        "counts": list(counts),
        "runs": [
            {
                "run_id": item.run_id,
                "evidence_count": item.evidence_count,
                "evidence_integrity": item.evidence_integrity.value,
                "run_kind": item.run_kind,
                "final_status": item.final_status,
                "last_evidence_digest": item.last_evidence_digest,
                "evidence_ids": list(item.evidence_ids),
                "evidence_digests": list(item.evidence_digests),
                "issue_codes": list(item.issue_codes),
            }
            for item in runs
        ],
        "snapshots": [
            {
                "snapshot_id": item.snapshot_id,
                "evidence_id": item.evidence_id,
                "persistent_run_id": item.persistent_run_id,
                "evidence_relative_path": item.evidence_relative_path,
                "evidence_digest": item.evidence_digest,
                "entry_count": item.entry_count,
                "evidence_integrity": item.evidence_integrity.value,
                "semantic_consistency": item.semantic_consistency.value,
                "replay_eligibility": (
                    item.replay_eligibility.value if item.replay_eligibility else None
                ),
                "reason_codes": list(item.reason_codes),
                "entries": [
                    {
                        "entry_id": entry.entry_id,
                        "snapshot_id": entry.snapshot_id,
                        "scope_id": entry.scope_id,
                        "relative_path": entry.relative_path,
                        "evidence_id": entry.evidence_id,
                        "evidence_relative_path": entry.evidence_relative_path,
                        "evidence_digest": entry.evidence_digest,
                        "replay_eligibility": (
                            entry.replay_eligibility.value
                            if entry.replay_eligibility is not None
                            else None
                        ),
                        "reason_codes": list(entry.reason_codes),
                    }
                    for entry in item.entries
                ],
            }
            for item in snapshots
        ],
    }
    return hashlib.sha256(canonical_json(value)).hexdigest()


def classify_operational_evidence(
    config: StewardConfig, *, connection: sqlite3.Connection | None = None
) -> ClassifiedReplayPlan:
    """Classify a complete Evidence corpus before any candidate database mutation."""
    run_items: list[RunEvidenceAccountingItem] = []
    run_files: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
    issues: list[dict[str, str]] = []
    for run_id in _verification_run_ids(config, None):
        files, load_errors = load_run_files(config.paths.evidence_dir, run_id)
        verification = _verify_run(config, run_id, index=False)
        errors = tuple(sorted(set((*load_errors, *verification.errors))))
        valid = verification.ledger_valid and not load_errors
        run_kind: str | None = None
        final_status: str | None = None
        if valid:
            first = files[0][1]
            run_kind = first["payload"]["run_kind"]
            status = RunStatus.CREATED
            for _path, document in files[1:]:
                if document["evidence_type"] == "run.state_transition":
                    status = RunStatus(document["payload"]["to_status"])
            final_status = status.value
        run_files[run_id] = files
        code = _run_integrity_code(errors) if errors else ""
        if code:
            issues.append(
                _classified_issue(
                    code,
                    "authoritative Run Evidence did not pass structural integrity validation",
                    persistent_run_id=run_id,
                )
            )
        evidence_integrity = (
            EvidenceIntegrityStatus.VALID
            if valid
            else EvidenceIntegrityStatus.UNKNOWN
            if code in {"EVIDENCE_SCHEMA_VERSION_UNSUPPORTED", "EVIDENCE_INTEGRITY_UNKNOWN"}
            else EvidenceIntegrityStatus.INVALID
        )
        run_items.append(
            RunEvidenceAccountingItem(
                run_id,
                len(files),
                evidence_integrity,
                run_kind,
                final_status,
                files[-1][1].get("evidence_digest") if files else None,
                tuple(
                    str(document.get("evidence_id", "")) for _path, document in files
                ),
                tuple(
                    str(document.get("evidence_digest", "")) for _path, document in files
                ),
                (code,) if code else (),
            )
        )

    raw_snapshots: list[dict[str, Any]] = []
    for run_id, files in run_files.items():
        for path, document in files:
            if document.get("evidence_type") != "filesystem.snapshot":
                continue
            relative = str(path.relative_to(config.paths.evidence_dir))
            intrinsic = validate_snapshot_evidence(document)
            payload = document.get("payload")
            snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
            entries = payload.get("entries") if isinstance(payload, dict) else None
            codes = {error["code"] for error in intrinsic.errors}
            if intrinsic.valid and snapshot_evidence_schema_version(document) == 2:
                try:
                    parsed = snapshot_v2_from_valid_evidence(document, relative)
                    codes.update(
                        issue["code"]
                        for issue in validate_snapshot_reuse_references(
                            config, parsed, connection=connection
                        )
                    )
                except (KeyError, TypeError, ValueError, EvidenceError):
                    codes.add("SNAPSHOT_REPLAY_SOURCE_INVALID")
            raw_snapshots.append(
                {
                    "snapshot_id": intrinsic.snapshot_id,
                    "evidence_id": intrinsic.evidence_id,
                    "run_id": document.get("run_id") if isinstance(document.get("run_id"), str) else run_id,
                    "relative": relative,
                    "evidence_digest": document.get("evidence_digest") if isinstance(document.get("evidence_digest"), str) else None,
                    "entry_count": len(entries) if isinstance(entries, list) else 0,
                    "entries": entries if isinstance(entries, list) else [],
                    "status": snapshot.get("status") if isinstance(snapshot, dict) else None,
                    "intrinsic_valid": intrinsic.valid and not codes,
                    "codes": codes,
                }
            )

    for key, code in (
        ("snapshot_id", "SNAPSHOT_ID_DUPLICATE"),
        ("run_id", "SNAPSHOT_RUN_DUPLICATE"),
        ("evidence_id", "SNAPSHOT_EVIDENCE_ID_CONFLICT"),
    ):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in raw_snapshots:
            value = item[key]
            if isinstance(value, str) and value:
                grouped.setdefault(value, []).append(item)
        for group in grouped.values():
            if len(group) > 1:
                for item in group:
                    item["codes"].add(code)
                    item["intrinsic_valid"] = False

    run_by_id = {item.run_id: item for item in run_items}
    snapshot_items: list[ClassifiedSnapshotReplayItem] = []
    for item in raw_snapshots:
        run = run_by_id.get(item["run_id"])
        integrity_valid = bool(item["intrinsic_valid"]) and run is not None and (
            run.evidence_integrity == EvidenceIntegrityStatus.VALID
        )
        codes = set(item["codes"])
        semantic = SemanticConsistencyStatus.UNKNOWN
        eligibility: ReplayEligibility | None = None
        if not integrity_valid:
            if run is None:
                codes.add("SNAPSHOT_RUN_MISSING")
            elif run.evidence_integrity != EvidenceIntegrityStatus.VALID:
                codes.add("SNAPSHOT_REPLAY_SOURCE_INVALID")
        elif run is None:
            codes.add("SNAPSHOT_RUN_MISSING")
        elif run.run_kind != "filesystem.snapshot":
            semantic = SemanticConsistencyStatus.INCONSISTENT
            codes.add("SNAPSHOT_RUN_KIND_INVALID")
        else:
            expected = snapshot_run_compatible(
                item["status"],
                run.run_kind,
                run.final_status,
                (document for _path, document in run_files.get(run.run_id, [])),
            )
            if item["status"] not in {
                SnapshotStatus.COMPLETE.value,
                SnapshotStatus.PARTIAL.value,
            } or run.final_status is None:
                semantic = SemanticConsistencyStatus.UNKNOWN
                codes.add("SNAPSHOT_SEMANTIC_CONSISTENCY_UNKNOWN")
            elif expected:
                semantic = SemanticConsistencyStatus.CONSISTENT
                eligibility = ReplayEligibility.ELIGIBLE
            elif (
                item["status"] == SnapshotStatus.PARTIAL.value
                and run.final_status == RunStatus.SCANNING.value
                and not is_supported_acquisition_run(
                    document for _path, document in run_files.get(run.run_id, [])
                )
            ):
                semantic = SemanticConsistencyStatus.INCONSISTENT
                eligibility = ReplayEligibility.INELIGIBLE
                codes.add("SNAPSHOT_RUN_STATUS_INVALID")
            else:
                semantic = SemanticConsistencyStatus.INCONSISTENT
                codes.add("SNAPSHOT_RUN_STATUS_INVALID")
        unknown_integrity = (
            run is not None and run.evidence_integrity == EvidenceIntegrityStatus.UNKNOWN
        ) or "EVIDENCE_SCHEMA_VERSION_UNSUPPORTED" in codes
        integrity = (
            EvidenceIntegrityStatus.VALID
            if integrity_valid
            else EvidenceIntegrityStatus.UNKNOWN
            if unknown_integrity
            else EvidenceIntegrityStatus.INVALID
        )
        entry_items = tuple(
            sorted(
                (
                    ClassifiedSnapshotEntryAccountingItem(
                        str(entry.get("entry_id", "")),
                        str(entry.get("snapshot_id", "")),
                        str(entry.get("scope_id", "")),
                        str(entry.get("relative_path", "")),
                        item["evidence_id"],
                        item["relative"],
                        item["evidence_digest"],
                        eligibility,
                        tuple(sorted(codes)) if eligibility == ReplayEligibility.INELIGIBLE else (),
                    )
                    for entry in item["entries"]
                    if isinstance(entry, dict)
                ),
                key=lambda entry: (
                    entry.scope_id,
                    entry.relative_path,
                    entry.entry_id,
                ),
            )
        )
        snapshot_items.append(
            ClassifiedSnapshotReplayItem(
                item["snapshot_id"],
                item["evidence_id"],
                item["run_id"],
                item["relative"],
                item["evidence_digest"],
                item["entry_count"],
                integrity,
                semantic,
                eligibility,
                False,
                tuple(sorted(codes)),
                entry_items,
            )
        )

    ordered_runs = tuple(sorted(run_items, key=lambda item: item.run_id))
    ordered_snapshots = tuple(
        sorted(
            snapshot_items,
            key=lambda item: (
                item.snapshot_id is None,
                item.snapshot_id or "",
                item.evidence_id or "",
                item.persistent_run_id or "",
            ),
        )
    )
    snapshot_runs = {item.persistent_run_id for item in ordered_snapshots}
    total_evidence = sum(item.evidence_count for item in ordered_runs)
    total_entries = sum(item.entry_count for item in ordered_snapshots)
    eligible = tuple(
        item for item in ordered_snapshots if item.replay_eligibility == ReplayEligibility.ELIGIBLE
    )
    ineligible = tuple(
        item for item in ordered_snapshots if item.replay_eligibility == ReplayEligibility.INELIGIBLE
    )
    non_snapshot_runs = tuple(item for item in ordered_runs if item.run_id not in snapshot_runs)
    counts = (
        len(ordered_runs),
        total_evidence,
        len(ordered_snapshots),
        total_entries,
        len(eligible),
        sum(item.entry_count for item in eligible),
        len(ineligible),
        sum(item.entry_count for item in ineligible),
        len(non_snapshot_runs),
        sum(item.evidence_count for item in non_snapshot_runs),
    )
    accounting_digest = _classified_accounting_digest(ordered_runs, ordered_snapshots, counts)
    plan = ClassifiedReplayPlan(
        not issues
        and all(item.evidence_integrity == EvidenceIntegrityStatus.VALID for item in ordered_runs)
        and all(
            item.evidence_integrity == EvidenceIntegrityStatus.VALID
            and item.semantic_consistency != SemanticConsistencyStatus.UNKNOWN
            and item.replay_eligibility is not None
            for item in ordered_snapshots
        ),
        *counts,
        accounting_digest,
        ordered_runs,
        ordered_snapshots,
        tuple(
            sorted(
                issues,
                key=lambda issue: (
                    issue["code"],
                    issue["snapshot_id"],
                    issue["evidence_id"],
                    issue["persistent_run_id"],
                ),
            )
        ),
    )
    validation_issues = validate_classified_replay_plan(plan)
    if not validation_issues:
        return plan
    combined = {
        (
            issue["code"],
            issue["snapshot_id"],
            issue["evidence_id"],
            issue["persistent_run_id"],
        ): issue
        for issue in (*plan.issues, *validation_issues)
    }
    return replace(
        plan,
        classification_complete=False,
        issues=tuple(
            combined[key] for key in sorted(combined)
        ),
    )


def validate_classified_replay_plan(
    plan: ClassifiedReplayPlan,
) -> tuple[dict[str, str], ...]:
    """Reject incomplete accounting and every non-governed exclusion deterministically."""
    issues: list[dict[str, str]] = []
    if any(item.evidence_integrity != EvidenceIntegrityStatus.VALID for item in plan.runs):
        issues.append(
            _classified_issue(
                "OPERATIONAL_REPLAY_EVIDENCE_INTEGRITY_FAILED",
                "one or more Run ledgers are not structurally trustworthy",
            )
        )
    for item in plan.snapshots:
        identity = {
            "snapshot_id": item.snapshot_id or "",
            "evidence_id": item.evidence_id or "",
            "persistent_run_id": item.persistent_run_id or "",
            "path": item.evidence_relative_path or "",
        }
        if item.evidence_integrity != EvidenceIntegrityStatus.VALID:
            issues.append(
                _classified_issue(
                    "OPERATIONAL_REPLAY_EVIDENCE_INTEGRITY_FAILED",
                    "Snapshot Evidence did not pass structural integrity validation",
                    **identity,
                )
            )
        elif item.semantic_consistency == SemanticConsistencyStatus.UNKNOWN:
            issues.append(
                _classified_issue(
                    "OPERATIONAL_REPLAY_SEMANTIC_CONSISTENCY_UNKNOWN",
                    "Snapshot semantic consistency could not be classified",
                    **identity,
                )
            )
        elif item.replay_eligibility is None:
            issues.append(
                _classified_issue(
                    "OPERATIONAL_REPLAY_ELIGIBILITY_UNCLASSIFIED",
                    "Snapshot replay eligibility was not classified",
                    **identity,
                )
            )
        elif item.replay_eligibility == ReplayEligibility.INELIGIBLE and (
            item.semantic_consistency != SemanticConsistencyStatus.INCONSISTENT
            or set(item.reason_codes) != {"SNAPSHOT_RUN_STATUS_INVALID"}
        ):
            issues.append(
                _classified_issue(
                    "OPERATIONAL_REPLAY_EXCLUSION_NOT_GOVERNED",
                    "Snapshot exclusion is outside the frozen lifecycle-mismatch rule",
                    **identity,
                )
            )
        if item.entry_count != len(item.entries):
            issues.append(
                _classified_issue(
                    "OPERATIONAL_REPLAY_ENTRY_ACCOUNTING_INCOMPLETE",
                    "Snapshot Entry identities do not match the classified entry count",
                    **identity,
                )
            )
    expected = (
        len(plan.runs),
        sum(item.evidence_count for item in plan.runs),
        len(plan.snapshots),
        sum(item.entry_count for item in plan.snapshots),
        sum(item.replay_eligibility == ReplayEligibility.ELIGIBLE for item in plan.snapshots),
        sum(
            item.entry_count
            for item in plan.snapshots
            if item.replay_eligibility == ReplayEligibility.ELIGIBLE
        ),
        sum(item.replay_eligibility == ReplayEligibility.INELIGIBLE for item in plan.snapshots),
        sum(
            item.entry_count
            for item in plan.snapshots
            if item.replay_eligibility == ReplayEligibility.INELIGIBLE
        ),
        plan.non_snapshot_run_count,
        plan.non_snapshot_evidence_count,
    )
    actual = (
        plan.total_run_count,
        plan.total_evidence_count,
        plan.total_snapshot_count,
        plan.total_snapshot_entry_count,
        plan.eligible_snapshot_count,
        plan.eligible_entry_count,
        plan.ineligible_snapshot_count,
        plan.ineligible_entry_count,
        plan.non_snapshot_run_count,
        plan.non_snapshot_evidence_count,
    )
    if actual != expected:
        issues.append(
            _classified_issue(
                "OPERATIONAL_REPLAY_ACCOUNTING_INCOMPLETE",
                "classified replay counts do not account for the complete corpus",
            )
        )
    if plan.accounting_digest != _classified_accounting_digest(plan.runs, plan.snapshots, actual):
        issues.append(
            _classified_issue(
                "OPERATIONAL_REPLAY_ACCOUNTING_DIGEST_INVALID",
                "classified replay accounting digest does not match the plan",
            )
        )
    if not plan.classification_complete:
        issues.append(
            _classified_issue(
                "OPERATIONAL_REPLAY_CLASSIFICATION_INCOMPLETE",
                "classified replay plan is not complete",
            )
        )
    unique = {
        (issue["code"], issue["snapshot_id"], issue["evidence_id"], issue["persistent_run_id"]): issue
        for issue in issues
    }
    return tuple(unique[key] for key in sorted(unique))


def storage_status(config: StewardConfig) -> StorageStatus:
    path = database_path(config)
    temporary = (
        len(list((config.paths.evidence_dir / "runs").glob("**/*.tmp")))
        if (config.paths.evidence_dir / "runs").exists()
        else 0
    )
    if not path.exists():
        ledger = [
            _verify_run(config, run_id, index=False)
            for run_id in _verification_run_ids(config, None)
        ]
        ledger_runs = len(ledger)
        ledger_evidence = sum(item.evidence_count for item in ledger)
        return StorageStatus(
            "UNINITIALIZED",
            False,
            False,
            0,
            0,
            ledger_runs,
            ledger_evidence,
            ledger_evidence,
            0,
            temporary,
            ("state.db is missing",),
        )
    try:
        with open_readonly_initialized(config) as conn:
            errors: list[str] = []
            runs = evidence = missing = orphaned = 0
            try:
                classified_plan = classify_operational_evidence(config, connection=conn)
            except (OSError, StewardError, ValueError, TypeError, KeyError):
                classified_plan = None
            classified_mode = bool(
                classified_plan is not None
                and classified_plan.classification_complete
                and classified_plan.ineligible_snapshot_count
            )
            ledger = (
                [
                    _verify_run(config, run_id, index=True, connection=conn)
                    for run_id in _verification_run_ids(config, None)
                ]
                if classified_mode
                else verify_evidence(config, connection=conn)
            )
            ledger_runs = len(ledger)
            ledger_evidence = sum(item.evidence_count for item in ledger)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            foreign_keys = list(conn.execute("PRAGMA foreign_key_check"))
            if integrity is None or integrity[0] != "ok" or foreign_keys:
                return StorageStatus(
                    "CORRUPT", True, True, 0, 0, ledger_runs, ledger_evidence,
                    0, 0, temporary, ("SQLite integrity or foreign-key check failed",),
                )
            runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
            evidence = conn.execute("SELECT count(*) FROM evidence_records").fetchone()[0]
            indexed = {row[0] for row in conn.execute("SELECT relative_path FROM evidence_records")}
            missing = sum(
                not (config.paths.evidence_dir / relative).is_file() for relative in indexed
            )
            for result in ledger:
                files, _ = load_run_files(config.paths.evidence_dir, result.run_id or "")
                orphaned += sum(
                    str(file.relative_to(config.paths.evidence_dir)) not in indexed
                    for file, _ in files
                )
            base_status = "INCONSISTENT" if missing or orphaned or any(
                not item.ledger_valid or not item.index_consistent for item in ledger
            ) else "DEGRADED" if temporary else "HEALTHY"
            try:
                inventory = _inspect_snapshot_inventory(config, conn)
                snapshot_integrity = classify_snapshot_inventory(inventory)
            except Exception as error:
                code = error.code if isinstance(error, StewardError) else "SNAPSHOT_INTEGRITY_CHECK_FAILED"
                message = "snapshot integrity check failed"
                return StorageStatus(
                    _compose_storage_status(base_status, "INCONSISTENT"),
                    True, True, runs, evidence, ledger_runs, ledger_evidence,
                    orphaned, missing, temporary, (message,), None,
                    ({
                        "code": code, "message": message, "severity": "INVALID",
                        "snapshot_id": "", "evidence_id": "", "persistent_run_id": "", "path": "",
                    },),
                )
            if classified_mode and classified_plan is not None:
                operational_status, operational_issues, diagnostics = (
                    _operational_inventory_assessment(inventory, classified_plan)
                )
                composed_status = _compose_storage_status(base_status, operational_status)
                reported_issues = operational_issues
            else:
                composed_status = _compose_storage_status(
                    base_status, _snapshot_storage_status(snapshot_integrity)
                )
                reported_issues = _snapshot_storage_issues(snapshot_integrity)
                diagnostics = ()
            return StorageStatus(
                composed_status, True, True, runs, evidence, ledger_runs, ledger_evidence,
                orphaned, missing, temporary, tuple(errors), snapshot_integrity,
                reported_issues, diagnostics,
            )
    except (StorageCorruptionError, sqlite3.Error) as error:
        return StorageStatus(
            "CORRUPT", True, False, 0, 0, 0, 0, 0, 0, temporary, (str(error),),
        )


def rebuild_index(config: StewardConfig) -> None:
    results = verify_evidence(config)
    if not results or any(not result.ledger_valid for result in results):
        raise EvidenceError("rebuild refused: evidence ledger is invalid or empty")
    temp = config.paths.cache_dir / f"state-rebuild-{uuid.uuid4()}.db"
    target = database_path(config)
    initialize(temp, __version__, utc_now())
    conn = (
        open_initialized(
            type("Config", (), {"paths": type("Paths", (), {"data_dir": temp.parent})()})()
        )
        if False
        else None
    )
    conn = sqlite3.connect(temp)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        for result in results:
            files, _ = load_run_files(config.paths.evidence_dir, result.run_id or "")
            first = files[0][1]
            final = files[-1][1]
            status = RunStatus.CREATED
            for _, item in files[1:]:
                if item["evidence_type"] == "run.state_transition":
                    status = RunStatus(item["payload"]["to_status"])
            conn.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.run_id,
                    first["payload"]["run_kind"],
                    status.value,
                    first["created_at"],
                    final["created_at"],
                    first["config_digest"],
                    json.dumps(first["payload"]["metadata"], sort_keys=True, separators=(",", ":")),
                    len(files),
                    final["evidence_digest"],
                    int(is_terminal(status)),
                ),
            )
            for path, item in files:
                conn.execute(
                    "INSERT INTO evidence_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item["evidence_id"],
                        result.run_id,
                        item["sequence"],
                        item["evidence_type"],
                        item["created_at"],
                        str(path.relative_to(config.paths.evidence_dir)),
                        item["previous_evidence_digest"],
                        item["evidence_digest"],
                            item["schema_version"],
                    ),
                )
                if item["evidence_type"] == "filesystem.snapshot":
                    relative = str(path.relative_to(config.paths.evidence_dir))
                    snapshot = snapshot_from_valid_evidence_versioned(item, relative)
                    _insert_snapshot_index_rows(
                        conn, snapshot, str(item["evidence_id"]), relative
                    )
        conn.commit()
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise StorageCorruptionError("rebuilt database integrity check failed")
    finally:
        conn.close()
    if target.exists():
        shutil.copy2(target, config.paths.cache_dir / f"state-before-rebuild-{uuid.uuid4()}.db")
    temp.replace(target)
