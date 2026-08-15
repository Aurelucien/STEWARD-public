"""Snapshot construction, immutable evidence persistence, and derived queries."""

import json
import math
import sqlite3
import uuid
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from .database import open_initialized, open_readonly_initialized
from .evidence import canonical_json, compute_config_digest, load_run_files, utc_now, write_evidence
from .errors import (
    EvidenceError,
    SnapshotBudgetError,
    SnapshotNotFoundError,
    SnapshotScopeError,
    StorageCorruptionError,
)
from .filesystem import scan, select_scopes
from .faults import FaultInjector, checkpoint as fault_checkpoint
from .models import (
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemObjectType,
    FilesystemObservationStatus,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    PayloadObservation,
    PayloadHashPolicy,
    PayloadObservationCount,
    PayloadObservationProvenance,
    PayloadObservationStatus,
    FilesystemSnapshotSummary,
    RunRecord,
    RunStatus,
    ScanBudget,
    ScopeConfig,
    SnapshotConsistency,
    SnapshotStatus,
    SnapshotEntryPage,
    SnapshotEvidenceValidationResult,
    SnapshotInventory,
    SnapshotInventoryItem,
    SnapshotStorageIntegrityItem,
    SnapshotStorageIntegrityReport,
    SnapshotStorageIntegrityStatus,
    SnapshotVerificationResult,
    StewardConfig,
)
from .payload_hashing import (
    LocalityProvider,
    observe_payloads,
    unknown_locality,
    validate_payload_hash_policy,
)
from .runs import _evidence, _get_run, _run, create_run, get_run, transition_run
from .snapshot_lifecycle import (
    snapshot_run_compatible,
    supported_acquisition_metadata_valid,
)


def _json(value: object) -> dict[str, Any]:
    return cast(
        dict[str, Any], json.loads(canonical_json(asdict(cast(Any, value))).decode("utf-8"))
    )


def _digest(value: object) -> str:
    return sha256(canonical_json(value)).hexdigest()


def _entry_dict(entry: FilesystemEntry | FilesystemEntryV2) -> dict[str, Any]:
    return _json(entry)


def _snapshot_dict(snapshot: FilesystemSnapshot | FilesystemSnapshotV2) -> dict[str, Any]:
    """Serialize the frozen v2 summary wrapper without changing its read model."""
    value = _json(snapshot)
    if isinstance(snapshot, FilesystemSnapshotV2):
        value["payload_observation_summary"] = {
            "status_counts": [
                {"status": item.status.value, "count": item.count}
                for item in snapshot.payload_observation_summary
            ]
        }
    return value


def entry_id(snapshot_id: str, scope_id: str, relative_path: str) -> str:
    """Frozen deterministic entry identity shared by writer and validator."""
    return sha256(f"{snapshot_id}\0{scope_id}\0{relative_path}".encode()).hexdigest()


def snapshot_statistics(entries: list[dict[str, Any]]) -> dict[str, int]:
    """Compute frozen summary values from Entry facts only."""
    return {
        "entry_count": len(entries),
        "observed_count": sum(item["observation_status"] == "observed" for item in entries),
        "error_count": sum(item["observation_status"] != "observed" for item in entries),
        "excluded_count": sum(item["excluded"] for item in entries),
        "total_regular_file_bytes": sum(
            (item["size_bytes"] or 0) for item in entries if item["object_type"] == "regular_file"
        ),
        "max_depth_observed": max(
            (
                0 if item["relative_path"] == "." else len(item["relative_path"].split("/"))
                for item in entries
            ),
            default=0,
        ),
    }


def snapshot_entries_digest(entries: list[dict[str, Any]]) -> str:
    return _digest(entries)


def snapshot_metadata_digest(metadata: dict[str, Any]) -> str:
    value = dict(metadata)
    value.pop("snapshot_digest", None)
    value.pop("evidence_id", None)
    value.pop("evidence_relative_path", None)
    value.pop("entries", None)
    return _digest(value)


def _snapshot(
    config: StewardConfig, run: RunRecord, scopes: tuple[ScopeConfig, ...], budget: ScanBudget
) -> FilesystemSnapshot:
    snapshot_id = str(uuid.uuid4())
    started = utc_now()
    entries, partial = scan(config, snapshot_id, scopes, budget)
    completed = utc_now()
    entry_data = [_entry_dict(entry) for entry in entries]
    entries_digest = _digest(entry_data)
    observed = sum(
        entry.observation_status == FilesystemObservationStatus.OBSERVED for entry in entries
    )
    status = (
        SnapshotStatus.PARTIAL if partial or observed != len(entries) else SnapshotStatus.COMPLETE
    )
    meta: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "run_id": run.run_id,
        "created_at": completed,
        "started_at": started,
        "completed_at": completed,
        "status": status.value,
        "consistency": SnapshotConsistency.BEST_EFFORT_POINT_IN_TIME.value,
        "config_digest": compute_config_digest(config),
        "scope_ids": [scope.scope_id for scope in scopes],
        "budget": _json(budget),
        "entry_count": len(entries),
        "observed_count": observed,
        "error_count": len(entries) - observed,
        "excluded_count": sum(entry.excluded for entry in entries),
        "total_regular_file_bytes": sum(
            (entry.size_bytes or 0)
            for entry in entries
            if entry.object_type.value == "regular_file"
        ),
        "max_depth_observed": max(
            (
                0 if entry.relative_path == "." else len(entry.relative_path.split("/"))
                for entry in entries
            ),
            default=0,
        ),
        "entries_digest": entries_digest,
    }
    snapshot_digest = _digest(meta)
    return FilesystemSnapshot(
        snapshot_id,
        run.run_id,
        completed,
        started,
        completed,
        status,
        SnapshotConsistency.BEST_EFFORT_POINT_IN_TIME,
        meta["config_digest"],
        tuple(meta["scope_ids"]),
        budget,
        meta["entry_count"],
        observed,
        meta["error_count"],
        meta["excluded_count"],
        meta["total_regular_file_bytes"],
        meta["max_depth_observed"],
        entries_digest,
        snapshot_digest,
        None,
        None,
        entries,
    )


def _persist(
    config: StewardConfig,
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
    *,
    _fault_injector: FaultInjector | None = None,
) -> None:
    """Persist only intrinsically valid Snapshot Evidence before its SQLite projection."""
    conn = open_initialized(config)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (snapshot.run_id,)).fetchone()
        if row is None:
            raise SnapshotNotFoundError("snapshot Run not found")
        run = _run(row)
        payload = {"snapshot": _snapshot_dict(snapshot), "entries": [_json(entry) for entry in snapshot.entries]}
        schema_version = 2 if isinstance(snapshot, FilesystemSnapshotV2) else 1
        item = _evidence(run, "filesystem.snapshot", payload, schema_version=schema_version)
        validation = validate_snapshot_evidence(item)
        if not validation.valid:
            raise EvidenceError("constructed Snapshot Evidence failed intrinsic validation")
        if isinstance(snapshot, FilesystemSnapshotV2):
            reuse_errors = validate_snapshot_reuse_references(
                config, snapshot, connection=conn
            )
            if reuse_errors:
                raise EvidenceError("constructed Snapshot Evidence has invalid reuse references")
        fault_checkpoint(_fault_injector, "snapshot.persist", "before_evidence_publish")
        relative = write_evidence(config.paths.evidence_dir, item)
        fault_checkpoint(_fault_injector, "snapshot.persist", "after_evidence_publish")
        conn.execute(
            "INSERT INTO evidence_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item["evidence_id"],
                run.run_id,
                item["sequence"],
                item["evidence_type"],
                item["created_at"],
                relative,
                item["previous_evidence_digest"],
                item["evidence_digest"],
                schema_version,
            ),
        )
        conn.execute(
            "UPDATE runs SET last_sequence=?,last_evidence_digest=?,updated_at=? WHERE run_id=?",
            (item["sequence"], item["evidence_digest"], item["created_at"], run.run_id),
        )
        _insert_snapshot_index_rows(conn, snapshot, str(item["evidence_id"]), relative)
        fault_checkpoint(_fault_injector, "snapshot.persist", "before_index_commit")
        conn.commit()
        fault_checkpoint(_fault_injector, "snapshot.persist", "after_index_commit")
    finally:
        conn.close()


def snapshot_from_valid_evidence(
    evidence: dict[str, Any], evidence_relative_path: str
) -> FilesystemSnapshot:
    """Map already strictly validated Snapshot Evidence into the shared Snapshot model."""
    payload = cast(dict[str, Any], evidence["payload"])
    data = cast(dict[str, Any], payload["snapshot"])
    budget_data = cast(dict[str, Any], data["budget"])
    budget = ScanBudget(
        budget_data["max_entries"],
        budget_data["max_total_stat_bytes"],
        budget_data["max_duration_seconds"],
        budget_data["max_depth"],
    )
    entries = tuple(
        FilesystemEntry(
            item["entry_id"],
            item["snapshot_id"],
            item["scope_id"],
            item["relative_path"],
            FilesystemObjectType(item["object_type"]),
            item["device_id"],
            item["inode"],
            item["mode"],
            item["uid"],
            item["gid"],
            item["size_bytes"],
            item["mtime_ns"],
            item["ctime_ns"],
            item["birthtime_ns"],
            item["link_count"],
            item["symlink_target_raw"],
            item["readable"],
            item["writable"],
            item["executable"],
            FilesystemObservationStatus(item["observation_status"]),
            item["error_code"],
            item["error_message"],
            item["excluded"],
        )
        for item in cast(list[dict[str, Any]], payload["entries"])
    )
    return FilesystemSnapshot(
        data["snapshot_id"],
        data["run_id"],
        data["created_at"],
        data["started_at"],
        data["completed_at"],
        SnapshotStatus(data["status"]),
        SnapshotConsistency(data["consistency"]),
        data["config_digest"],
        tuple(data["scope_ids"]),
        budget,
        data["entry_count"],
        data["observed_count"],
        data["error_count"],
        data["excluded_count"],
        data["total_regular_file_bytes"],
        data["max_depth_observed"],
        data["entries_digest"],
        data["snapshot_digest"],
        evidence["evidence_id"],
        evidence_relative_path,
        entries,
    )


def _insert_snapshot_index_rows(
    conn: Any,
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
    evidence_id: str,
    evidence_relative_path: str,
) -> None:
    """Shared Snapshot/Entry derived-index writer for creation and isolated replay."""
    hash_policy: str | None
    payload_summary: str | None
    allocated_known_sum: int | None
    allocated_unknown_count: int | None
    if isinstance(snapshot, FilesystemSnapshotV2):
        schema_version = 2
        hash_policy = canonical_json(_json(snapshot.hash_policy)).decode("utf-8")
        allocated_known_sum = snapshot.allocated_regular_file_bytes_known_sum
        allocated_unknown_count = snapshot.allocated_regular_file_unknown_count
        payload_summary = canonical_json(
            {
                "status_counts": [
                    {"status": item.status.value, "count": item.count}
                    for item in snapshot.payload_observation_summary
                ]
            }
        ).decode("utf-8")
    else:
        schema_version = 1
        hash_policy = payload_summary = None
        allocated_known_sum = allocated_unknown_count = None
    conn.execute(
        "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot.snapshot_id,
            snapshot.run_id,
            snapshot.status.value,
            snapshot.consistency.value,
            snapshot.created_at,
            snapshot.started_at,
            snapshot.completed_at,
            snapshot.config_digest,
            json.dumps(snapshot.scope_ids),
            canonical_json(_json(snapshot.budget)).decode(),
            snapshot.entry_count,
            snapshot.observed_count,
            snapshot.error_count,
            snapshot.excluded_count,
            snapshot.total_regular_file_bytes,
            snapshot.max_depth_observed,
            snapshot.entries_digest,
            snapshot.snapshot_digest,
            evidence_id,
            evidence_relative_path,
            schema_version,
            hash_policy,
            allocated_known_sum,
            allocated_unknown_count,
            payload_summary,
        ),
    )
    conn.executemany(
        "INSERT INTO snapshot_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                snapshot.snapshot_id,
                entry.scope_id,
                entry.relative_path,
                entry.entry_id,
                entry.object_type.value,
                entry.device_id,
                entry.inode,
                entry.mode,
                entry.uid,
                entry.gid,
                entry.size_bytes,
                entry.mtime_ns,
                entry.ctime_ns,
                entry.birthtime_ns,
                entry.link_count,
                entry.symlink_target_raw,
                int(entry.readable),
                int(entry.writable),
                int(entry.executable),
                entry.observation_status.value,
                entry.error_code,
                entry.error_message,
                int(entry.excluded),
                _entry_allocated_size(entry),
                _entry_payload_projection(entry),
            )
            for entry in snapshot.entries
        ],
    )


