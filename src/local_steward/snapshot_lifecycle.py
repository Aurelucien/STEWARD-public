"""Shared Snapshot/Run lifecycle compatibility for legacy and supported acquisition."""

from __future__ import annotations

import math
import re
from hashlib import sha256
from typing import Any, Iterable

from .evidence import canonical_json, digest
from .models import RunStatus, SnapshotStatus

SUPPORTED_ACQUISITION_WORKFLOW = "supported_snapshot_acquisition"
SUPPORTED_ACQUISITION_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_METADATA_KEYS = {
    "workflow",
    "workflow_version",
    "scope_binding",
    "scan_budget",
    "payload_hash_policy",
}
_BINDING_KEYS = {
    "scope_id",
    "role",
    "device_id",
    "inode",
    "follow_directory_symlinks",
    "allow_cross_mount",
    "binding_digest",
}
_BUDGET_KEYS = {
    "max_entries",
    "max_total_stat_bytes",
    "max_duration_seconds",
    "max_depth",
}


def expected_run_status(snapshot_status: object) -> RunStatus | None:
    """Return the only Snapshot-compatible observation status."""
    value = (
        snapshot_status.value
        if isinstance(snapshot_status, SnapshotStatus)
        else snapshot_status
        if isinstance(snapshot_status, str)
        else None
    )
    if value == SnapshotStatus.COMPLETE.value:
        return RunStatus.SCANNED
    if value == SnapshotStatus.PARTIAL.value:
        return RunStatus.PARTIAL
    return None


def scope_binding_digest(config_digest: str, binding: dict[str, Any]) -> str:
    """Bind one path-free scope identity to the immutable Run configuration fact."""
    value = {
        "domain": "local_steward.snapshot_acquisition.scope_binding.v1",
        "config_digest": config_digest,
        "scope_id": binding["scope_id"],
        "role": binding["role"],
        "device_id": binding["device_id"],
        "inode": binding["inode"],
        "follow_directory_symlinks": binding["follow_directory_symlinks"],
        "allow_cross_mount": binding["allow_cross_mount"],
    }
    return sha256(canonical_json(value)).hexdigest()


def supported_acquisition_metadata_valid(
    metadata: object, config_digest: object
) -> bool:
    """Recognize only the exact path-free workflow-v1 authority marker."""
    if not isinstance(metadata, dict) or set(metadata) != _METADATA_KEYS:
        return False
    if metadata.get("workflow") != SUPPORTED_ACQUISITION_WORKFLOW:
        return False
    if type(metadata.get("workflow_version")) is not int or metadata.get(
        "workflow_version"
    ) != SUPPORTED_ACQUISITION_VERSION:
        return False
    if metadata.get("payload_hash_policy") is not None:
        return False
    if not isinstance(config_digest, str) or not _SHA256.fullmatch(config_digest):
        return False
    binding = metadata.get("scope_binding")
    if not isinstance(binding, dict) or set(binding) != _BINDING_KEYS:
        return False
    if not isinstance(binding.get("scope_id"), str) or not binding["scope_id"]:
        return False
    if binding.get("role") not in {"managed_root", "reference_root"}:
        return False
    if type(binding.get("device_id")) is not int or binding["device_id"] < 0:
        return False
    if type(binding.get("inode")) is not int or binding["inode"] < 0:
        return False
    if binding.get("follow_directory_symlinks") is not False:
        return False
    if binding.get("allow_cross_mount") is not False:
        return False
    if not isinstance(binding.get("binding_digest"), str) or not _SHA256.fullmatch(
        binding["binding_digest"]
    ):
        return False
    try:
        if binding["binding_digest"] != scope_binding_digest(config_digest, binding):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    budget = metadata.get("scan_budget")
    if not isinstance(budget, dict) or set(budget) != _BUDGET_KEYS:
        return False
    entries = budget.get("max_entries")
    duration = budget.get("max_duration_seconds")
    stat_bytes = budget.get("max_total_stat_bytes")
    depth = budget.get("max_depth")
    if type(entries) is not int or not 1 <= entries <= 1_000_000:
        return False
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        return False
    if not math.isfinite(float(duration)) or not 0 < float(duration) <= 600:
        return False
    if stat_bytes is not None and (type(stat_bytes) is not int or stat_bytes < 0):
        return False
    if depth is not None and (type(depth) is not int or depth < 0):
        return False
    return True


