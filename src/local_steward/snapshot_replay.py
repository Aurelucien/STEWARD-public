"""Strict, isolated replay of valid Snapshot Evidence into a candidate derived index."""

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from . import __version__
from .database import SCHEMA_VERSION, connect, database_path, initialize, validate_schema
from .evidence import canonical_json, load_run_files
from .errors import StewardError
from .models import (
    ClassifiedOperationalReplayReport,
    ClassifiedReplayPlan,
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    ReplayEligibility,
    RunStatus,
    SnapshotReplayItem,
    SnapshotReplayReport,
    SnapshotReplayStatus,
    StewardConfig,
)
from .snapshots import _insert_snapshot_index_rows, snapshot_from_valid_evidence_versioned
from .state_machine import is_terminal
from .storage import (
    classify_operational_evidence,
    validate_classified_replay_plan,
    verify_evidence_report,
)

_REPLAY_SCHEMA_CREATED_AT = "1970-01-01T00:00:00.000000Z"


def _issue(
    code: str,
    message: str,
    *,
    snapshot_id: str = "",
    evidence_id: str = "",
    persistent_run_id: str = "",
    path: str = "",
    expected: str = "",
    actual: str = "",
) -> dict[str, str]:
    return {
        "code": code,
        "message": message,
        "snapshot_id": snapshot_id,
        "evidence_id": evidence_id,
        "persistent_run_id": persistent_run_id,
        "path": path,
        "expected": expected,
        "actual": actual,
    }


def _failed_report(
    destination: Path,
    code: str,
    message: str,
    *,
    destination_schema_version: int = 0,
) -> SnapshotReplayReport:
    return SnapshotReplayReport(
        SnapshotReplayStatus.FAILED,
        False,
        str(destination),
        0,
        0,
        0,
        0,
        0,
        "",
        "",
        destination_schema_version,
        (),
        (_issue(code, message, path=str(destination)),),
        (),
    )


def _target_is_live_database(config: StewardConfig, destination: Path) -> bool:
    live = database_path(config)
    try:
        aliases = {
            live.resolve(strict=False),
            Path(f"{live}-wal").resolve(strict=False),
            Path(f"{live}-shm").resolve(strict=False),
        }
        return destination.is_symlink() or destination.resolve(strict=False) in aliases
    except OSError:
        return True


def _entry_projection(entry: FilesystemEntry | FilesystemEntryV2) -> dict[str, Any]:
    value: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "snapshot_id": entry.snapshot_id,
        "scope_id": entry.scope_id,
        "relative_path": entry.relative_path,
        "object_type": entry.object_type.value,
        "device_id": entry.device_id,
        "inode": entry.inode,
        "mode": entry.mode,
        "uid": entry.uid,
        "gid": entry.gid,
        "size_bytes": entry.size_bytes,
        "mtime_ns": entry.mtime_ns,
        "ctime_ns": entry.ctime_ns,
        "birthtime_ns": entry.birthtime_ns,
        "link_count": entry.link_count,
        "symlink_target_raw": entry.symlink_target_raw,
        "readable": entry.readable,
        "writable": entry.writable,
        "executable": entry.executable,
        "observation_status": entry.observation_status.value,
        "error_code": entry.error_code,
        "error_message": entry.error_message,
        "excluded": entry.excluded,
    }
    if isinstance(entry, FilesystemEntryV2):
        value["allocated_size_bytes"] = entry.allocated_size_bytes
        value["payload_observation"] = {
            "status": entry.payload_observation.status.value,
            "algorithm": entry.payload_observation.algorithm,
            "algorithm_version": entry.payload_observation.algorithm_version,
            "digest": entry.payload_observation.digest,
            "bytes_hashed": entry.payload_observation.bytes_hashed,
            "provenance": entry.payload_observation.provenance.value if entry.payload_observation.provenance else None,
            "reused_from_snapshot_id": entry.payload_observation.reused_from_snapshot_id,
            "failure_code": entry.payload_observation.failure_code,
            "os_error_code": entry.payload_observation.os_error_code,
        }
    else:
        value["allocated_size_bytes"] = None
        value["payload_observation"] = None
    return value


