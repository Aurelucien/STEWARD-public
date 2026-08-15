"""Single Run Coordinator for durable evidence and derived state."""

import json
import re
import sqlite3
import uuid
from typing import Any

from . import __version__
from .database import open_initialized
from .errors import InvalidRunTransitionError, RunKindError, RunNotFoundError, StorageBusyError
from .evidence import compute_config_digest, digest, utc_now, write_evidence
from .faults import FaultInjector, checkpoint as fault_checkpoint
from .models import EvidenceRecord, RunRecord, RunStatus, StewardConfig
from .state_machine import is_terminal, validate_transition

_KIND = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")


def _run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        row["run_id"],
        row["run_kind"],
        RunStatus(row["status"]),
        row["created_at"],
        row["updated_at"],
        row["config_digest"],
        json.loads(row["metadata_json"]),
        row["last_sequence"],
        row["last_evidence_digest"],
        bool(row["terminal"]),
    )


def _get_run(conn: sqlite3.Connection, run_id: str) -> RunRecord:
    row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise RunNotFoundError(f"run not found: {run_id}")
    return _run(row)


def _evidence(
    run: RunRecord, evidence_type: str, payload: dict[str, Any], *, schema_version: int = 1
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "schema_version": schema_version,
        "evidence_id": str(uuid.uuid4()),
        "evidence_type": evidence_type,
        "run_id": run.run_id,
        "sequence": run.last_sequence + 1,
        "created_at": utc_now(),
        "tool_version": __version__,
        "config_digest": run.config_digest,
        "policy_digest": None,
        "provider_versions": {},
        "previous_evidence_digest": run.last_evidence_digest,
        "payload": payload,
    }
    item["evidence_digest"] = digest(item)
    return item


def create_run(
    config: StewardConfig,
    run_kind: str,
    metadata: dict[str, object] | None = None,
    *,
    _run_id: str | None = None,
    _fault_injector: FaultInjector | None = None,
) -> RunRecord:
    if not _KIND.fullmatch(run_kind):
        raise RunKindError("run kind must match [a-z][a-z0-9._-]{0,63}")
    if metadata is not None and not isinstance(metadata, dict):
        raise RunKindError("metadata must be a JSON object")
    try:
        metadata_json = json.dumps(
            metadata or {}, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise RunKindError("metadata must be JSON serializable") from error
    now = utc_now()
    run_id = _run_id or str(uuid.uuid4())
    try:
        parsed_run_id = uuid.UUID(run_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise RunKindError("run ID must be a canonical UUIDv4") from error
    if parsed_run_id.version != 4 or str(parsed_run_id) != run_id:
        raise RunKindError("run ID must be a canonical UUIDv4")
    run = RunRecord(
        run_id,
        run_kind,
        RunStatus.CREATED,
        now,
        now,
        compute_config_digest(config),
        json.loads(metadata_json),
        0,
        None,
        False,
    )
    conn = open_initialized(config)
    try:
        conn.execute("BEGIN IMMEDIATE")
        item = _evidence(
            run,
            "run.created",
            {"run_kind": run_kind, "initial_status": "created", "metadata": run.metadata},
        )
        fault_checkpoint(_fault_injector, "run.create", "before_evidence_publish")
        relative = write_evidence(config.paths.evidence_dir, item)
        fault_checkpoint(_fault_injector, "run.create", "after_evidence_publish")
        now = item["created_at"]
        conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.run_kind,
                run.status.value,
                now,
                now,
                run.config_digest,
                metadata_json,
                1,
                item["evidence_digest"],
                0,
            ),
        )
        conn.execute(
            "INSERT INTO evidence_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item["evidence_id"],
                run.run_id,
                1,
                item["evidence_type"],
                item["created_at"],
                relative,
                None,
                item["evidence_digest"],
                1,
            ),
        )
        fault_checkpoint(_fault_injector, "run.create", "before_index_commit")
        conn.commit()
        fault_checkpoint(_fault_injector, "run.create", "after_index_commit")
        return get_run(config, run.run_id)
    except sqlite3.OperationalError as error:
        conn.rollback()
        raise StorageBusyError("storage write lock timed out") from error
    finally:
        conn.close()


def get_run(config: StewardConfig, run_id: str) -> RunRecord:
    conn = open_initialized(config)
    try:
        return _get_run(conn, run_id)
    finally:
        conn.close()


def list_runs(
    config: StewardConfig,
    status: RunStatus | None = None,
    run_kind: str | None = None,
    limit: int = 50,
) -> list[RunRecord]:
    if not 1 <= limit <= 1000:
        raise RunKindError("limit must be between 1 and 1000")
    sql = "SELECT * FROM runs WHERE 1=1"
    values: list[object] = []
    if status:
        sql += " AND status=?"
        values.append(status.value)
    if run_kind:
        sql += " AND run_kind=?"
        values.append(run_kind)
    sql += " ORDER BY created_at DESC, run_id ASC LIMIT ?"
    values.append(limit)
    conn = open_initialized(config)
    try:
        return [_run(row) for row in conn.execute(sql, values)]
    finally:
        conn.close()


def transition_run(
    config: StewardConfig,
    run_id: str,
    target_status: RunStatus,
    reason: str,
    *,
    _fault_injector: FaultInjector | None = None,
) -> RunRecord:
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        raise InvalidRunTransitionError("reason must contain 1-1000 non-whitespace characters")
    conn = open_initialized(config)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        current = _run(row)
        if current.status == RunStatus.CANCELLED and target_status == RunStatus.CANCELLED:
            conn.rollback()
            return current
        validate_transition(current.status, target_status)
        item = _evidence(
            current,
            "run.state_transition",
            {
                "from_status": current.status.value,
                "to_status": target_status.value,
                "reason": reason,
            },
        )
        operation = f"run.transition.{target_status.value}"
        fault_checkpoint(_fault_injector, operation, "before_evidence_publish")
        relative = write_evidence(config.paths.evidence_dir, item)
        fault_checkpoint(_fault_injector, operation, "after_evidence_publish")
        now = item["created_at"]
        conn.execute(
            "INSERT INTO evidence_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item["evidence_id"],
                run_id,
                item["sequence"],
                item["evidence_type"],
                now,
                relative,
                item["previous_evidence_digest"],
                item["evidence_digest"],
                1,
            ),
        )
        conn.execute(
            "UPDATE runs SET status=?, updated_at=?, last_sequence=?, last_evidence_digest=?, terminal=? WHERE run_id=?",
            (
                target_status.value,
                now,
                item["sequence"],
                item["evidence_digest"],
                int(is_terminal(target_status)),
                run_id,
            ),
        )
        fault_checkpoint(_fault_injector, operation, "before_index_commit")
        conn.commit()
        fault_checkpoint(_fault_injector, operation, "after_index_commit")
        return get_run(config, run_id)
    except sqlite3.OperationalError as error:
        conn.rollback()
        raise StorageBusyError("storage write lock timed out") from error
    finally:
        conn.close()


def evidence_records(config: StewardConfig, run_id: str) -> list[EvidenceRecord]:
    conn = open_initialized(config)
    try:
        return [
            EvidenceRecord(
                row["evidence_id"],
                row["run_id"],
                row["sequence"],
                row["evidence_type"],
                row["created_at"],
                row["relative_path"],
                row["previous_evidence_digest"],
                row["evidence_digest"],
            )
            for row in conn.execute(
                "SELECT * FROM evidence_records WHERE run_id=? ORDER BY sequence", (run_id,)
            )
        ]
    finally:
        conn.close()