def _entry_allocated_size(entry: FilesystemEntry | FilesystemEntryV2) -> int | None:
    return entry.allocated_size_bytes if isinstance(entry, FilesystemEntryV2) else None


def _entry_payload_projection(entry: FilesystemEntry | FilesystemEntryV2) -> str | None:
    if not isinstance(entry, FilesystemEntryV2):
        return None
    observation = entry.payload_observation
    return canonical_json(
        {
            "status": observation.status.value,
            "algorithm": observation.algorithm,
            "algorithm_version": observation.algorithm_version,
            "digest": observation.digest,
            "bytes_hashed": observation.bytes_hashed,
            "provenance": observation.provenance.value if observation.provenance else None,
            "reused_from_snapshot_id": observation.reused_from_snapshot_id,
            "failure_code": observation.failure_code,
            "os_error_code": observation.os_error_code,
        }
    ).decode("utf-8")


def _metadata_reuse_eligible(
    current: FilesystemEntry | FilesystemEntryV2, source: FilesystemEntryV2
) -> bool:
    """The frozen same-location continuity predicate, never a content relation."""
    if (
        current.scope_id != source.scope_id
        or current.relative_path != source.relative_path
        or current.object_type != FilesystemObjectType.REGULAR_FILE
        or source.object_type != FilesystemObjectType.REGULAR_FILE
        or current.observation_status != FilesystemObservationStatus.OBSERVED
        or source.observation_status != FilesystemObservationStatus.OBSERVED
        or current.excluded
        or source.excluded
    ):
        return False
    required = ("device_id", "inode", "size_bytes", "mtime_ns", "ctime_ns")
    if any(getattr(current, name) is None or getattr(source, name) is None for name in required):
        return False
    if any(getattr(current, name) != getattr(source, name) for name in required):
        return False
    return (
        current.birthtime_ns == source.birthtime_ns
        if current.birthtime_ns is not None and source.birthtime_ns is not None
        else current.birthtime_ns is None and source.birthtime_ns is None
    )


def _direct_reuse_source_eligible(
    current: FilesystemEntry | FilesystemEntryV2,
    source: FilesystemEntryV2,
    policy: PayloadHashPolicy,
) -> bool:
    observation = source.payload_observation
    return (
        _metadata_reuse_eligible(current, source)
        and observation.status
        in {PayloadObservationStatus.HASHED, PayloadObservationStatus.EMPTY_FILE_HASHED}
        and observation.provenance == PayloadObservationProvenance.DIRECT_READ
        and observation.reused_from_snapshot_id is None
        and observation.algorithm == policy.algorithm
        and observation.algorithm_version == policy.algorithm_version
        and isinstance(observation.digest, str)
        and len(observation.digest) == 64
        and all(value in "0123456789abcdef" for value in observation.digest)
        and observation.bytes_hashed == source.size_bytes
    )


class _VerifiedReuseSourceSelector:
    """Ephemeral Evidence-backed source selection for one current Snapshot."""

    def __init__(self, config: StewardConfig, current_started_at: str, policy: PayloadHashPolicy):
        self.config = config
        self.current_started_at = current_started_at
        self.policy = policy
        self._candidates: tuple[FilesystemSnapshotV2, ...] | None = None

    def _load_candidates(self) -> tuple[FilesystemSnapshotV2, ...]:
        if self._candidates is not None:
            return self._candidates
        runs_root = self.config.paths.evidence_dir / "runs"
        if not runs_root.is_dir() or runs_root.is_symlink():
            raise EvidenceError("verified reuse source repository is unavailable")
        candidates: list[FilesystemSnapshotV2] = []
        for directory in sorted(runs_root.iterdir(), key=lambda item: item.name):
            if directory.is_symlink() or not directory.is_dir():
                continue
            files, _errors = load_run_files(self.config.paths.evidence_dir, directory.name)
            for path, evidence in files:
                if (
                    evidence.get("evidence_type") != "filesystem.snapshot"
                    or snapshot_evidence_schema_version(evidence) != 2
                ):
                    continue
                intrinsic = validate_snapshot_evidence(evidence)
                if not intrinsic.valid:
                    continue
                snapshot = snapshot_v2_from_valid_evidence(
                    evidence, str(path.relative_to(self.config.paths.evidence_dir))
                )
                if snapshot.completed_at <= self.current_started_at:
                    candidates.append(snapshot)
        self._candidates = tuple(
            sorted(
                candidates,
                key=lambda item: (item.completed_at, item.created_at, item.snapshot_id),
                reverse=True,
            )
        )
        return self._candidates

    def resolve(self, current: FilesystemEntry) -> PayloadObservation | None:
        for candidate in self._load_candidates():
            # The established verifier validates the persisted Evidence, index,
            # Run lifecycle, and any pre-existing reuse references.
            if verify_snapshot(self.config, candidate.snapshot_id).status != "VALID":
                continue
            source = next(
                (
                    entry
                    for entry in candidate.entries
                    if (entry.scope_id, entry.relative_path)
                    == (current.scope_id, current.relative_path)
                ),
                None,
            )
            if source is None or not _direct_reuse_source_eligible(current, source, self.policy):
                continue
            observation = source.payload_observation
            return PayloadObservation(
                observation.status,
                observation.algorithm,
                observation.algorithm_version,
                observation.digest,
                observation.bytes_hashed,
                PayloadObservationProvenance.REUSED_FROM_VERIFIED_SNAPSHOT,
                candidate.snapshot_id,
                None,
                None,
            )
        return None


def _snapshot_v2(
    config: StewardConfig,
    snapshot: FilesystemSnapshot,
    scopes: tuple[ScopeConfig, ...],
    policy: PayloadHashPolicy,
    *,
    locality_provider: LocalityProvider,
) -> FilesystemSnapshotV2:
    """Add bounded payload facts to an already complete metadata observation."""
    selector = (
        _VerifiedReuseSourceSelector(config, snapshot.started_at, policy)
        if policy.allow_verified_reuse
        else None
    )
    entries = observe_payloads(
        snapshot.entries,
        scopes,
        policy,
        locality_provider=locality_provider,
        reuse_resolver=selector.resolve if selector is not None else None,
    )
    entry_data = [_json(entry) for entry in entries]
    statuses = [entry.payload_observation.status for entry in entries]
    summary = tuple(
        PayloadObservationCount(status, statuses.count(status))
        for status in sorted(set(statuses), key=lambda item: item.value)
    )
    allocated_unknown_count = sum(
        entry.object_type == FilesystemObjectType.REGULAR_FILE for entry in entries
    )
    entries_digest = snapshot_v2_entries_digest(entry_data)
    provisional = FilesystemSnapshotV2(
        2,
        snapshot.snapshot_id,
        snapshot.run_id,
        snapshot.created_at,
        snapshot.started_at,
        snapshot.completed_at,
        snapshot.status,
        snapshot.consistency,
        snapshot.config_digest,
        snapshot.scope_ids,
        snapshot.budget,
        snapshot.entry_count,
        snapshot.observed_count,
        snapshot.error_count,
        snapshot.excluded_count,
        snapshot.total_regular_file_bytes,
        snapshot.max_depth_observed,
        policy,
        0,
        allocated_unknown_count,
        summary,
        entries_digest,
        "",
        None,
        None,
        entries,
    )
    snapshot_digest = snapshot_v2_metadata_digest(_snapshot_dict(provisional))
    return FilesystemSnapshotV2(
        provisional.snapshot_schema_version,
        provisional.snapshot_id,
        provisional.run_id,
        provisional.created_at,
        provisional.started_at,
        provisional.completed_at,
        provisional.status,
        provisional.consistency,
        provisional.config_digest,
        provisional.scope_ids,
        provisional.budget,
        provisional.entry_count,
        provisional.observed_count,
        provisional.error_count,
        provisional.excluded_count,
        provisional.total_regular_file_bytes,
        provisional.max_depth_observed,
        provisional.hash_policy,
        provisional.allocated_regular_file_bytes_known_sum,
        provisional.allocated_regular_file_unknown_count,
        provisional.payload_observation_summary,
        provisional.entries_digest,
        snapshot_digest,
        None,
        None,
        provisional.entries,
    )


def create_snapshot(
    config: StewardConfig,
    scope_ids: tuple[str, ...],
    budget: ScanBudget,
    payload_hash_policy: PayloadHashPolicy | None = None,
    *,
    locality_provider: LocalityProvider = unknown_locality,
) -> FilesystemSnapshot | FilesystemSnapshotV2:
    """Create v1 by default; explicit validated policy selects v2 direct reads."""
    if payload_hash_policy is not None:
        validate_payload_hash_policy(payload_hash_policy)
    scopes = select_scopes(config, scope_ids)
    run = create_run(config, "filesystem.snapshot")
    transition_run(config, run.run_id, RunStatus.SCANNING, "filesystem snapshot started")
    current = get_run(config, run.run_id)
    base_snapshot = _snapshot(config, current, scopes, budget)
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2 = base_snapshot
    if payload_hash_policy is not None:
        snapshot = _snapshot_v2(
            config, base_snapshot, scopes, payload_hash_policy, locality_provider=locality_provider
        )
    _persist(config, snapshot)
    transition_run(
        config,
        run.run_id,
        RunStatus.PARTIAL if snapshot.status == SnapshotStatus.PARTIAL else RunStatus.SCANNED,
        "filesystem snapshot complete",
    )
    return snapshot


def list_snapshots(
    config: StewardConfig, limit: int | None = 50
) -> list[FilesystemSnapshotSummary]:
    """List persisted Snapshot summaries in descending creation order.

    ``limit=None`` is the repository's complete read-only view, used by status
    review to find the most recent VALID facts without guessing a fixed bound.
    """
    with open_readonly_initialized(config) as conn:
        return _list_snapshots(conn, limit)