def _snapshot_projection(snapshot: FilesystemSnapshot | FilesystemSnapshotV2) -> dict[str, Any]:
    value: dict[str, Any] = {
        "snapshot_id": snapshot.snapshot_id,
        "run_id": snapshot.run_id,
        "status": snapshot.status.value,
        "consistency": snapshot.consistency.value,
        "created_at": snapshot.created_at,
        "started_at": snapshot.started_at,
        "completed_at": snapshot.completed_at,
        "config_digest": snapshot.config_digest,
        "scope_ids": list(snapshot.scope_ids),
        "budget": {
            "max_entries": snapshot.budget.max_entries,
            "max_total_stat_bytes": snapshot.budget.max_total_stat_bytes,
            "max_duration_seconds": snapshot.budget.max_duration_seconds,
            "max_depth": snapshot.budget.max_depth,
        },
        "entry_count": snapshot.entry_count,
        "observed_count": snapshot.observed_count,
        "error_count": snapshot.error_count,
        "excluded_count": snapshot.excluded_count,
        "total_regular_file_bytes": snapshot.total_regular_file_bytes,
        "max_depth_observed": snapshot.max_depth_observed,
        "entries_digest": snapshot.entries_digest,
        "snapshot_digest": snapshot.snapshot_digest,
        "evidence_id": snapshot.evidence_id,
        "evidence_relative_path": snapshot.evidence_relative_path,
    }
    if isinstance(snapshot, FilesystemSnapshotV2):
        value["snapshot_evidence_schema_version"] = 2
        value["hash_policy"] = {
            "algorithm": snapshot.hash_policy.algorithm,
            "algorithm_version": snapshot.hash_policy.algorithm_version,
            "max_hash_file_bytes": snapshot.hash_policy.max_hash_file_bytes,
            "max_total_hash_bytes": snapshot.hash_policy.max_total_hash_bytes,
            "max_hash_duration_seconds": snapshot.hash_policy.max_hash_duration_seconds,
            "hash_chunk_size": snapshot.hash_policy.hash_chunk_size,
            "allow_non_local_content": snapshot.hash_policy.allow_non_local_content,
            "allow_verified_reuse": snapshot.hash_policy.allow_verified_reuse,
        }
        value["allocated_regular_file_bytes_known_sum"] = snapshot.allocated_regular_file_bytes_known_sum
        value["allocated_regular_file_unknown_count"] = snapshot.allocated_regular_file_unknown_count
        value["payload_observation_summary"] = {
            "status_counts": [
                {"status": item.status.value, "count": item.count}
                for item in snapshot.payload_observation_summary
            ]
        }
    else:
        value["snapshot_evidence_schema_version"] = 1
        value["hash_policy"] = None
        value["allocated_regular_file_bytes_known_sum"] = None
        value["allocated_regular_file_unknown_count"] = None
        value["payload_observation_summary"] = None
    return value


def _business_digest(snapshots: list[FilesystemSnapshot | FilesystemSnapshotV2]) -> str:
    ordered_snapshots = sorted(snapshots, key=lambda item: (item.snapshot_id, item.evidence_id or ""))
    projection = {
        "snapshots": [_snapshot_projection(snapshot) for snapshot in ordered_snapshots],
        "entries": [
            _entry_projection(cast(FilesystemEntry | FilesystemEntryV2, entry))
            for snapshot in ordered_snapshots
            for entry in sorted(snapshot.entries, key=lambda value: (value.scope_id, value.relative_path))
        ],
    }
    return hashlib.sha256(canonical_json(projection)).hexdigest()


