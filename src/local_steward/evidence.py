"""Immutable canonical-JSON evidence files and verification helpers."""

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import EvidenceConflictError, EvidenceError

_NAME = re.compile(r"^(\d{8})_([a-z][a-z0-9_.-]*)\.json$")


def canonical_json(value: Any) -> bytes:
    """Encode only strict JSON values in the one ledger representation."""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise EvidenceError("value is not canonical JSON serializable") from error


def digest(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("evidence_digest", None)
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def compute_config_digest(config: Any) -> str:
    """Hash parsed configuration semantics, never source TOML formatting."""
    value = {
        "schema_version": config.schema_version,
        "project_name": config.project_name,
        "paths": {
            name: str(getattr(config.paths, name))
            for name in ("data_dir", "cache_dir", "evidence_dir", "quarantine_dir")
        },
        "scopes": [
            {
                "scope_id": item.scope_id,
                "role": item.role.value,
                "raw_path": item.raw_path,
                "normalized_path": str(item.normalized_path),
                "enabled": item.enabled,
                "follow_directory_symlinks": item.follow_directory_symlinks,
                "allow_cross_mount": item.allow_cross_mount,
            }
            for item in config.scopes
        ],
    }
    return hashlib.sha256(canonical_json(value)).hexdigest()


def filename(sequence: int, evidence_type: str) -> str:
    safe = evidence_type.replace("/", ".")
    if not re.fullmatch(r"[a-z][a-z0-9_.-]*", safe):
        raise EvidenceError("invalid evidence type")
    return f"{sequence:08d}_{safe}.json"


def write_evidence(evidence_root: Path, record: dict[str, Any]) -> str:
    """Durably create a new immutable evidence file, never overwrite it."""
    run_dir = evidence_root / "runs" / record["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    final = run_dir / filename(record["sequence"], record["evidence_type"])
    if final.exists() or final.is_symlink():
        raise EvidenceConflictError(f"evidence target already exists: {final.name}")
    expected = digest(record)
    if record.get("evidence_digest") != expected:
        raise EvidenceError("evidence digest does not match content")
    temp = run_dir / f".{final.name}.{uuid.uuid4()}.tmp"
    try:
        with temp.open("xb") as handle:
            handle.write(canonical_json(record))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, final)
        directory_fd = os.open(run_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        temp.unlink(missing_ok=True)
        raise EvidenceError(f"unable to write evidence: {error}") from error
    if not final.is_file() or digest(json.loads(final.read_text(encoding="utf-8"))) != expected:
        raise EvidenceError("evidence post-write verification failed")
    return str(final.relative_to(evidence_root))


def load_run_files(
    evidence_root: Path, run_id: str
) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    run_dir = evidence_root / "runs" / run_id
    if not run_dir.is_dir() or run_dir.is_symlink():
        return [], [f"missing or unsafe run directory: {run_id}"]
    items: list[tuple[Path, dict[str, Any]]] = []
    errors: list[str] = []
    for path in sorted(run_dir.iterdir()):
        if path.is_symlink() or not path.is_file():
            errors.append(f"unsafe evidence object: {path.name}")
            continue
        if path.name.endswith(".tmp"):
            continue
        if not _NAME.fullmatch(path.name):
            errors.append(f"unknown evidence file: {path.name}")
            continue
        try:
            items.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            errors.append(f"invalid JSON: {path.name}")
    return items, errors