def _list_snapshots(
    conn: sqlite3.Connection, limit: int | None = 50
) -> list[FilesystemSnapshotSummary]:
    query = "SELECT * FROM snapshots ORDER BY created_at DESC"
    parameters: tuple[int, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        parameters = (limit,)
    return [
        FilesystemSnapshotSummary(
            row["snapshot_id"], row["run_id"], SnapshotStatus(row["status"]), row["created_at"],
            tuple(json.loads(row["scope_ids_json"])), row["entry_count"], row["observed_count"],
            row["error_count"], row["snapshot_digest"],
        )
        for row in conn.execute(query, parameters)
    ]


def _entry_from_row(row: Any) -> FilesystemEntry | FilesystemEntryV2:
    entry = FilesystemEntry(
        row["entry_id"],
        row["snapshot_id"],
        row["scope_id"],
        row["relative_path"],
        FilesystemObjectType(row["object_type"]),
        row["device_id"],
        row["inode"],
        row["mode"],
        row["uid"],
        row["gid"],
        row["size_bytes"],
        row["mtime_ns"],
        row["ctime_ns"],
        row["birthtime_ns"],
        row["link_count"],
        row["symlink_target_raw"],
        bool(row["readable"]),
        bool(row["writable"]),
        bool(row["executable"]),
        FilesystemObservationStatus(row["observation_status"]),
        row["error_code"],
        row["error_message"],
        bool(row["excluded"]),
    )
    if row["payload_observation_json"] is None:
        return entry
    payload = json.loads(row["payload_observation_json"])
    return FilesystemEntryV2(
        entry.entry_id,
        entry.snapshot_id,
        entry.scope_id,
        entry.relative_path,
        entry.object_type,
        entry.device_id,
        entry.inode,
        entry.mode,
        entry.uid,
        entry.gid,
        entry.size_bytes,
        entry.mtime_ns,
        entry.ctime_ns,
        entry.birthtime_ns,
        entry.link_count,
        entry.symlink_target_raw,
        entry.readable,
        entry.writable,
        entry.executable,
        entry.observation_status,
        entry.error_code,
        entry.error_message,
        entry.excluded,
        row["allocated_size_bytes"],
        PayloadObservation(
            PayloadObservationStatus(payload["status"]),
            payload["algorithm"],
            payload["algorithm_version"],
            payload["digest"],
            payload["bytes_hashed"],
            (
                PayloadObservationProvenance(payload["provenance"])
                if payload["provenance"] is not None
                else None
            ),
            payload["reused_from_snapshot_id"],
            payload["failure_code"],
            payload["os_error_code"],
        ),
    )


def _validate_page(limit: int, offset: int, path_prefix: str | None) -> None:
    if not 1 <= limit <= 1000:
        raise SnapshotBudgetError("QUERY_LIMIT_INVALID: limit must be 1 through 1000")
    if offset < 0:
        raise SnapshotBudgetError("QUERY_OFFSET_INVALID: offset must be non-negative")
    if path_prefix is not None and (
        path_prefix.startswith("/") or path_prefix == ".." or path_prefix.startswith("../")
    ):
        raise SnapshotBudgetError("QUERY_PATH_PREFIX_INVALID: path-prefix must be relative")


def _snapshot_from_row(
    row: Any, entries: tuple[FilesystemEntry | FilesystemEntryV2, ...]
) -> FilesystemSnapshot | FilesystemSnapshotV2:
    """Map a derived-index row into the shared Snapshot model."""
    if row["snapshot_evidence_schema_version"] == 2:
        if not all(isinstance(entry, FilesystemEntryV2) for entry in entries):
            raise StorageCorruptionError("v2 Snapshot index rows are missing payload observations")
        policy = json.loads(row["hash_policy_json"])
        summary = json.loads(row["payload_observation_summary_json"])
        return FilesystemSnapshotV2(
            2,
            row["snapshot_id"],
            row["run_id"],
            row["created_at"],
            row["started_at"],
            row["completed_at"],
            SnapshotStatus(row["status"]),
            SnapshotConsistency(row["consistency"]),
            row["config_digest"],
            tuple(json.loads(row["scope_ids_json"])),
            ScanBudget(**json.loads(row["budget_json"])),
            row["entry_count"],
            row["observed_count"],
            row["error_count"],
            row["excluded_count"],
            row["total_regular_file_bytes"],
            row["max_depth_observed"],
            PayloadHashPolicy(**policy),
            row["allocated_regular_file_bytes_known_sum"],
            row["allocated_regular_file_unknown_count"],
            tuple(
                PayloadObservationCount(PayloadObservationStatus(item["status"]), item["count"])
                for item in summary["status_counts"]
            ),
            row["entries_digest"],
            row["snapshot_digest"],
            row["evidence_id"],
            row["evidence_relative_path"],
            cast(tuple[FilesystemEntryV2, ...], entries),
        )
    return FilesystemSnapshot(
        row["snapshot_id"],
        row["run_id"],
        row["created_at"],
        row["started_at"],
        row["completed_at"],
        SnapshotStatus(row["status"]),
        SnapshotConsistency(row["consistency"]),
        row["config_digest"],
        tuple(json.loads(row["scope_ids_json"])),
        ScanBudget(**json.loads(row["budget_json"])),
        row["entry_count"],
        row["observed_count"],
        row["error_count"],
        row["excluded_count"],
        row["total_regular_file_bytes"],
        row["max_depth_observed"],
        row["entries_digest"],
        row["snapshot_digest"],
        row["evidence_id"],
        row["evidence_relative_path"],
        cast(tuple[FilesystemEntry, ...], entries),
    )


def get_snapshot(config: StewardConfig, snapshot_id: str) -> FilesystemSnapshot | FilesystemSnapshotV2:
    with open_readonly_initialized(config) as conn:
        return _get_snapshot(conn, snapshot_id)


def _get_snapshot(
    conn: sqlite3.Connection, snapshot_id: str
) -> FilesystemSnapshot | FilesystemSnapshotV2:
    row = conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise SnapshotNotFoundError(f"snapshot not found: {snapshot_id}")
    entries = tuple(
        _entry_from_row(item)
        for item in conn.execute(
            "SELECT * FROM snapshot_entries WHERE snapshot_id=? ORDER BY scope_id, relative_path",
            (snapshot_id,),
        )
    )
    return _snapshot_from_row(row, entries)


def list_snapshot_entries(
    config: StewardConfig,
    snapshot_id: str,
    scope_id: str | None = None,
    object_type: FilesystemObjectType | None = None,
    observation_status: FilesystemObservationStatus | None = None,
    path_prefix: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> SnapshotEntryPage:
    """Read paginated derived entry rows only; never touch scopes or evidence."""
    _validate_page(limit, offset, path_prefix)
    with open_readonly_initialized(config) as conn:
        return _list_snapshot_entries(
            conn, snapshot_id, scope_id, object_type, observation_status, path_prefix, limit, offset
        )


def verified_snapshot_document_entries(
    config: StewardConfig,
    snapshot_id: str,
    scope_id: str | None = None,
) -> tuple[
    SnapshotVerificationResult,
    FilesystemSnapshot | FilesystemSnapshotV2,
    tuple[FilesystemEntry | FilesystemEntryV2, ...],
]:
    """Return verified regular-file metadata from one guarded database identity."""

    with open_readonly_initialized(config) as conn:
        verification = _verify_snapshot(config, conn, snapshot_id)
        snapshot = _get_snapshot(conn, snapshot_id)
        if scope_id is not None and scope_id not in snapshot.scope_ids:
            raise SnapshotScopeError(
                f"unknown historical scope_id for Snapshot {snapshot_id}: {scope_id}"
            )
        if verification.status != "VALID":
            raise StorageCorruptionError("Snapshot document discovery requires VALID Evidence")
        entries = tuple(
            entry
            for entry in snapshot.entries
            if (scope_id is None or entry.scope_id == scope_id)
            and entry.object_type == FilesystemObjectType.REGULAR_FILE
            and not entry.excluded
        )
        return verification, snapshot, entries


def _list_snapshot_entries(
    conn: sqlite3.Connection,
    snapshot_id: str,
    scope_id: str | None = None,
    object_type: FilesystemObjectType | None = None,
    observation_status: FilesystemObservationStatus | None = None,
    path_prefix: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> SnapshotEntryPage:
    if conn.execute("SELECT 1 FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone() is None:
        raise SnapshotNotFoundError(f"snapshot not found: {snapshot_id}")
    sql = "SELECT * FROM snapshot_entries WHERE snapshot_id=?"
    values: list[Any] = [snapshot_id]
    if scope_id is not None:
        sql += " AND scope_id=?"
        values.append(scope_id)
    if object_type is not None:
        sql += " AND object_type=?"
        values.append(object_type.value)
    if observation_status is not None:
        sql += " AND observation_status=?"
        values.append(observation_status.value)
    if path_prefix is not None and path_prefix != ".":
        escaped = path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND (relative_path=? OR relative_path LIKE ? ESCAPE '\\')"
        values.extend([path_prefix, f"{escaped}/%"])
    sql += " ORDER BY scope_id, relative_path LIMIT ? OFFSET ?"
    values.extend([limit + 1, offset])
    rows = list(conn.execute(sql, values))
    has_more = len(rows) > limit
    rows = rows[:limit]
    entries = tuple(_entry_from_row(row) for row in rows)
    return SnapshotEntryPage(snapshot_id, entries, len(entries), limit, offset, has_more)


def _validate_snapshot_evidence_v1(evidence: dict[str, Any]) -> SnapshotEvidenceValidationResult:
    """Validate one snapshot Evidence fact without SQLite, Run, or filesystem access."""
    errors: list[dict[str, str]] = []

    def problem(code: str, message: str) -> None:
        errors.append({"code": code, "message": message})

    envelope_fields = {
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
    envelope_valid = isinstance(evidence, dict) and set(evidence) == envelope_fields
    if not envelope_valid:
        problem("SNAPSHOT_EVIDENCE_SCHEMA_INVALID", "invalid evidence envelope schema")
    evidence_id = (
        evidence.get("evidence_id")
        if isinstance(evidence, dict) and isinstance(evidence.get("evidence_id"), str)
        else None
    )
    evidence_type_valid = (
        evidence.get("evidence_type") == "filesystem.snapshot"
        if isinstance(evidence, dict)
        else False
    )
    if not evidence_type_valid:
        problem("SNAPSHOT_EVIDENCE_TYPE_INVALID", "evidence_type must be filesystem.snapshot")
    from .evidence import digest

    evidence_digest_valid = (
        isinstance(evidence, dict)
        and isinstance(evidence.get("evidence_digest"), str)
        and evidence.get("evidence_digest") == digest(evidence)
    )
    if not evidence_digest_valid:
        problem("SNAPSHOT_EVIDENCE_INVALID", "evidence digest mismatch")
    payload = evidence.get("payload") if isinstance(evidence, dict) else None
    payload_schema_valid = isinstance(payload, dict) and set(payload) == {"snapshot", "entries"}
    if not payload_schema_valid:
        problem("SNAPSHOT_PAYLOAD_SCHEMA_INVALID", "payload must contain snapshot and entries")
    snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
    entries = payload.get("entries") if isinstance(payload, dict) else None
    snapshot_id = (
        snapshot.get("snapshot_id")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("snapshot_id"), str)
        else None
    )
    snapshot_fields = {
        "snapshot_id",
        "run_id",
        "created_at",
        "started_at",
        "completed_at",
        "status",
        "consistency",
        "config_digest",
        "scope_ids",
        "budget",
        "entry_count",
        "observed_count",
        "error_count",
        "excluded_count",
        "total_regular_file_bytes",
        "max_depth_observed",
        "entries_digest",
        "snapshot_digest",
        "entries",
    }
    snapshot_fields_current = snapshot_fields | {"evidence_id", "evidence_relative_path"}
    snapshot_schema_valid = (
        isinstance(snapshot, dict)
        and (set(snapshot) == snapshot_fields or set(snapshot) == snapshot_fields_current)
        and isinstance(entries, list)
    )
    if not snapshot_schema_valid:
        problem("SNAPSHOT_PAYLOAD_SCHEMA_INVALID", "invalid snapshot schema")
    snapshot_data: dict[str, Any] = snapshot if isinstance(snapshot, dict) else {}
    run_id_consistent = snapshot_schema_valid and snapshot_data.get("run_id") == evidence.get(
        "run_id"
    )
    if not run_id_consistent:
        problem("SNAPSHOT_RUN_ID_MISMATCH", "snapshot run_id differs from evidence run_id")
    order_valid = keys_unique = ids_valid = scopes_valid = paths_valid = entries_schema_valid = True
    expected_fields = {
        "entry_id",
        "snapshot_id",
        "scope_id",
        "relative_path",
        "object_type",
        "device_id",
        "inode",
        "mode",
        "uid",
        "gid",
        "size_bytes",
        "mtime_ns",
        "ctime_ns",
        "birthtime_ns",
        "link_count",
        "symlink_target_raw",
        "readable",
        "writable",
        "executable",
        "observation_status",
        "error_code",
        "error_message",
        "excluded",
    }
    if not isinstance(entries, list):
        entries = []
        entries_schema_valid = False
    scopes = snapshot_data.get("scope_ids", [])
    seen_keys: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    previous: tuple[str, bytes] | None = None
    for item in entries:
        if not isinstance(item, dict) or set(item) != expected_fields:
            entries_schema_valid = False
            continue
        scope = item.get("scope_id")
        path = item.get("relative_path")
        if (
            item.get("snapshot_id") != snapshot_id
            or not isinstance(scope, str)
            or scope not in scopes
        ):
            scopes_valid = False
        if (
            not isinstance(path, str)
            or path.startswith("/")
            or path == ".."
            or path.startswith("../")
            or "/../" in path
        ):
            paths_valid = False
        if not all(
            isinstance(item.get(name), bool)
            for name in ("readable", "writable", "executable", "excluded")
        ):
            entries_schema_valid = False
        if item.get("entry_id") != entry_id(str(snapshot_id), str(scope), str(path)):
            ids_valid = False
        key = (str(scope), str(path))
        keys_unique = keys_unique and key not in seen_keys
        seen_keys.add(key)
        identifier = str(item.get("entry_id"))
        ids_valid = ids_valid and identifier not in seen_ids
        seen_ids.add(identifier)
        sort_key = (str(scope), str(path).encode("utf-8", "surrogateescape"))
        order_valid = order_valid and (previous is None or previous <= sort_key)
        previous = sort_key
    for ok, code, message in (
        (entries_schema_valid, "SNAPSHOT_ENTRY_SCHEMA_INVALID", "invalid entry schema"),
        (order_valid, "SNAPSHOT_ENTRY_ORDER_INVALID", "entries are not sorted"),
        (keys_unique, "SNAPSHOT_ENTRY_DUPLICATE", "duplicate entry key"),
        (ids_valid, "SNAPSHOT_ENTRY_ID_INVALID", "invalid entry id"),
        (scopes_valid, "SNAPSHOT_SCOPE_MISMATCH", "invalid entry scope"),
        (paths_valid, "SNAPSHOT_PATH_INVALID", "invalid entry path"),
    ):
        if not ok:
            problem(code, message)
    summary_valid = snapshot_schema_valid and all(
        snapshot_data.get(key) == value for key, value in snapshot_statistics(entries).items()
    )
    if not summary_valid:
        problem("SNAPSHOT_SUMMARY_INVALID", "snapshot summary mismatch")
    entries_digest_valid = snapshot_schema_valid and snapshot_data.get(
        "entries_digest"
    ) == snapshot_entries_digest(entries)
    if not entries_digest_valid:
        problem("SNAPSHOT_ENTRIES_DIGEST_INVALID", "entries digest mismatch")
    snapshot_digest_valid = snapshot_schema_valid and snapshot_data.get(
        "snapshot_digest"
    ) == snapshot_metadata_digest(snapshot_data)
    if not snapshot_digest_valid:
        problem("SNAPSHOT_DIGEST_INVALID", "snapshot digest mismatch")
    return SnapshotEvidenceValidationResult(
        evidence_id,
        snapshot_id,
        not errors,
        envelope_valid,
        evidence_digest_valid,
        evidence_type_valid,
        payload_schema_valid,
        bool(run_id_consistent),
        snapshot_schema_valid,
        entries_schema_valid,
        order_valid,
        keys_unique,
        ids_valid,
        scopes_valid,
        paths_valid,
        summary_valid,
        entries_digest_valid,
        snapshot_digest_valid,
        tuple(sorted(errors, key=lambda value: (value["code"], value["message"]))),
    )


_V2_ENVELOPE_FIELDS = {
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
_V2_SNAPSHOT_FIELDS = {
    "snapshot_schema_version",
    "snapshot_id",
    "run_id",
    "created_at",
    "started_at",
    "completed_at",
    "status",
    "consistency",
    "config_digest",
    "scope_ids",
    "budget",
    "entry_count",
    "observed_count",
    "error_count",
    "excluded_count",
    "total_regular_file_bytes",
    "max_depth_observed",
    "hash_policy",
    "allocated_regular_file_bytes_known_sum",
    "allocated_regular_file_unknown_count",
    "payload_observation_summary",
    "entries_digest",
    "snapshot_digest",
    "evidence_id",
    "evidence_relative_path",
    "entries",
}
_V2_ENTRY_FIELDS = {
    "entry_id",
    "snapshot_id",
    "scope_id",
    "relative_path",
    "object_type",
    "device_id",
    "inode",
    "mode",
    "uid",
    "gid",
    "size_bytes",
    "mtime_ns",
    "ctime_ns",
    "birthtime_ns",
    "link_count",
    "symlink_target_raw",
    "readable",
    "writable",
    "executable",
    "observation_status",
    "error_code",
    "error_message",
    "excluded",
    "allocated_size_bytes",
    "payload_observation",
}
_V2_PAYLOAD_FIELDS = {
    "status",
    "algorithm",
    "algorithm_version",
    "digest",
    "bytes_hashed",
    "provenance",
    "reused_from_snapshot_id",
    "failure_code",
    "os_error_code",
}
_V2_HASH_POLICY_FIELDS = {
    "algorithm",
    "algorithm_version",
    "max_hash_file_bytes",
    "max_total_hash_bytes",
    "max_hash_duration_seconds",
    "hash_chunk_size",
    "allow_non_local_content",
    "allow_verified_reuse",
}
_V2_SUCCESS_STATUSES = {
    PayloadObservationStatus.HASHED.value,
    PayloadObservationStatus.EMPTY_FILE_HASHED.value,
}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_optional_nonnegative_int(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _v2_entry_sort_key(item: dict[str, Any]) -> tuple[str, bytes]:
    return (
        str(item.get("scope_id", "")),
        str(item.get("relative_path", "")).encode("utf-8", "surrogateescape"),
    )


def canonical_snapshot_v2_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the frozen scoped-location ordering without mutating caller input."""
    return sorted(entries, key=_v2_entry_sort_key)


def snapshot_v2_entries_digest(entries: list[dict[str, Any]]) -> str:
    """Digest all ordered v2 Entry facts in their separate integrity domain."""
    return _digest(
        {
            "domain": "local_steward.snapshot.entries.v2",
            "snapshot_schema_version": 2,
            "entries": canonical_snapshot_v2_entries(entries),
        }
    )


def snapshot_v2_metadata_digest(metadata: dict[str, Any]) -> str:
    """Digest v2 Snapshot metadata without repeating Entry/Evidence identity facts."""
    value = dict(metadata)
    value.pop("snapshot_digest", None)
    value.pop("evidence_id", None)
    value.pop("evidence_relative_path", None)
    value.pop("entries", None)
    return _digest(
        {
            "domain": "local_steward.snapshot.metadata.v2",
            "snapshot": value,
        }
    )


def snapshot_evidence_schema_version(evidence: object) -> int | None:
    """The one version-dispatch entry; None denotes a malformed discriminator."""
    if not isinstance(evidence, dict):
        return None
    value = evidence.get("schema_version")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _version_result(
    evidence: object, code: str, message: str
) -> SnapshotEvidenceValidationResult:
    evidence_id = (
        evidence.get("evidence_id")
        if isinstance(evidence, dict) and isinstance(evidence.get("evidence_id"), str)
        else None
    )
    return SnapshotEvidenceValidationResult(
        evidence_id=evidence_id,
        snapshot_id=None,
        valid=False,
        envelope_valid=False,
        evidence_digest_valid=False,
        evidence_type_valid=False,
        payload_schema_valid=False,
        run_id_consistent=False,
        snapshot_schema_valid=False,
        entries_schema_valid=False,
        entry_order_valid=False,
        entry_keys_unique=False,
        entry_ids_valid=False,
        scope_membership_valid=False,
        paths_valid=False,
        summary_valid=False,
        entries_digest_valid=False,
        snapshot_digest_valid=False,
        errors=({"code": code, "message": message},),
    )


def _valid_hash_policy(policy: object) -> bool:
    if not isinstance(policy, dict) or set(policy) != _V2_HASH_POLICY_FIELDS:
        return False
    duration = policy["max_hash_duration_seconds"]
    return (
        policy["algorithm"] == "sha256"
        and _is_int(policy["algorithm_version"])
        and policy["algorithm_version"] > 0
        and all(
            _is_optional_nonnegative_int(policy[name])
            for name in ("max_hash_file_bytes", "max_total_hash_bytes")
        )
        and (
            duration is None
            or (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and math.isfinite(duration)
                and duration > 0
            )
        )
        and _is_int(policy["hash_chunk_size"])
        and policy["hash_chunk_size"] > 0
        and isinstance(policy["allow_non_local_content"], bool)
        and isinstance(policy["allow_verified_reuse"], bool)
    )


def _valid_payload_observation(item: dict[str, Any]) -> bool:
    value = item.get("payload_observation")
    if not isinstance(value, dict) or set(value) != _V2_PAYLOAD_FIELDS:
        return False
    status = value["status"]
    if status not in PayloadObservationStatus._value2member_map_:
        return False
    success = status in _V2_SUCCESS_STATUSES
    algorithm = value["algorithm"]
    version = value["algorithm_version"]
    digest_value = value["digest"]
    bytes_hashed = value["bytes_hashed"]
    provenance = value["provenance"]
    source = value["reused_from_snapshot_id"]
    failure_code = value["failure_code"]
    os_error_code = value["os_error_code"]
    if not _is_optional_nonnegative_int(os_error_code):
        return False
    if failure_code is not None and (not isinstance(failure_code, str) or not failure_code):
        return False
    if not success:
        return (
            algorithm is None
            and version is None
            and digest_value is None
            and bytes_hashed is None
            and provenance is None
            and source is None
        )
    if (
        algorithm != "sha256"
        or not _is_int(version)
        or version <= 0
        or not isinstance(digest_value, str)
        or len(digest_value) != 64
        or any(character not in "0123456789abcdef" for character in digest_value)
        or not _is_int(bytes_hashed)
        or bytes_hashed < 0
        or failure_code is not None
        or os_error_code is not None
        or provenance not in PayloadObservationProvenance._value2member_map_
        or item.get("object_type") != FilesystemObjectType.REGULAR_FILE.value
        or not _is_int(item.get("size_bytes"))
        or item["size_bytes"] < 0
        or bytes_hashed != item["size_bytes"]
    ):
        return False
    if status == PayloadObservationStatus.HASHED.value and item["size_bytes"] == 0:
        return False
    if status == PayloadObservationStatus.EMPTY_FILE_HASHED.value and item["size_bytes"] != 0:
        return False
    if provenance == PayloadObservationProvenance.DIRECT_READ.value:
        return source is None
    if not isinstance(source, str):
        return False
    try:
        uuid.UUID(source)
    except ValueError:
        return False
    return True


def _v2_entry_schema_valid(item: object) -> bool:
    if not isinstance(item, dict) or set(item) != _V2_ENTRY_FIELDS:
        return False
    if not all(isinstance(item[name], str) for name in ("entry_id", "snapshot_id", "scope_id", "relative_path")):
        return False
    if item["object_type"] not in FilesystemObjectType._value2member_map_:
        return False
    if item["observation_status"] not in FilesystemObservationStatus._value2member_map_:
        return False
    if not all(isinstance(item[name], bool) for name in ("readable", "writable", "executable", "excluded")):
        return False
    if not all(
        value is None or _is_int(value)
        for value in (
            item["device_id"], item["inode"], item["mode"], item["uid"], item["gid"],
            item["size_bytes"], item["mtime_ns"], item["ctime_ns"], item["birthtime_ns"],
            item["link_count"],
        )
    ):
        return False
    if not all(item[name] is None or isinstance(item[name], str) for name in ("symlink_target_raw", "error_code", "error_message")):
        return False
    return _is_optional_nonnegative_int(item["allocated_size_bytes"]) and _valid_payload_observation(item)


def _validate_snapshot_evidence_v2(evidence: dict[str, Any]) -> SnapshotEvidenceValidationResult:
    """Validate one v2 Snapshot fact without SQLite, Run, or filesystem access."""
    errors: list[dict[str, str]] = []

    def problem(code: str, message: str) -> None:
        errors.append({"code": code, "message": message})

    envelope_valid = set(evidence) == _V2_ENVELOPE_FIELDS and evidence.get("schema_version") == 2
    if not envelope_valid:
        problem("SNAPSHOT_EVIDENCE_SCHEMA_INVALID", "invalid v2 evidence envelope schema")
    evidence_id = evidence.get("evidence_id") if isinstance(evidence.get("evidence_id"), str) else None
    evidence_type_valid = evidence.get("evidence_type") == "filesystem.snapshot"
    if not evidence_type_valid:
        problem("SNAPSHOT_EVIDENCE_TYPE_INVALID", "evidence_type must be filesystem.snapshot")
    from .evidence import digest

    try:
        evidence_digest_valid = isinstance(evidence.get("evidence_digest"), str) and evidence.get(
            "evidence_digest"
        ) == digest(evidence)
    except EvidenceError:
        evidence_digest_valid = False
    if not evidence_digest_valid:
        problem("SNAPSHOT_EVIDENCE_INVALID", "evidence digest mismatch")
    payload = evidence.get("payload")
    payload_schema_valid = isinstance(payload, dict) and set(payload) == {"snapshot", "entries"}
    if not payload_schema_valid:
        problem("SNAPSHOT_PAYLOAD_SCHEMA_INVALID", "payload must contain snapshot and entries")
    snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
    entries = payload.get("entries") if isinstance(payload, dict) else None
    snapshot_id = snapshot.get("snapshot_id") if isinstance(snapshot, dict) and isinstance(snapshot.get("snapshot_id"), str) else None
    snapshot_schema_valid = isinstance(snapshot, dict) and set(snapshot) == _V2_SNAPSHOT_FIELDS and isinstance(entries, list)
    if not snapshot_schema_valid:
        problem("SNAPSHOT_PAYLOAD_SCHEMA_INVALID", "invalid v2 snapshot schema")
    snapshot_data: dict[str, Any] = snapshot if isinstance(snapshot, dict) else {}
    typed_snapshot_valid = snapshot_schema_valid and all(
        isinstance(snapshot_data.get(name), str)
        for name in ("snapshot_id", "run_id", "created_at", "started_at", "completed_at", "config_digest", "entries_digest", "snapshot_digest")
    ) and snapshot_data.get("snapshot_schema_version") == 2 and snapshot_data.get("status") in SnapshotStatus._value2member_map_ and snapshot_data.get("consistency") in SnapshotConsistency._value2member_map_ and isinstance(snapshot_data.get("scope_ids"), list) and all(isinstance(value, str) for value in snapshot_data.get("scope_ids", [])) and isinstance(snapshot_data.get("budget"), dict) and _valid_hash_policy(snapshot_data.get("hash_policy")) and all(_is_int(snapshot_data.get(name)) and snapshot_data[name] >= 0 for name in ("entry_count", "observed_count", "error_count", "excluded_count", "total_regular_file_bytes", "max_depth_observed", "allocated_regular_file_bytes_known_sum", "allocated_regular_file_unknown_count"))
    if not typed_snapshot_valid:
        problem("SNAPSHOT_PAYLOAD_SCHEMA_INVALID", "invalid v2 snapshot field types")
    run_id_consistent = typed_snapshot_valid and snapshot_data.get("run_id") == evidence.get("run_id")
    if not run_id_consistent:
        problem("SNAPSHOT_RUN_ID_MISMATCH", "snapshot run_id differs from evidence run_id")
    values = entries if isinstance(entries, list) else []
    payload_observations_valid = all(
        isinstance(item, dict) and _valid_payload_observation(item) for item in values
    )
    allocated_sizes_valid = all(
        isinstance(item, dict) and _is_optional_nonnegative_int(item.get("allocated_size_bytes"))
        for item in values
    )
    entries_schema_valid = isinstance(entries, list) and all(
        _v2_entry_schema_valid(item) for item in entries
    )
    if not entries_schema_valid:
        problem("SNAPSHOT_ENTRY_SCHEMA_INVALID", "invalid v2 entry or payload observation")
    if not payload_observations_valid:
        problem("PAYLOAD_OBSERVATION_INVALID", "invalid payload observation")
    if not allocated_sizes_valid:
        problem("ALLOCATED_SIZE_INVALID", "allocated size must be non-negative or null")
    order_valid = values == canonical_snapshot_v2_entries(values) if entries_schema_valid else False
    if not order_valid:
        problem("SNAPSHOT_ENTRY_ORDER_INVALID", "entries are not sorted")
    scoped_keys = [(item.get("scope_id"), item.get("relative_path")) for item in values if isinstance(item, dict)]
    entry_keys_unique = len(scoped_keys) == len(set(scoped_keys))
    if not entry_keys_unique:
        problem("SNAPSHOT_ENTRY_DUPLICATE", "duplicate scoped location")
    entry_ids_valid = all(
        isinstance(item, dict)
        and item.get("entry_id") == entry_id(str(snapshot_id), str(item.get("scope_id")), str(item.get("relative_path")))
        for item in values
    )
    if not entry_ids_valid:
        problem("SNAPSHOT_ENTRY_ID_INVALID", "invalid entry id")
    scope_membership_valid = all(
        isinstance(item, dict)
        and item.get("snapshot_id") == snapshot_id
        and item.get("scope_id") in snapshot_data.get("scope_ids", [])
        for item in values
    )
    if not scope_membership_valid:
        problem("SNAPSHOT_SCOPE_MISMATCH", "invalid entry scope")
    paths_valid = all(
        isinstance(item, dict)
        and isinstance(item.get("relative_path"), str)
        and not item["relative_path"].startswith("/")
        and item["relative_path"] != ".."
        and not item["relative_path"].startswith("../")
        and "/../" not in item["relative_path"]
        for item in values
    )
    if not paths_valid:
        problem("SNAPSHOT_PATH_INVALID", "invalid entry path")
    entry_collection_valid = typed_snapshot_valid and snapshot_data.get("entries") == values
    if not entry_collection_valid:
        problem("SNAPSHOT_ENTRY_COLLECTION_MISMATCH", "snapshot entries differ from payload entries")
    summary = snapshot_statistics(values) if entries_schema_valid else {}
    allocated_known_sum = (
        sum(
            item["allocated_size_bytes"] or 0
            for item in values
            if item["object_type"] == FilesystemObjectType.REGULAR_FILE.value
        )
        if entries_schema_valid
        else 0
    )
    allocated_unknown_count = (
        sum(
            item["allocated_size_bytes"] is None
            for item in values
            if item["object_type"] == FilesystemObjectType.REGULAR_FILE.value
        )
        if entries_schema_valid
        else 0
    )
    statuses = (
        [item["payload_observation"]["status"] for item in values]
        if entries_schema_valid
        else []
    )
    expected_summary = {
        "status_counts": [
            {"status": status, "count": statuses.count(status)} for status in sorted(set(statuses))
        ]
    }
    summary_valid = typed_snapshot_valid and all(snapshot_data.get(key) == value for key, value in summary.items()) and snapshot_data.get("allocated_regular_file_bytes_known_sum") == allocated_known_sum and snapshot_data.get("allocated_regular_file_unknown_count") == allocated_unknown_count and snapshot_data.get("payload_observation_summary") == expected_summary
    if not summary_valid:
        problem("SNAPSHOT_SUMMARY_INVALID", "snapshot summary or aggregate mismatch")
    try:
        entries_digest_valid = typed_snapshot_valid and snapshot_data.get("entries_digest") == snapshot_v2_entries_digest(values)
        snapshot_digest_valid = typed_snapshot_valid and snapshot_data.get("snapshot_digest") == snapshot_v2_metadata_digest(snapshot_data)
    except EvidenceError:
        entries_digest_valid = snapshot_digest_valid = False
    if not entries_digest_valid:
        problem("SNAPSHOT_ENTRIES_DIGEST_INVALID", "entries digest mismatch")
    if not snapshot_digest_valid:
        problem("SNAPSHOT_DIGEST_INVALID", "snapshot digest mismatch")
    return SnapshotEvidenceValidationResult(
        evidence_id,
        snapshot_id,
        not errors,
        envelope_valid,
        evidence_digest_valid,
        evidence_type_valid,
        payload_schema_valid,
        bool(run_id_consistent),
        bool(snapshot_schema_valid and typed_snapshot_valid),
        entries_schema_valid,
        order_valid,
        entry_keys_unique,
        entry_ids_valid,
        scope_membership_valid,
        paths_valid,
        summary_valid,
        entries_digest_valid,
        snapshot_digest_valid,
        tuple(sorted(errors, key=lambda value: (value["code"], value["message"]))),
    )


def validate_snapshot_evidence(evidence: dict[str, Any]) -> SnapshotEvidenceValidationResult:
    """Dispatch Snapshot Evidence exactly once by its required envelope version."""
    version = snapshot_evidence_schema_version(evidence)
    if version is None:
        return _version_result(
            evidence, "EVIDENCE_SCHEMA_VERSION_INVALID", "invalid or missing schema_version"
        )
    if version == 1:
        return _validate_snapshot_evidence_v1(evidence)
    if version == 2:
        return _validate_snapshot_evidence_v2(evidence)
    return _version_result(
        evidence, "EVIDENCE_SCHEMA_VERSION_UNSUPPORTED", "unsupported schema_version"
    )


def snapshot_v2_from_valid_evidence(
    evidence: dict[str, Any], evidence_relative_path: str
) -> FilesystemSnapshotV2:
    """Map already validated v2 Evidence into its immutable read representation."""
    payload = cast(dict[str, Any], evidence["payload"])
    data = cast(dict[str, Any], payload["snapshot"])
    budget_data = cast(dict[str, Any], data["budget"])
    budget = ScanBudget(
        budget_data["max_entries"],
        budget_data["max_total_stat_bytes"],
        budget_data["max_duration_seconds"],
        budget_data["max_depth"],
    )
    entries = tuple(
        FilesystemEntryV2(
            item["entry_id"],
            item["snapshot_id"],
            item["scope_id"],
            item["relative_path"],
            FilesystemObjectType(item["object_type"]),
            item["device_id"],
            item["inode"],
            item["mode"],
            item["uid"],
            item["gid"],
            item["size_bytes"],
            item["mtime_ns"],
            item["ctime_ns"],
            item["birthtime_ns"],
            item["link_count"],
            item["symlink_target_raw"],
            item["readable"],
            item["writable"],
            item["executable"],
            FilesystemObservationStatus(item["observation_status"]),
            item["error_code"],
            item["error_message"],
            item["excluded"],
            item["allocated_size_bytes"],
            PayloadObservation(
                PayloadObservationStatus(item["payload_observation"]["status"]),
                item["payload_observation"]["algorithm"],
                item["payload_observation"]["algorithm_version"],
                item["payload_observation"]["digest"],
                item["payload_observation"]["bytes_hashed"],
                (
                    PayloadObservationProvenance(item["payload_observation"]["provenance"])
                    if item["payload_observation"]["provenance"] is not None
                    else None
                ),
                item["payload_observation"]["reused_from_snapshot_id"],
                item["payload_observation"]["failure_code"],
                item["payload_observation"]["os_error_code"],
            ),
        )
        for item in cast(list[dict[str, Any]], payload["entries"])
    )
    return FilesystemSnapshotV2(
        data["snapshot_schema_version"],
        data["snapshot_id"],
        data["run_id"],
        data["created_at"],
        data["started_at"],
        data["completed_at"],
        SnapshotStatus(data["status"]),
        SnapshotConsistency(data["consistency"]),
        data["config_digest"],
        tuple(data["scope_ids"]),
        budget,
        data["entry_count"],
        data["observed_count"],
        data["error_count"],
        data["excluded_count"],
        data["total_regular_file_bytes"],
        data["max_depth_observed"],
        PayloadHashPolicy(
            data["hash_policy"]["algorithm"],
            data["hash_policy"]["algorithm_version"],
            data["hash_policy"]["max_hash_file_bytes"],
            data["hash_policy"]["max_total_hash_bytes"],
            data["hash_policy"]["max_hash_duration_seconds"],
            data["hash_policy"]["hash_chunk_size"],
            data["hash_policy"]["allow_non_local_content"],
            data["hash_policy"]["allow_verified_reuse"],
        ),
        data["allocated_regular_file_bytes_known_sum"],
        data["allocated_regular_file_unknown_count"],
        tuple(
            PayloadObservationCount(
                PayloadObservationStatus(item["status"]), item["count"]
            )
            for item in data["payload_observation_summary"]["status_counts"]
        ),
        data["entries_digest"],
        data["snapshot_digest"],
        evidence["evidence_id"],
        evidence_relative_path,
        entries,
    )


def _find_snapshot_evidence(
    config: StewardConfig, snapshot_id: str
) -> tuple[dict[str, Any], str] | None:
    """Find a persisted Snapshot fact by identity without consulting SQLite."""
    runs_root = config.paths.evidence_dir / "runs"
    if not runs_root.is_dir() or runs_root.is_symlink():
        raise EvidenceError("verified reuse source repository is unavailable")
    matches: list[tuple[dict[str, Any], str]] = []
    for directory in sorted(runs_root.iterdir(), key=lambda item: item.name):
        if directory.is_symlink() or not directory.is_dir():
            continue
        files, _errors = load_run_files(config.paths.evidence_dir, directory.name)
        for path, evidence in files:
            payload = evidence.get("payload") if isinstance(evidence, dict) else None
            snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
            if (
                evidence.get("evidence_type") == "filesystem.snapshot"
                and isinstance(snapshot, dict)
                and snapshot.get("snapshot_id") == snapshot_id
            ):
                matches.append((evidence, str(path.relative_to(config.paths.evidence_dir))))
    if len(matches) > 1:
        raise EvidenceError("verified reuse source identity is ambiguous")
    return matches[0] if matches else None


def validate_snapshot_reuse_references(
    config: StewardConfig,
    current: FilesystemSnapshotV2,
    *,
    connection: sqlite3.Connection | None = None,
) -> tuple[dict[str, str], ...]:
    """Repository-aware validation of already intrinsic-valid REUSED facts."""
    errors: list[dict[str, str]] = []
    for entry in current.entries:
        observation = entry.payload_observation
        if observation.provenance != PayloadObservationProvenance.REUSED_FROM_VERIFIED_SNAPSHOT:
            continue
        source_id = observation.reused_from_snapshot_id
        source_fact = _find_snapshot_evidence(config, source_id or "") if source_id else None
        if source_fact is None:
            errors.append({"code": "PAYLOAD_REUSE_SOURCE_MISSING", "message": "reuse source is missing"})
            continue
        evidence, relative = source_fact
        if snapshot_evidence_schema_version(evidence) != 2:
            errors.append(
                {"code": "PAYLOAD_REUSE_SOURCE_SCHEMA_UNSUPPORTED", "message": "reuse source schema is unsupported"}
            )
            continue
        if not validate_snapshot_evidence(evidence).valid:
            errors.append({"code": "PAYLOAD_REUSE_SOURCE_INVALID", "message": "reuse source is invalid"})
            continue
        source = snapshot_v2_from_valid_evidence(evidence, relative)
        if source.completed_at > current.started_at:
            errors.append({"code": "PAYLOAD_REUSE_SOURCE_NOT_EARLIER", "message": "reuse source is not earlier"})
            continue
        verification = (
            _verify_snapshot(config, connection, source.snapshot_id)
            if connection is not None
            else verify_snapshot(config, source.snapshot_id)
        )
        if verification.status != "VALID":
            errors.append({"code": "PAYLOAD_REUSE_SOURCE_INVALID", "message": "reuse source is not VALID"})
            continue
        source_entry = next(
            (
                value
                for value in source.entries
                if (value.scope_id, value.relative_path) == (entry.scope_id, entry.relative_path)
            ),
            None,
        )
        if source_entry is None:
            errors.append({"code": "PAYLOAD_REUSE_SOURCE_ENTRY_MISSING", "message": "reuse source Entry is missing"})
            continue
        if not _direct_reuse_source_eligible(entry, source_entry, current.hash_policy):
            errors.append(
                {"code": "PAYLOAD_REUSE_SOURCE_METADATA_MISMATCH", "message": "reuse source is not eligible"}
            )
            continue
        source_observation = source_entry.payload_observation
        if (
            observation.status != source_observation.status
            or observation.algorithm != source_observation.algorithm
            or observation.algorithm_version != source_observation.algorithm_version
            or observation.bytes_hashed != source_observation.bytes_hashed
        ):
            errors.append(
                {"code": "PAYLOAD_REUSE_SOURCE_PAYLOAD_INVALID", "message": "reuse payload fields differ"}
            )
        elif observation.digest != source_observation.digest:
            errors.append(
                {"code": "PAYLOAD_REUSE_SOURCE_DIGEST_MISMATCH", "message": "reuse digest differs"}
            )
    return tuple(sorted(errors, key=lambda item: (item["code"], item["message"])))


def snapshot_v2_stat_view(snapshot: FilesystemSnapshotV2) -> FilesystemSnapshot:
    """Expose the frozen v1 stat view without treating v2 payload fields as Diff facts."""
    entries = tuple(
        FilesystemEntry(
            entry.entry_id,
            entry.snapshot_id,
            entry.scope_id,
            entry.relative_path,
            entry.object_type,
            entry.device_id,
            entry.inode,
            entry.mode,
            entry.uid,
            entry.gid,
            entry.size_bytes,
            entry.mtime_ns,
            entry.ctime_ns,
            entry.birthtime_ns,
            entry.link_count,
            entry.symlink_target_raw,
            entry.readable,
            entry.writable,
            entry.executable,
            entry.observation_status,
            entry.error_code,
            entry.error_message,
            entry.excluded,
        )
        for entry in snapshot.entries
    )
    return FilesystemSnapshot(
        snapshot.snapshot_id,
        snapshot.run_id,
        snapshot.created_at,
        snapshot.started_at,
        snapshot.completed_at,
        snapshot.status,
        snapshot.consistency,
        snapshot.config_digest,
        snapshot.scope_ids,
        snapshot.budget,
        snapshot.entry_count,
        snapshot.observed_count,
        snapshot.error_count,
        snapshot.excluded_count,
        snapshot.total_regular_file_bytes,
        snapshot.max_depth_observed,
        snapshot.entries_digest,
        snapshot.snapshot_digest,
        snapshot.evidence_id,
        snapshot.evidence_relative_path,
        entries,
    )


def snapshot_from_valid_evidence_versioned(
    evidence: dict[str, Any], evidence_relative_path: str
) -> FilesystemSnapshot | FilesystemSnapshotV2:
    """Map a valid v1/v2 document without querying SQLite or the filesystem."""
    result = validate_snapshot_evidence(evidence)
    if not result.valid:
        raise EvidenceError("Snapshot Evidence failed intrinsic validation")
    if snapshot_evidence_schema_version(evidence) == 1:
        return snapshot_from_valid_evidence(evidence, evidence_relative_path)
    return snapshot_v2_from_valid_evidence(evidence, evidence_relative_path)


def verify_snapshot(config: StewardConfig, snapshot_id: str) -> SnapshotVerificationResult:
    """Read-only composition of Evidence fact, derived index and Run lifecycle."""
    with open_readonly_initialized(config) as conn:
        return _verify_snapshot(config, conn, snapshot_id)


def _verify_snapshot(
    config: StewardConfig, conn: sqlite3.Connection, snapshot_id: str
) -> SnapshotVerificationResult:
    snapshot = _get_snapshot(conn, snapshot_id)
    errors: list[dict[str, str]] = []
    evidence_path = config.paths.evidence_dir / str(snapshot.evidence_relative_path)
    evidence_present = evidence_path.is_file()
    if not evidence_present:
        errors.append(
            {"code": "SNAPSHOT_EVIDENCE_MISSING", "message": "snapshot evidence is missing"}
        )
        return SnapshotVerificationResult(
            snapshot_id,
            "INVALID",
            snapshot.evidence_id,
            snapshot.run_id,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            tuple(errors),
        )
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        errors.append(
            {"code": "SNAPSHOT_EVIDENCE_INVALID", "message": "snapshot evidence cannot be read"}
        )
        return SnapshotVerificationResult(
            snapshot_id,
            "INVALID",
            snapshot.evidence_id,
            snapshot.run_id,
            True,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            tuple(errors),
        )
    intrinsic = validate_snapshot_evidence(evidence)
    evidence_valid = intrinsic.valid
    if not evidence_valid:
        errors.extend(intrinsic.errors)
    reuse_references_valid = True
    if evidence_valid and snapshot_evidence_schema_version(evidence) == 2:
        evidence_snapshot = snapshot_v2_from_valid_evidence(
            evidence, str(snapshot.evidence_relative_path)
        )
        reuse_errors = validate_snapshot_reuse_references(
            config, evidence_snapshot, connection=conn
        )
        if reuse_errors:
            errors.extend(reuse_errors)
            reuse_references_valid = False
    payload = evidence.get("payload", {}) if isinstance(evidence, dict) else {}
    fact = payload.get("snapshot", {}) if isinstance(payload, dict) else {}
    identity_ok = (
        evidence.get("evidence_id") == snapshot.evidence_id
        and evidence.get("run_id") == snapshot.run_id
        and fact.get("snapshot_id") == snapshot_id
    )
    if not identity_ok:
        errors.append(
            {"code": "SNAPSHOT_ID_MISMATCH", "message": "index and evidence identities differ"}
        )
    stable = (
        "run_id",
        "status",
        "consistency",
        "created_at",
        "started_at",
        "completed_at",
        "config_digest",
        "entry_count",
        "observed_count",
        "error_count",
        "excluded_count",
        "total_regular_file_bytes",
        "max_depth_observed",
        "entries_digest",
        "snapshot_digest",
    )
    snapshot_row_consistent = identity_ok and all(
        getattr(snapshot, name) == fact.get(name)
        or getattr(getattr(snapshot, name), "value", None) == fact.get(name)
        for name in stable
    )
    if not snapshot_row_consistent:
        errors.append(
            {
                "code": "SNAPSHOT_INDEX_INCOMPLETE",
                "message": "snapshot index metadata differs from evidence",
            }
        )
    evidence_entries = payload.get("entries", []) if isinstance(payload, dict) else []
    index_entries = [_entry_dict(entry) for entry in snapshot.entries]
    entry_count_consistent = len(evidence_entries) == len(index_entries)
    entry_order_consistent = [
        (item.get("scope_id"), item.get("relative_path"))
        for item in evidence_entries
        if isinstance(item, dict)
    ] == [(item["scope_id"], item["relative_path"]) for item in index_entries]
    entry_content_consistent = evidence_entries == index_entries
    if not entry_count_consistent:
        errors.append(
            {
                "code": "SNAPSHOT_ENTRY_INDEX_INCOMPLETE",
                "message": "snapshot entry count differs from evidence",
            }
        )
    elif not entry_content_consistent:
        errors.append(
            {
                "code": "SNAPSHOT_ENTRY_INDEX_INCONSISTENT",
                "message": "snapshot entry content differs from evidence",
            }
        )
    try:
        run = _get_run(conn, snapshot.run_id)
        run_present = True
        documents: tuple[dict[str, Any], ...] = ()
        indexed_chain_valid = True
        if supported_acquisition_metadata_valid(run.metadata, run.config_digest):
            chain: list[dict[str, Any]] = []
            try:
                for row in conn.execute(
                    "SELECT sequence,relative_path FROM evidence_records "
                    "WHERE run_id=? ORDER BY sequence",
                    (snapshot.run_id,),
                ):
                    relative = str(row["relative_path"])
                    relative_path = Path(relative)
                    if relative_path.is_absolute() or ".." in relative_path.parts:
                        raise ValueError("unsafe Evidence path")
                    document = json.loads(
                        (config.paths.evidence_dir / relative_path).read_text(encoding="utf-8")
                    )
                    if not isinstance(document, dict) or document.get("sequence") != row["sequence"]:
                        raise ValueError("indexed Evidence sequence mismatch")
                    chain.append(document)
            except (OSError, ValueError, json.JSONDecodeError):
                indexed_chain_valid = False
            documents = tuple(chain)
        run_consistent = indexed_chain_valid and snapshot_run_compatible(
            snapshot.status,
            run.run_kind,
            run.status,
            documents,
        )
    except Exception:
        run_present = False
        run_consistent = False
    if not run_present:
        errors.append({"code": "SNAPSHOT_RUN_MISSING", "message": "persistent Run is missing"})
    elif not run_consistent:
        errors.append(
            {
                "code": "SNAPSHOT_RUN_STATUS_INVALID",
                "message": "Run is incompatible with snapshot status",
            }
        )
    if not evidence_valid or not reuse_references_valid or not identity_ok or not run_consistent:
        status = "INVALID"
    elif (
        snapshot_row_consistent
        and entry_count_consistent
        and entry_content_consistent
        and entry_order_consistent
    ):
        status = "VALID"
    else:
        status = "INCOMPLETE"
    return SnapshotVerificationResult(
        snapshot_id,
        status,
        snapshot.evidence_id,
        snapshot.run_id,
        True,
        evidence_valid,
        True,
        snapshot_row_consistent and entry_count_consistent and entry_content_consistent,
        run_present,
        run_consistent,
        snapshot_row_consistent,
        entry_count_consistent,
        entry_content_consistent,
        entry_order_consistent,
        tuple(errors),
    )


def _verified_snapshot_detail(
    config: StewardConfig, snapshot_id: str
) -> tuple[SnapshotVerificationResult, FilesystemSnapshot | FilesystemSnapshotV2]:
    """Verification and detail from one guarded database identity."""
    with open_readonly_initialized(config) as conn:
        verification = _verify_snapshot(config, conn, snapshot_id)
        snapshot = _get_snapshot(conn, snapshot_id)
        return verification, snapshot


def _verified_snapshot_entries(
    config: StewardConfig,
    snapshot_id: str,
    scope_id: str | None = None,
    object_type: FilesystemObjectType | None = None,
    observation_status: FilesystemObservationStatus | None = None,
    path_prefix: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[
    SnapshotVerificationResult,
    FilesystemSnapshot | FilesystemSnapshotV2,
    SnapshotEntryPage,
]:
    """Verification, scope authority and page from one guarded database identity."""
    _validate_page(limit, offset, path_prefix)
    with open_readonly_initialized(config) as conn:
        verification = _verify_snapshot(config, conn, snapshot_id)
        snapshot = _get_snapshot(conn, snapshot_id)
        if scope_id is not None and scope_id not in snapshot.scope_ids:
            raise SnapshotScopeError(
                f"unknown historical scope_id for Snapshot {snapshot_id}: {scope_id}"
            )
        page = _list_snapshot_entries(
            conn,
            snapshot_id,
            scope_id,
            object_type,
            observation_status,
            path_prefix,
            limit,
            offset,
        )
        return verification, snapshot, page


def _snapshot_inventory_with_verification(
    config: StewardConfig, limit: int | None = 50
) -> tuple[tuple[FilesystemSnapshotSummary, SnapshotVerificationResult], ...]:
    """Inventory classifications from one guarded database identity."""
    with open_readonly_initialized(config) as conn:
        summaries = _list_snapshots(conn, limit)
        return tuple((item, _verify_snapshot(config, conn, item.snapshot_id)) for item in summaries)


def inspect_snapshot_inventory(config: StewardConfig) -> SnapshotInventory:
    with open_readonly_initialized(config) as conn:
        return _inspect_snapshot_inventory(config, conn)


def _inspect_snapshot_inventory(
    config: StewardConfig, conn: sqlite3.Connection
) -> SnapshotInventory:
    """Read both ledger and v2 index independently; record relationships without classifying them."""
    facts: list[
        tuple[str | None, str | None, str | None, str | None, tuple[str, ...], bool]
    ] = []
    runs_dir = config.paths.evidence_dir / "runs"
    try:
        directories = (
            sorted(
                (path for path in runs_dir.iterdir() if path.is_dir() and not path.is_symlink()),
                key=lambda path: path.name,
            )
            if runs_dir.is_dir() and not runs_dir.is_symlink()
            else []
        )
    except OSError as error:
        raise EvidenceError(f"unable to read evidence ledger: {error}") from error
    try:
        for directory in directories:
            files, failures = load_run_files(config.paths.evidence_dir, directory.name)
            for path, document in files:
                relative_path = str(path.relative_to(config.paths.evidence_dir))
                if not isinstance(document, dict):
                    if path.name.endswith("_filesystem.snapshot.json"):
                        facts.append(
                            (
                                None,
                                None,
                                directory.name,
                                relative_path,
                                ("SNAPSHOT_EVIDENCE_INVALID",),
                                False,
                            )
                        )
                    continue
                if document.get("evidence_type") != "filesystem.snapshot":
                    continue
                result = validate_snapshot_evidence(document)
                fact_codes = tuple(error["code"] for error in result.errors)
                facts.append(
                    (
                        result.snapshot_id,
                        result.evidence_id,
                        document.get("run_id") if isinstance(document.get("run_id"), str) else directory.name,
                        relative_path,
                        fact_codes,
                        result.valid,
                    )
                )
            for failure in failures:
                name = failure.partition(": ")[2]
                if name.endswith("_filesystem.snapshot.json"):
                    facts.append(
                        (
                            None,
                            None,
                            directory.name,
                            f"runs/{directory.name}/{name}",
                            ("SNAPSHOT_EVIDENCE_INVALID",),
                            False,
                        )
                    )
    except OSError as error:
        raise EvidenceError(f"unable to read evidence ledger: {error}") from error
    try:
        snapshots = list(conn.execute("SELECT * FROM snapshots ORDER BY snapshot_id"))
        evidence_records = list(
            conn.execute(
                "SELECT evidence_id, evidence_type, relative_path FROM evidence_records ORDER BY evidence_id"
            )
        )
        entries = list(
            conn.execute(
                "SELECT snapshot_id, scope_id, relative_path, entry_id "
                "FROM snapshot_entries ORDER BY snapshot_id, scope_id, relative_path"
            )
        )
        run_ids = {str(row[0]) for row in conn.execute("SELECT run_id FROM runs")}
    except sqlite3.Error as error:
        raise StorageCorruptionError(f"unable to inspect snapshot index: {error}") from error


    entry_counts: dict[str, int] = {}
    entry_orphans: set[str] = set()
    entry_cross_references: set[str] = set()
    indexed_ids = {str(row["snapshot_id"]) for row in snapshots}
    for entry in entries:
        indexed_snapshot_id = str(entry["snapshot_id"])
        entry_counts[indexed_snapshot_id] = entry_counts.get(indexed_snapshot_id, 0) + 1
        if indexed_snapshot_id not in indexed_ids:
            entry_orphans.add(indexed_snapshot_id)
        if entry["entry_id"] != entry_id(
            indexed_snapshot_id, str(entry["scope_id"]), str(entry["relative_path"])
        ):
            entry_cross_references.add(indexed_snapshot_id)

    facts_by_evidence: dict[str, list[int]] = {}
    facts_by_snapshot: dict[str, list[int]] = {}
    facts_by_run: dict[str, list[int]] = {}
    facts_by_path: dict[str, list[int]] = {}
    for position, fact in enumerate(facts):
        fact_snapshot_id, fact_evidence_id, fact_run_id, fact_relative_path, _fact_codes, valid = fact
        if fact_evidence_id is not None:
            facts_by_evidence.setdefault(fact_evidence_id, []).append(position)
        if valid and fact_snapshot_id is not None:
            facts_by_snapshot.setdefault(fact_snapshot_id, []).append(position)
        if valid and fact_run_id is not None:
            facts_by_run.setdefault(fact_run_id, []).append(position)
        if fact_relative_path is not None:
            facts_by_path.setdefault(fact_relative_path, []).append(position)

    evidence_record_by_id = {str(row["evidence_id"]): row for row in evidence_records}
    indexes_by_evidence: dict[str, list[int]] = {}
    indexes_by_snapshot: dict[str, list[int]] = {}
    for position, row in enumerate(snapshots):
        indexes_by_evidence.setdefault(str(row["evidence_id"]), []).append(position)
        indexes_by_snapshot.setdefault(str(row["snapshot_id"]), []).append(position)

    fact_issue_codes: list[set[str]] = [set(fact[4]) for fact in facts]
    index_issue_codes: list[set[str]] = [set() for _row in snapshots]
    for positions in facts_by_snapshot.values():
        evidence_ids = {facts[position][1] for position in positions}
        if len(evidence_ids) > 1:
            for position in positions:
                fact_issue_codes[position].add("SNAPSHOT_ID_DUPLICATE")
            for position in positions:
                for index_position in indexes_by_snapshot.get(str(facts[position][0]), []):
                    index_issue_codes[index_position].add("SNAPSHOT_ID_DUPLICATE")
    for positions in facts_by_run.values():
        snapshot_ids = {facts[position][0] for position in positions}
        if len(snapshot_ids) > 1:
            for position in positions:
                fact_issue_codes[position].add("SNAPSHOT_RUN_DUPLICATE")
            for position in positions:
                for index_position in indexes_by_evidence.get(str(facts[position][1]), []):
                    index_issue_codes[index_position].add("SNAPSHOT_RUN_DUPLICATE")
    for positions in indexes_by_evidence.values():
        if len(positions) > 1:
            for position in positions:
                index_issue_codes[position].add("SNAPSHOT_EVIDENCE_INDEX_DUPLICATE")

    for fact_position, fact in enumerate(facts):
        snapshot_id, evidence_id, run_id, _relative_path, _fact_codes, valid = fact
        if run_id is not None and run_id not in run_ids:
            fact_issue_codes[fact_position].add("SNAPSHOT_RUN_MISSING")
        if not valid:
            fact_issue_codes[fact_position].add("SNAPSHOT_EVIDENCE_INVALID")
            continue
        matching_indexes = indexes_by_evidence.get(evidence_id or "", [])
        exact_indexes = [
            position
            for position in matching_indexes
            if snapshots[position]["snapshot_id"] == snapshot_id
        ]
        if not exact_indexes:
            fact_issue_codes[fact_position].add("SNAPSHOT_EVIDENCE_ORPHANED")
        for index_position in matching_indexes:
            if snapshots[index_position]["snapshot_id"] != snapshot_id:
                fact_issue_codes[fact_position].add("SNAPSHOT_INDEX_SNAPSHOT_ID_MISMATCH")
                index_issue_codes[index_position].add("SNAPSHOT_INDEX_SNAPSHOT_ID_MISMATCH")
        if snapshot_id is not None and not matching_indexes and indexes_by_snapshot.get(snapshot_id):
            fact_issue_codes[fact_position].add("SNAPSHOT_INDEX_EVIDENCE_ID_MISMATCH")
            for index_position in indexes_by_snapshot[snapshot_id]:
                index_issue_codes[index_position].add("SNAPSHOT_INDEX_EVIDENCE_ID_MISMATCH")

    for index_position, row in enumerate(snapshots):
        snapshot_id = str(row["snapshot_id"])
        evidence_id = str(row["evidence_id"])
        relative_path = str(row["evidence_relative_path"])
        record = evidence_record_by_id.get(evidence_id)
        matching_facts = facts_by_evidence.get(evidence_id, [])
        path_facts = facts_by_path.get(relative_path, [])
        if record is None:
            index_issue_codes[index_position].add("SNAPSHOT_INDEX_EVIDENCE_MISSING")
        elif record["evidence_type"] != "filesystem.snapshot":
            index_issue_codes[index_position].add("SNAPSHOT_INDEX_EVIDENCE_TYPE_MISMATCH")
        if not matching_facts or not any(
            facts[position][3] == relative_path for position in matching_facts
        ):
            index_issue_codes[index_position].add("SNAPSHOT_INDEX_EVIDENCE_MISSING")
        if any(facts[position][1] != evidence_id for position in path_facts):
            index_issue_codes[index_position].add("SNAPSHOT_INDEX_EVIDENCE_ID_MISMATCH")
        if not any(
            facts[position][5] and facts[position][0] == snapshot_id
            for position in matching_facts
        ) and matching_facts:
            index_issue_codes[index_position].add("SNAPSHOT_INDEX_SNAPSHOT_ID_MISMATCH")
        if str(row["run_id"]) not in run_ids:
            index_issue_codes[index_position].add("SNAPSHOT_RUN_MISSING")
        if snapshot_id in entry_cross_references:
            index_issue_codes[index_position].add("SNAPSHOT_ENTRY_CROSS_REFERENCE")

    items: list[SnapshotInventoryItem] = []
    exact_fact_index_pairs: set[tuple[int, int]] = set()
    for fact_position, fact in enumerate(facts):
        fact_snapshot_id, fact_evidence_id, fact_run_id, fact_relative_path, _fact_codes, _valid = fact
        exact_indexes = [
            position
            for position in indexes_by_evidence.get(fact_evidence_id or "", [])
            if snapshots[position]["snapshot_id"] == fact_snapshot_id
        ]
        for index_position in exact_indexes:
            exact_fact_index_pairs.add((fact_position, index_position))
        codes = set(fact_issue_codes[fact_position])
        for index_position in exact_indexes:
            codes.update(index_issue_codes[index_position])
        items.append(
            SnapshotInventoryItem(
                fact_snapshot_id,
                fact_evidence_id,
                fact_run_id,
                True,
                bool(exact_indexes),
                fact_run_id in run_ids if fact_run_id is not None else False,
                entry_counts.get(fact_snapshot_id, 0) if fact_snapshot_id is not None else 0,
                fact_relative_path,
                tuple(sorted(codes)),
            )
        )
    for index_position, row in enumerate(snapshots):
        if any(pair[1] == index_position for pair in exact_fact_index_pairs):
            continue
        snapshot_id = str(row["snapshot_id"])
        evidence_id = str(row["evidence_id"])
        matching_facts = facts_by_evidence.get(evidence_id, [])
        items.append(
            SnapshotInventoryItem(
                snapshot_id,
                evidence_id,
                str(row["run_id"]),
                bool(matching_facts),
                True,
                str(row["run_id"]) in run_ids,
                entry_counts.get(snapshot_id, 0),
                str(row["evidence_relative_path"]),
                tuple(sorted(index_issue_codes[index_position])),
            )
        )
    for snapshot_id in sorted(entry_orphans):
        codes = {"SNAPSHOT_ENTRY_ORPHANED"}
        if snapshot_id in entry_cross_references:
            codes.add("SNAPSHOT_ENTRY_CROSS_REFERENCE")
        items.append(
            SnapshotInventoryItem(
                snapshot_id,
                None,
                None,
                False,
                False,
                False,
                entry_counts[snapshot_id],
                None,
                tuple(sorted(codes)),
            )
        )
    issues: list[dict[str, str]] = []
    for item in items:
        for code in item.issue_codes:
            issues.append(
                {
                    "code": code,
                    "snapshot_id": item.snapshot_id or "",
                    "evidence_id": item.evidence_id or "",
                }
            )
    ordered = tuple(
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
    return SnapshotInventory(
        len(facts),
        len(snapshots),
        len(entry_counts),
        len(run_ids),
        ordered,
        tuple(
            sorted(
                issues,
                key=lambda item: (
                    item["snapshot_id"] == "",
                    item["snapshot_id"],
                    item["evidence_id"],
                    item["code"],
                ),
            )
        ),
        sum(entry_counts.values()),
    )


_INTEGRITY_ISSUE_SEVERITIES: dict[str, SnapshotStorageIntegrityStatus] = {
    "SNAPSHOT_EVIDENCE_ORPHANED": SnapshotStorageIntegrityStatus.DEGRADED,
    "SNAPSHOT_INDEX_MISSING": SnapshotStorageIntegrityStatus.DEGRADED,
    "SNAPSHOT_INDEX_INCOMPLETE": SnapshotStorageIntegrityStatus.DEGRADED,
    "SNAPSHOT_ENTRY_INDEX_INCOMPLETE": SnapshotStorageIntegrityStatus.DEGRADED,
    "SNAPSHOT_ENTRY_INDEX_INCONSISTENT": SnapshotStorageIntegrityStatus.DEGRADED,
    "SNAPSHOT_EVIDENCE_MISSING": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_EVIDENCE_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_EVIDENCE_SCHEMA_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_EVIDENCE_TYPE_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_PAYLOAD_SCHEMA_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_RUN_ID_MISMATCH": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_PAYLOAD_RUN_ID_MISMATCH": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_ENTRY_SCHEMA_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_ENTRY_ORDER_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_ENTRY_DUPLICATE": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_ENTRY_ID_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_SCOPE_MISMATCH": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_PATH_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_SUMMARY_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_ENTRIES_DIGEST_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_DIGEST_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_INDEX_EVIDENCE_MISSING": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_INDEX_EVIDENCE_TYPE_MISMATCH": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_INDEX_EVIDENCE_ID_MISMATCH": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_INDEX_SNAPSHOT_ID_MISMATCH": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_RUN_MISSING": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_RUN_KIND_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_RUN_STATUS_INVALID": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_ID_DUPLICATE": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_RUN_DUPLICATE": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_EVIDENCE_INDEX_DUPLICATE": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_ENTRY_ORPHANED": SnapshotStorageIntegrityStatus.INVALID,
    "SNAPSHOT_ENTRY_CROSS_REFERENCE": SnapshotStorageIntegrityStatus.INVALID,
}

_INTEGRITY_STATUS_ORDER = {
    SnapshotStorageIntegrityStatus.INVALID: 0,
    SnapshotStorageIntegrityStatus.DEGRADED: 1,
    SnapshotStorageIntegrityStatus.HEALTHY: 2,
}


def _integrity_severity(code: str) -> SnapshotStorageIntegrityStatus:
    """Unknown integrity facts are unsafe to classify as healthy."""
    return _INTEGRITY_ISSUE_SEVERITIES.get(code, SnapshotStorageIntegrityStatus.INVALID)


def _item_integrity_codes(item: SnapshotInventoryItem) -> tuple[str, ...]:
    """Derive absence facts already represented by an inventory item, without reading storage."""
    codes = set(item.issue_codes)
    if not item.evidence_present:
        codes.add("SNAPSHOT_EVIDENCE_MISSING")
    if not item.run_present:
        codes.add("SNAPSHOT_RUN_MISSING")
    if not item.index_present and item.evidence_present and item.run_present:
        codes.add("SNAPSHOT_INDEX_MISSING")
    return tuple(sorted(codes))


def _item_integrity_status(codes: tuple[str, ...]) -> SnapshotStorageIntegrityStatus:
    if not codes:
        return SnapshotStorageIntegrityStatus.HEALTHY
    return min((_integrity_severity(code) for code in codes), key=_INTEGRITY_STATUS_ORDER.__getitem__)


def _integrity_issue_key(issue: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        issue["code"],
        issue.get("snapshot_id", ""),
        issue.get("evidence_id", ""),
        issue.get("persistent_run_id", ""),
        issue.get("evidence_relative_path", ""),
    )


def _integrity_relation_count(
    issues: tuple[dict[str, str], ...], codes: set[str], fields: tuple[str, ...]
) -> int:
    return len(
        {
            tuple(issue.get(field, "") for field in fields)
            for issue in issues
            if issue["code"] in codes
        }
    )


def classify_snapshot_inventory(
    inventory: SnapshotInventory,
) -> SnapshotStorageIntegrityReport:
    """Purely classify already-enumerated Snapshot inventory facts; never read or repair storage."""
    classified: list[SnapshotStorageIntegrityItem] = []
    issue_by_key: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    raw_codes_by_item: list[set[str]] = [set() for _item in inventory.items]
    for raw_issue in inventory.issues:
        code = raw_issue.get("code", "SNAPSHOT_INTEGRITY_CLASSIFICATION_UNKNOWN")
        snapshot_id = raw_issue.get("snapshot_id", "")
        evidence_id = raw_issue.get("evidence_id", "")
        if not snapshot_id and not evidence_id:
            continue
        for position, item in enumerate(inventory.items):
            if (not snapshot_id or item.snapshot_id == snapshot_id) and (
                not evidence_id or item.evidence_id == evidence_id
            ):
                raw_codes_by_item[position].add(code)
    for position, item in enumerate(inventory.items):
        codes = tuple(sorted(set(_item_integrity_codes(item)) | raw_codes_by_item[position]))
        status = _item_integrity_status(codes)
        classified_item = SnapshotStorageIntegrityItem(
            item.snapshot_id,
            item.evidence_id,
            item.persistent_run_id,
            status,
            item.evidence_present,
            item.index_present,
            item.run_present,
            item.indexed_entry_count,
            codes,
            item.evidence_relative_path,
        )
        classified.append(classified_item)
        for code in codes:
            issue = {
                "code": code,
                "severity": _integrity_severity(code).value,
                "snapshot_id": item.snapshot_id or "",
                "evidence_id": item.evidence_id or "",
                "persistent_run_id": item.persistent_run_id or "",
                "evidence_relative_path": item.evidence_relative_path or "",
            }
            issue_by_key[_integrity_issue_key(issue)] = issue
    for raw_issue in inventory.issues:
        code = raw_issue.get("code", "SNAPSHOT_INTEGRITY_CLASSIFICATION_UNKNOWN")
        snapshot_id = raw_issue.get("snapshot_id", "")
        evidence_id = raw_issue.get("evidence_id", "")
        matching = [
            item
            for item in inventory.items
            if (not snapshot_id or item.snapshot_id == snapshot_id)
            and (not evidence_id or item.evidence_id == evidence_id)
        ]
        if matching:
            for item in matching:
                issue = {
                    "code": code,
                    "severity": _integrity_severity(code).value,
                    "snapshot_id": snapshot_id,
                    "evidence_id": evidence_id,
                    "persistent_run_id": item.persistent_run_id or "",
                    "evidence_relative_path": item.evidence_relative_path or "",
                }
                issue_by_key[_integrity_issue_key(issue)] = issue
        else:
            issue = {
                "code": code,
                "severity": _integrity_severity(code).value,
                "snapshot_id": snapshot_id,
                "evidence_id": evidence_id,
                "persistent_run_id": "",
                "evidence_relative_path": "",
            }
            issue_by_key[_integrity_issue_key(issue)] = issue
    ordered_items = tuple(
        sorted(
            classified,
            key=lambda item: (
                _INTEGRITY_STATUS_ORDER[item.status],
                item.snapshot_id is None,
                item.snapshot_id or "",
                item.evidence_id or "",
                item.persistent_run_id or "",
            ),
        )
    )
    ordered_issues = tuple(
        sorted(
            issue_by_key.values(),
            key=lambda issue: (
                _INTEGRITY_STATUS_ORDER[_integrity_severity(issue["code"])],
                issue["code"],
                issue["snapshot_id"],
                issue["evidence_id"],
                issue["persistent_run_id"],
                issue["evidence_relative_path"],
            ),
        )
    )
    healthy = sum(item.status == SnapshotStorageIntegrityStatus.HEALTHY for item in ordered_items)
    degraded = sum(item.status == SnapshotStorageIntegrityStatus.DEGRADED for item in ordered_items)
    invalid = sum(item.status == SnapshotStorageIntegrityStatus.INVALID for item in ordered_items)
    issue_severities = {_integrity_severity(issue["code"]) for issue in ordered_issues}
    if invalid or SnapshotStorageIntegrityStatus.INVALID in issue_severities:
        status = SnapshotStorageIntegrityStatus.INVALID
    elif degraded or SnapshotStorageIntegrityStatus.DEGRADED in issue_severities:
        status = SnapshotStorageIntegrityStatus.DEGRADED
    else:
        status = SnapshotStorageIntegrityStatus.HEALTHY
    return SnapshotStorageIntegrityReport(
        status,
        inventory.snapshot_evidence_records,
        inventory.indexed_snapshots,
        inventory.indexed_entry_groups,
        inventory.indexed_entry_count,
        inventory.runs,
        healthy,
        degraded,
        invalid,
        _integrity_relation_count(
            ordered_issues, {"SNAPSHOT_EVIDENCE_ORPHANED"}, ("evidence_id", "evidence_relative_path")
        ),
        _integrity_relation_count(
            ordered_issues,
            {"SNAPSHOT_EVIDENCE_MISSING", "SNAPSHOT_INDEX_EVIDENCE_MISSING"},
            ("snapshot_id", "evidence_id", "evidence_relative_path"),
        ),
        _integrity_relation_count(ordered_issues, {"SNAPSHOT_ID_DUPLICATE"}, ("snapshot_id",)),
        _integrity_relation_count(
            ordered_issues, {"SNAPSHOT_RUN_DUPLICATE"}, ("persistent_run_id",)
        ),
        _integrity_relation_count(
            ordered_issues, {"SNAPSHOT_EVIDENCE_INDEX_DUPLICATE"}, ("evidence_id",)
        ),
        _integrity_relation_count(ordered_issues, {"SNAPSHOT_ENTRY_ORPHANED"}, ("snapshot_id",)),
        _integrity_relation_count(
            ordered_issues, {"SNAPSHOT_ENTRY_CROSS_REFERENCE"}, ("snapshot_id",)
        ),
        ordered_items,
        ordered_issues,
        (),
    )