def _destination_digest(conn: sqlite3.Connection) -> str:
    snapshots: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM snapshots ORDER BY snapshot_id, evidence_id"):
        value = dict(row)
        snapshots.append(
            {
                "snapshot_id": value["snapshot_id"], "run_id": value["run_id"], "status": value["status"],
                "consistency": value["consistency"], "created_at": value["created_at"], "started_at": value["started_at"],
                "completed_at": value["completed_at"], "config_digest": value["config_digest"],
                "scope_ids": json.loads(value["scope_ids_json"]), "budget": json.loads(value["budget_json"]),
                "entry_count": value["entry_count"], "observed_count": value["observed_count"], "error_count": value["error_count"],
                "excluded_count": value["excluded_count"], "total_regular_file_bytes": value["total_regular_file_bytes"],
                "max_depth_observed": value["max_depth_observed"], "entries_digest": value["entries_digest"],
                "snapshot_digest": value["snapshot_digest"], "evidence_id": value["evidence_id"],
                "evidence_relative_path": value["evidence_relative_path"],
                "snapshot_evidence_schema_version": value["snapshot_evidence_schema_version"],
                "hash_policy": json.loads(value["hash_policy_json"]) if value["hash_policy_json"] else None,
                "allocated_regular_file_bytes_known_sum": value["allocated_regular_file_bytes_known_sum"],
                "allocated_regular_file_unknown_count": value["allocated_regular_file_unknown_count"],
                "payload_observation_summary": json.loads(value["payload_observation_summary_json"]) if value["payload_observation_summary_json"] else None,
            }
        )
    entries: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM snapshot_entries ORDER BY snapshot_id, scope_id, relative_path"):
        value = dict(row)
        entry = {key: value[key] for key in _entry_projection_keys()}
        for key in ("readable", "writable", "executable", "excluded"):
            entry[key] = bool(entry[key])
        entries.append(entry | {"allocated_size_bytes": value["allocated_size_bytes"], "payload_observation": json.loads(value["payload_observation_json"]) if value["payload_observation_json"] else None})
    return hashlib.sha256(canonical_json({"snapshots": snapshots, "entries": entries})).hexdigest()


def _entry_projection_keys() -> tuple[str, ...]:
    return ("entry_id", "snapshot_id", "scope_id", "relative_path", "object_type", "device_id", "inode", "mode", "uid", "gid", "size_bytes", "mtime_ns", "ctime_ns", "birthtime_ns", "link_count", "symlink_target_raw", "readable", "writable", "executable", "observation_status", "error_code", "error_message", "excluded")


def _source_documents(
    config: StewardConfig, replayable: set[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, list[tuple[Path, dict[str, Any]]]]]:
    documents: dict[str, dict[str, Any]] = {}
    files_by_run: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    runs_root = config.paths.evidence_dir / "runs"
    if not runs_root.exists():
        return documents, files_by_run
    for run_dir in sorted(runs_root.iterdir(), key=lambda path: path.name):
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        files, errors = load_run_files(config.paths.evidence_dir, run_dir.name)
        if errors:
            continue
        files_by_run[run_dir.name] = files
        for path, document in files:
            evidence_id = document.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id in replayable:
                documents[evidence_id] = {
                    "document": document,
                    "relative_path": str(path.relative_to(config.paths.evidence_dir)),
                }
    return documents, files_by_run


def _insert_run_ledger(
    conn: sqlite3.Connection, files: list[tuple[Path, dict[str, Any]]], evidence_root: Path
) -> None:
    first = files[0][1]
    final = files[-1][1]
    status = RunStatus.CREATED
    for _path, document in files[1:]:
        if document["evidence_type"] == "run.state_transition":
            status = RunStatus(document["payload"]["to_status"])
    conn.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            first["run_id"],
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
    conn.executemany(
        "INSERT INTO evidence_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                document["evidence_id"],
                document["run_id"],
                document["sequence"],
                document["evidence_type"],
                document["created_at"],
                str(path.relative_to(evidence_root)),
                document["previous_evidence_digest"],
                document["evidence_digest"],
                document["schema_version"],
            )
            for path, document in files
        ],
    )