def run_created_metadata(documents: Iterable[dict[str, Any]]) -> tuple[object, object]:
    """Return the immutable metadata/config facts from the first ledger document."""
    items = tuple(documents)
    if not items:
        return None, None
    first = items[0]
    payload = first.get("payload")
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    return metadata, first.get("config_digest")


def is_supported_acquisition_run(documents: Iterable[dict[str, Any]]) -> bool:
    items = tuple(documents)
    metadata, config_digest = run_created_metadata(items)
    if not supported_acquisition_metadata_valid(metadata, config_digest):
        return False
    first = items[0]
    payload = first.get("payload")
    return (
        first.get("evidence_type") == "run.created"
        and isinstance(payload, dict)
        and payload.get("run_kind") == "filesystem.snapshot"
        and payload.get("initial_status") == RunStatus.CREATED.value
    )


def _transition_pairs(documents: Iterable[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for document in documents:
        if document.get("evidence_type") != "run.state_transition":
            continue
        payload = document.get("payload")
        if not isinstance(payload, dict):
            return ()
        before = payload.get("from_status")
        after = payload.get("to_status")
        if not isinstance(before, str) or not isinstance(after, str):
            return ()
        pairs.append((before, after))
    return tuple(pairs)


def _supported_ledger_integrity(documents: tuple[dict[str, Any], ...]) -> bool:
    if not documents:
        return False
    run_id = documents[0].get("run_id")
    config_digest = documents[0].get("config_digest")
    previous: str | None = None
    evidence_ids: set[str] = set()
    evidence_digests: set[str] = set()
    for sequence, document in enumerate(documents, 1):
        evidence_id = document.get("evidence_id")
        evidence_digest = document.get("evidence_digest")
        if (
            document.get("run_id") != run_id
            or document.get("config_digest") != config_digest
            or document.get("sequence") != sequence
            or document.get("previous_evidence_digest") != previous
            or not isinstance(evidence_id, str)
            or not isinstance(evidence_digest, str)
            or evidence_id in evidence_ids
            or evidence_digest in evidence_digests
            or digest(document) != evidence_digest
        ):
            return False
        evidence_ids.add(evidence_id)
        evidence_digests.add(evidence_digest)
        previous = evidence_digest
    return True


def snapshot_run_compatible(
    snapshot_status: object,
    run_kind: str | None,
    final_status: str | RunStatus | None,
    documents: Iterable[dict[str, Any]] = (),
) -> bool:
    """Accept legacy final states and only chain-proven workflow-v1 closure states."""
    expected = expected_run_status(snapshot_status)
    final = final_status.value if isinstance(final_status, RunStatus) else final_status
    if run_kind != "filesystem.snapshot" or expected is None:
        return False
    items = tuple(documents)
    if final == expected.value:
        if not is_supported_acquisition_run(items):
            return True
        return _supported_ledger_integrity(items) and _transition_pairs(items) == (
            (RunStatus.CREATED.value, RunStatus.SCANNING.value),
            (RunStatus.SCANNING.value, expected.value),
        )
    if final not in {RunStatus.VERIFYING.value, RunStatus.VERIFIED.value}:
        return False
    if not is_supported_acquisition_run(items):
        return False
    if not _supported_ledger_integrity(items):
        return False
    if sum(item.get("evidence_type") == "filesystem.snapshot" for item in items) != 1:
        return False
    required = (
        (RunStatus.CREATED.value, RunStatus.SCANNING.value),
        (RunStatus.SCANNING.value, expected.value),
        (expected.value, RunStatus.VERIFYING.value),
    )
    pairs = _transition_pairs(items)
    if final == RunStatus.VERIFYING.value:
        return pairs == required
    return pairs == (*required, (RunStatus.VERIFYING.value, RunStatus.VERIFIED.value))