def replay_snapshot_index(config: StewardConfig, destination_database: Path) -> SnapshotReplayReport:
    """STRICT replay to an isolated empty database; never replaces the live derived index."""
    destination = Path(destination_database)
    if _target_is_live_database(config, destination):
        return _failed_report(
            destination,
            "SNAPSHOT_REPLAY_TARGET_IS_LIVE_DATABASE",
            "replay destination must not reference the live database or its journals",
        )
    try:
        if destination.exists():
            conn = connect(destination)
            try:
                validate_schema(conn)
                if any(
                    conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("runs", "evidence_records", "snapshots", "snapshot_entries")
                ):
                    return _failed_report(
                        destination,
                        "SNAPSHOT_REPLAY_TARGET_NOT_EMPTY",
                        "replay destination contains business rows and will not be overwritten",
                        destination_schema_version=SCHEMA_VERSION,
                    )
            finally:
                conn.close()
        else:
            initialize(destination, __version__, _REPLAY_SCHEMA_CREATED_AT)
        verification = verify_evidence_report(config)
    except (OSError, sqlite3.Error, StewardError, ValueError, TypeError):
        return _failed_report(
            destination,
            "SNAPSHOT_REPLAY_DESTINATION_INVALID",
            "unable to initialize or validate isolated replay database",
            destination_schema_version=SCHEMA_VERSION if destination.exists() else 0,
        )
    report = verification.snapshot_evidence
    invalid_ledger_runs = {
        result.run_id
        for result in verification.verifications
        if result.run_id is not None and not result.ledger_valid
    }
    replay_items: list[SnapshotReplayItem] = []
    issues: list[dict[str, str]] = []
    replayable_ids: set[str] = set()
    for item in report.items:
        evidence_ledger_valid = item.persistent_run_id not in invalid_ledger_runs
        codes = tuple(
            sorted(
                {error["code"] for error in item.errors}
                | ({"SNAPSHOT_REPLAY_SOURCE_INVALID"} if not evidence_ledger_valid else set())
            )
        )
        entry_count = 0
        item_replayable = item.valid and evidence_ledger_valid
        if item_replayable and item.evidence_id:
            replayable_ids.add(item.evidence_id)
        replay_items.append(
            SnapshotReplayItem(
                item.snapshot_id,
                item.evidence_id,
                item.persistent_run_id,
                item_replayable,
                False,
                entry_count,
                codes,
            )
        )
        if not item_replayable:
            issues.append(
                _issue(
                    "SNAPSHOT_REPLAY_EVIDENCE_REJECTED",
                    "Snapshot Evidence is not safe to replay in STRICT mode",
                    snapshot_id=item.snapshot_id or "",
                    evidence_id=item.evidence_id or "",
                    persistent_run_id=item.persistent_run_id or "",
                    path=item.evidence_relative_path or "",
                )
            )
            if not evidence_ledger_valid:
                issues.append(
                    _issue(
                        "SNAPSHOT_REPLAY_SOURCE_INVALID",
                        "Snapshot Run evidence ledger is not valid",
                        snapshot_id=item.snapshot_id or "",
                        evidence_id=item.evidence_id or "",
                        persistent_run_id=item.persistent_run_id or "",
                        path=item.evidence_relative_path or "",
                    )
                )
    for issue in report.issues:
        issues.append(
            _issue(
                issue["code"],
                issue["message"],
                snapshot_id=issue["snapshot_id"],
                evidence_id=issue["evidence_id"],
                persistent_run_id=issue["persistent_run_id"],
                path=issue["evidence_relative_path"],
            )
        )
    try:
        documents, files_by_run = _source_documents(config, replayable_ids)
    except (OSError, sqlite3.Error, StewardError, ValueError, TypeError, KeyError):
        return _failed_report(
            destination,
            "SNAPSHOT_REPLAY_SOURCE_INVALID",
            "replay source could not be read safely",
            destination_schema_version=SCHEMA_VERSION,
        )
    sources: list[FilesystemSnapshot | FilesystemSnapshotV2] = []
    for position, replay_item in enumerate(replay_items):
        if not replay_item.replayable or not replay_item.evidence_id:
            continue
        source = documents.get(replay_item.evidence_id)
        if source is None:
            replay_items[position] = SnapshotReplayItem(
                replay_item.snapshot_id,
                replay_item.evidence_id,
                replay_item.persistent_run_id,
                False,
                False,
                0,
                ("SNAPSHOT_REPLAY_SOURCE_INVALID",),
            )
            issues.append(
                _issue(
                    "SNAPSHOT_REPLAY_SOURCE_INVALID",
                    "replayable Snapshot Evidence was not found in the ledger",
                    snapshot_id=replay_item.snapshot_id or "",
                    evidence_id=replay_item.evidence_id,
                    persistent_run_id=replay_item.persistent_run_id or "",
                )
            )
            continue
        try:
            snapshot = snapshot_from_valid_evidence_versioned(source["document"], source["relative_path"])
        except (KeyError, TypeError, ValueError):
            replay_items[position] = SnapshotReplayItem(
                replay_item.snapshot_id,
                replay_item.evidence_id,
                replay_item.persistent_run_id,
                False,
                False,
                0,
                ("SNAPSHOT_REPLAY_SOURCE_INVALID",),
            )
            issues.append(
                _issue(
                    "SNAPSHOT_REPLAY_SOURCE_INVALID",
                    "validated Snapshot Evidence could not be mapped for replay",
                    snapshot_id=replay_item.snapshot_id or "",
                    evidence_id=replay_item.evidence_id,
                    persistent_run_id=replay_item.persistent_run_id or "",
                    path=source["relative_path"],
                )
            )
            continue
        sources.append(snapshot)
        replay_items[position] = SnapshotReplayItem(
            snapshot.snapshot_id,
            snapshot.evidence_id,
            snapshot.run_id,
            True,
            False,
            snapshot.entry_count,
            (),
        )
    source_digest = _business_digest(sources)
    ordered_items = tuple(
        sorted(
            replay_items,
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
                issue["path"],
            ),
        )
    )
    rejected = sum(not item.replayable for item in ordered_items)
    if rejected:
        conn = connect(destination)
        try:
            destination_digest = _destination_digest(conn)
        finally:
            conn.close()
        return SnapshotReplayReport(
            SnapshotReplayStatus.FAILED,
            False,
            str(destination),
            len(ordered_items),
            len(ordered_items) - rejected,
            rejected,
            0,
            0,
            source_digest,
            destination_digest,
            SCHEMA_VERSION,
            ordered_items,
            ordered_issues,
            ("STRICT replay rejected Snapshot Evidence; no business rows were written",),
        )
    conn = connect(destination)
    try:
        validate_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        used_runs = {snapshot.run_id for snapshot in sources}
        for run_id in sorted(used_runs):
            files = files_by_run.get(run_id)
            if not files:
                raise sqlite3.IntegrityError("Snapshot Run ledger is unavailable")
            _insert_run_ledger(conn, files, config.paths.evidence_dir)
        for snapshot in sorted(sources, key=lambda item: (item.snapshot_id, item.evidence_id or "")):
            _insert_snapshot_index_rows(
                conn,
                snapshot,
                snapshot.evidence_id or "",
                snapshot.evidence_relative_path or "",
            )
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("foreign key check failed")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.IntegrityError("integrity check failed")
        destination_digest = _destination_digest(conn)
        if source_digest != destination_digest:
            raise sqlite3.IntegrityError("business digest mismatch")
        snapshot_count = conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        entry_count = conn.execute("SELECT count(*) FROM snapshot_entries").fetchone()[0]
        if snapshot_count != len(sources) or entry_count != sum(
            snapshot.entry_count for snapshot in sources
        ):
            raise sqlite3.IntegrityError("destination count mismatch")
        conn.commit()
    except (OSError, sqlite3.Error, StewardError, ValueError, TypeError, KeyError) as error:
        conn.rollback()
        conn.close()
        message = str(error).lower()
        if "digest" in message:
            code = "SNAPSHOT_REPLAY_DIGEST_MISMATCH"
        elif "count" in message:
            code = "SNAPSHOT_REPLAY_COUNT_MISMATCH"
        elif "integrity" in message or "foreign key" in message:
            code = "SNAPSHOT_REPLAY_INTEGRITY_CHECK_FAILED"
        else:
            code = "SNAPSHOT_REPLAY_WRITE_FAILED"
        return SnapshotReplayReport(
            SnapshotReplayStatus.FAILED,
            False,
            str(destination),
            len(ordered_items),
            len(ordered_items),
            0,
            0,
            0,
            source_digest,
            "",
            SCHEMA_VERSION,
            ordered_items,
            tuple(
                sorted(
                    ordered_issues + (_issue(code, "isolated replay transaction failed"),),
                    key=lambda issue: (issue["code"], issue["snapshot_id"], issue["evidence_id"]),
                )
            ),
            (),
        )
    else:
        conn.close()
    replayed_items = tuple(
        SnapshotReplayItem(
            item.snapshot_id,
            item.evidence_id,
            item.persistent_run_id,
            item.replayable,
            item.replayable,
            item.entry_count,
            item.issue_codes,
        )
        for item in ordered_items
    )
    return SnapshotReplayReport(
        SnapshotReplayStatus.READY,
        True,
        str(destination),
        len(replayed_items),
        len(replayed_items),
        0,
        len(replayed_items),
        sum(item.entry_count for item in replayed_items),
        source_digest,
        destination_digest,
        SCHEMA_VERSION,
        replayed_items,
        ordered_issues,
        (),
    )


def _failed_operational_report(
    destination: Path,
    plan: ClassifiedReplayPlan,
    issues: tuple[dict[str, str], ...],
    *,
    destination_schema_version: int = 0,
) -> ClassifiedOperationalReplayReport:
    return ClassifiedOperationalReplayReport(
        SnapshotReplayStatus.FAILED,
        False,
        "UNHEALTHY",
        bool(plan.ineligible_snapshot_count),
        str(destination),
        destination_schema_version,
        "not_checked",
        "not_checked",
        plan.total_run_count,
        0,
        plan.total_evidence_count,
        0,
        plan.eligible_snapshot_count,
        0,
        plan.eligible_entry_count,
        0,
        plan.ineligible_snapshot_count,
        plan.ineligible_entry_count,
        "",
        "",
        plan.accounting_digest,
        plan,
        issues,
        (),
    )


def replay_classified_operational_index(
    config: StewardConfig, destination_database: Path
) -> ClassifiedOperationalReplayReport:
    """Build one isolated schema-v3 candidate from the complete classified eligible set."""
    destination = Path(destination_database)
    if _target_is_live_database(config, destination):
        empty = classify_operational_evidence(config)
        return _failed_operational_report(
            destination,
            empty,
            (
                _issue(
                    "OPERATIONAL_REPLAY_TARGET_IS_LIVE_DATABASE",
                    "operational replay destination must not reference the live database",
                    path=str(destination),
                ),
            ),
        )
    try:
        plan = classify_operational_evidence(config)
    except (OSError, sqlite3.Error, StewardError, ValueError, TypeError, KeyError):
        empty = ClassifiedReplayPlan(False, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "", (), (), ())
        return _failed_operational_report(
            destination,
            empty,
            (
                _issue(
                    "OPERATIONAL_REPLAY_CLASSIFICATION_FAILED",
                    "authoritative Evidence could not be classified safely",
                ),
            ),
        )
    validation_issues = validate_classified_replay_plan(plan)
    if validation_issues:
        return _failed_operational_report(destination, plan, validation_issues)

    eligible_ids = {
        item.evidence_id
        for item in plan.snapshots
        if item.replay_eligibility == ReplayEligibility.ELIGIBLE and item.evidence_id
    }
    try:
        documents, files_by_run = _source_documents(config, eligible_ids)
        if len(files_by_run) != plan.total_run_count or sum(
            len(files) for files in files_by_run.values()
        ) != plan.total_evidence_count:
            raise ValueError("classified source accounting mismatch")
        sources: list[FilesystemSnapshot | FilesystemSnapshotV2] = []
        for item in plan.snapshots:
            if item.replay_eligibility != ReplayEligibility.ELIGIBLE or not item.evidence_id:
                continue
            source = documents.get(item.evidence_id)
            if source is None:
                raise ValueError("eligible Snapshot Evidence is unavailable")
            snapshot = snapshot_from_valid_evidence_versioned(
                source["document"], source["relative_path"]
            )
            if snapshot.entry_count != item.entry_count:
                raise ValueError("eligible Snapshot entry accounting mismatch")
            sources.append(snapshot)
    except (OSError, sqlite3.Error, StewardError, ValueError, TypeError, KeyError):
        return _failed_operational_report(
            destination,
            plan,
            (
                _issue(
                    "OPERATIONAL_REPLAY_SOURCE_INVALID",
                    "classified replay source could not be mapped safely",
                ),
            ),
        )

    try:
        if destination.exists():
            conn = connect(destination)
            try:
                validate_schema(conn)
                if any(
                    conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("runs", "evidence_records", "snapshots", "snapshot_entries")
                ):
                    return _failed_operational_report(
                        destination,
                        plan,
                        (
                            _issue(
                                "OPERATIONAL_REPLAY_TARGET_NOT_EMPTY",
                                "operational replay destination contains derived rows",
                            ),
                        ),
                        destination_schema_version=SCHEMA_VERSION,
                    )
            finally:
                conn.close()
        else:
            initialize(destination, __version__, _REPLAY_SCHEMA_CREATED_AT)
    except (OSError, sqlite3.Error, StewardError, ValueError, TypeError):
        return _failed_operational_report(
            destination,
            plan,
            (
                _issue(
                    "OPERATIONAL_REPLAY_DESTINATION_INVALID",
                    "unable to initialize or validate isolated operational candidate",
                ),
            ),
            destination_schema_version=SCHEMA_VERSION if destination.exists() else 0,
        )

    source_digest = _business_digest(sources)
    conn = connect(destination)
    try:
        validate_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        for run_id in sorted(files_by_run):
            _insert_run_ledger(conn, files_by_run[run_id], config.paths.evidence_dir)
        for snapshot in sorted(sources, key=lambda item: (item.snapshot_id, item.evidence_id or "")):
            _insert_snapshot_index_rows(
                conn,
                snapshot,
                snapshot.evidence_id or "",
                snapshot.evidence_relative_path or "",
            )
        excluded_ids = {
            item.snapshot_id
            for item in plan.snapshots
            if item.replay_eligibility == ReplayEligibility.INELIGIBLE and item.snapshot_id
        }
        if excluded_ids:
            placeholders = ",".join("?" for _item in excluded_ids)
            leaked_snapshots = conn.execute(
                f"SELECT count(*) FROM snapshots WHERE snapshot_id IN ({placeholders})",
                tuple(sorted(excluded_ids)),
            ).fetchone()[0]
            leaked_entries = conn.execute(
                f"SELECT count(*) FROM snapshot_entries WHERE snapshot_id IN ({placeholders})",
                tuple(sorted(excluded_ids)),
            ).fetchone()[0]
            if leaked_snapshots or leaked_entries:
                raise sqlite3.IntegrityError("ineligible business rows leaked into candidate")
        foreign_key = conn.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key is not None:
            raise sqlite3.IntegrityError("foreign key check failed")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise sqlite3.IntegrityError("integrity check failed")
        actual_run_count = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
        actual_evidence_count = conn.execute("SELECT count(*) FROM evidence_records").fetchone()[0]
        actual_snapshot_count = conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        actual_entry_count = conn.execute("SELECT count(*) FROM snapshot_entries").fetchone()[0]
        expected_counts = (
            plan.total_run_count,
            plan.total_evidence_count,
            plan.eligible_snapshot_count,
            plan.eligible_entry_count,
        )
        actual_counts = (
            actual_run_count,
            actual_evidence_count,
            actual_snapshot_count,
            actual_entry_count,
        )
        if actual_counts != expected_counts:
            raise sqlite3.IntegrityError("classified candidate count mismatch")
        destination_digest = _destination_digest(conn)
        if destination_digest != source_digest:
            raise sqlite3.IntegrityError("classified candidate business digest mismatch")
        conn.commit()
    except (OSError, sqlite3.Error, StewardError, ValueError, TypeError, KeyError) as error:
        conn.rollback()
        message = str(error).lower()
        code = (
            "OPERATIONAL_REPLAY_DIGEST_MISMATCH"
            if "digest" in message
            else "OPERATIONAL_REPLAY_COUNT_MISMATCH"
            if "count" in message
            else "OPERATIONAL_REPLAY_INTEGRITY_CHECK_FAILED"
            if "integrity" in message or "foreign key" in message or "leaked" in message
            else "OPERATIONAL_REPLAY_WRITE_FAILED"
        )
        return _failed_operational_report(
            destination,
            plan,
            (_issue(code, "isolated classified replay transaction failed"),),
            destination_schema_version=SCHEMA_VERSION,
        )
    finally:
        conn.close()

    replayed_snapshots = tuple(
        replace(
            item,
            replayed=item.replay_eligibility == ReplayEligibility.ELIGIBLE,
        )
        for item in plan.snapshots
    )
    replayed_plan = replace(plan, snapshots=replayed_snapshots)
    return ClassifiedOperationalReplayReport(
        SnapshotReplayStatus.READY,
        True,
        "HEALTHY",
        bool(plan.ineligible_snapshot_count),
        str(destination),
        SCHEMA_VERSION,
        "ok",
        "ok",
        plan.total_run_count,
        actual_run_count,
        plan.total_evidence_count,
        actual_evidence_count,
        plan.eligible_snapshot_count,
        actual_snapshot_count,
        plan.eligible_entry_count,
        actual_entry_count,
        plan.ineligible_snapshot_count,
        plan.ineligible_entry_count,
        source_digest,
        destination_digest,
        plan.accounting_digest,
        replayed_plan,
        (),
        (
            ("historical replay-ineligible Snapshot diagnostics are present",)
            if plan.ineligible_snapshot_count
            else ()
        ),
    )
