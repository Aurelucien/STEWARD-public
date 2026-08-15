"""Pure-memory v2 Snapshot Evidence protocol checks."""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from local_steward.evidence import canonical_json, digest
from local_steward.snapshot_diff import compute_snapshot_diff
from local_steward.snapshots import (
    canonical_snapshot_v2_entries,
    entry_id,
    snapshot_evidence_schema_version,
    snapshot_from_valid_evidence_versioned,
    snapshot_v2_entries_digest,
    snapshot_v2_from_valid_evidence,
    snapshot_v2_metadata_digest,
    snapshot_v2_stat_view,
    validate_snapshot_evidence,
)


_SNAPSHOT_ID = "00000000-0000-4000-8000-000000000001"
_RUN_ID = "00000000-0000-4000-8000-000000000002"
_EVIDENCE_ID = "00000000-0000-4000-8000-000000000003"
_REUSE_SOURCE = "00000000-0000-4000-8000-000000000004"


def _observation(
    *, status: str = "HASHED", provenance: str | None = "DIRECT_READ", size: int = 4
) -> dict[str, object]:
    if status == "EMPTY_FILE_HASHED":
        size = 0
    success = status in {"HASHED", "EMPTY_FILE_HASHED"}
    return {
        "status": status,
        "algorithm": "sha256" if success else None,
        "algorithm_version": 1 if success else None,
        "digest": "a" * 64 if success else None,
        "bytes_hashed": size if success else None,
        "provenance": provenance if success else None,
        "reused_from_snapshot_id": _REUSE_SOURCE
        if provenance == "REUSED_FROM_VERIFIED_SNAPSHOT"
        else None,
        "failure_code": None,
        "os_error_code": None,
    }


def _entry(
    relative_path: str = "alpha.txt", *, observation: dict[str, object] | None = None
) -> dict[str, object]:
    size = int((observation or _observation())["bytes_hashed"] or 4)
    if observation and observation["status"] == "EMPTY_FILE_HASHED":
        size = 0
    return {
        "entry_id": entry_id(_SNAPSHOT_ID, "scope-a", relative_path),
        "snapshot_id": _SNAPSHOT_ID,
        "scope_id": "scope-a",
        "relative_path": relative_path,
        "object_type": "regular_file",
        "device_id": 1,
        "inode": 2,
        "mode": 33188,
        "uid": 501,
        "gid": 20,
        "size_bytes": size,
        "mtime_ns": 10,
        "ctime_ns": 11,
        "birthtime_ns": 9,
        "link_count": 1,
        "symlink_target_raw": None,
        "readable": True,
        "writable": True,
        "executable": False,
        "observation_status": "observed",
        "error_code": None,
        "error_message": None,
        "excluded": False,
        "allocated_size_bytes": 4096,
        "payload_observation": observation or _observation(size=size),
    }


def _evidence(entries: list[dict[str, object]] | None = None) -> dict[str, object]:
    values = canonical_snapshot_v2_entries(entries or [_entry()])
    statuses = [entry["payload_observation"]["status"] for entry in values]  # type: ignore[index]
    snapshot: dict[str, object] = {
        "snapshot_schema_version": 2,
        "snapshot_id": _SNAPSHOT_ID,
        "run_id": _RUN_ID,
        "created_at": "2026-01-01T00:00:00.000000Z",
        "started_at": "2026-01-01T00:00:00.000000Z",
        "completed_at": "2026-01-01T00:00:00.000000Z",
        "status": "complete",
        "consistency": "best_effort_point_in_time",
        "config_digest": "c" * 64,
        "scope_ids": ["scope-a"],
        "budget": {
            "max_entries": 10,
            "max_total_stat_bytes": None,
            "max_duration_seconds": 1.0,
            "max_depth": None,
        },
        "entry_count": len(values),
        "observed_count": len(values),
        "error_count": 0,
        "excluded_count": 0,
        "total_regular_file_bytes": sum(int(entry["size_bytes"] or 0) for entry in values),
        "max_depth_observed": max(
            (0 if entry["relative_path"] == "." else len(str(entry["relative_path"]).split("/")) for entry in values),
            default=0,
        ),
        "hash_policy": {
            "algorithm": "sha256",
            "algorithm_version": 1,
            "max_hash_file_bytes": None,
            "max_total_hash_bytes": None,
            "max_hash_duration_seconds": None,
            "hash_chunk_size": 1024,
            "allow_non_local_content": False,
            "allow_verified_reuse": False,
        },
        "allocated_regular_file_bytes_known_sum": sum(
            int(entry["allocated_size_bytes"] or 0) for entry in values
        ),
        "allocated_regular_file_unknown_count": sum(
            entry["allocated_size_bytes"] is None for entry in values
        ),
        "payload_observation_summary": {
            "status_counts": [
                {"status": status, "count": statuses.count(status)} for status in sorted(set(statuses))
            ]
        },
        "entries_digest": snapshot_v2_entries_digest(values),
        "snapshot_digest": "",
        "evidence_id": None,
        "evidence_relative_path": None,
        "entries": values,
    }
    snapshot["snapshot_digest"] = snapshot_v2_metadata_digest(snapshot)
    result: dict[str, object] = {
        "schema_version": 2,
        "evidence_id": _EVIDENCE_ID,
        "evidence_type": "filesystem.snapshot",
        "run_id": _RUN_ID,
        "sequence": 2,
        "created_at": "2026-01-01T00:00:01.000000Z",
        "tool_version": "0.1",
        "config_digest": "c" * 64,
        "policy_digest": None,
        "provider_versions": {},
        "previous_evidence_digest": "d" * 64,
        "payload": {"snapshot": snapshot, "entries": values},
        "evidence_digest": "",
    }
    result["evidence_digest"] = digest(result)
    return result


def _redigest(value: dict[str, object]) -> None:
    value["evidence_digest"] = digest(value)


def test_v2_round_trip_dispatch_and_read_adapter() -> None:
    value = _evidence()
    encoded = canonical_json(value)

    result = validate_snapshot_evidence(value)
    parsed = snapshot_from_valid_evidence_versioned(value, "runs/example/00000002_filesystem.snapshot.json")

    assert result.valid
    assert snapshot_evidence_schema_version(value) == 2
    assert canonical_json(__import__("json").loads(encoded)) == encoded
    assert parsed.snapshot_schema_version == 2  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.pop("schema_version"), "EVIDENCE_SCHEMA_VERSION_INVALID"),
        (lambda value: value.__setitem__("schema_version", True), "EVIDENCE_SCHEMA_VERSION_INVALID"),
        (lambda value: value.__setitem__("schema_version", 9), "EVIDENCE_SCHEMA_VERSION_UNSUPPORTED"),
    ],
)
def test_version_dispatch_rejects_missing_malformed_and_unknown(
    mutation, code: str
) -> None:
    value = _evidence()
    mutation(value)
    _redigest(value)

    result = validate_snapshot_evidence(value)

    assert not result.valid
    assert result.errors[0]["code"] == code


@pytest.mark.parametrize(
    "observation",
    [
        _observation(),
        _observation(status="EMPTY_FILE_HASHED", size=0),
        _observation(provenance="REUSED_FROM_VERIFIED_SNAPSHOT"),
    ],
)
def test_valid_v2_payload_observation_forms(observation: dict[str, object]) -> None:
    assert validate_snapshot_evidence(_evidence([_entry(observation=observation)])).valid


@pytest.mark.parametrize(
    "mutate",
    [
        lambda entry: entry["payload_observation"].__setitem__("digest", None),
        lambda entry: entry["payload_observation"].__setitem__("digest", "A" * 64),
        lambda entry: entry["payload_observation"].__setitem__("bytes_hashed", -1),
        lambda entry: entry["payload_observation"].__setitem__("provenance", "DERIVED_CACHE"),
        lambda entry: entry.__setitem__("allocated_size_bytes", -1),
        lambda entry: entry["payload_observation"].__setitem__("reused_from_snapshot_id", _REUSE_SOURCE),
    ],
)
def test_invalid_v2_payload_observation_forms_are_rejected(mutate) -> None:
    value = _evidence()
    entry = value["payload"]["entries"][0]  # type: ignore[index]
    mutate(entry)
    value["payload"]["snapshot"]["entries"] = value["payload"]["entries"]  # type: ignore[index]
    _redigest(value)

    assert not validate_snapshot_evidence(value).valid


def test_canonical_entry_digest_is_input_order_independent_but_verifier_requires_order() -> None:
    left = _entry("Case.txt")
    right = _entry("é.txt")
    assert snapshot_v2_entries_digest([left, right]) == snapshot_v2_entries_digest([right, left])
    value = _evidence([right, left])
    value["payload"]["entries"].reverse()  # type: ignore[index]
    value["payload"]["snapshot"]["entries"] = value["payload"]["entries"]  # type: ignore[index]
    _redigest(value)

    result = validate_snapshot_evidence(value)

    assert not result.valid
    assert not result.entry_order_valid


def test_duplicate_scope_location_and_aggregate_tampering_are_rejected() -> None:
    first = _entry("same.txt")
    second = deepcopy(first)
    second["entry_id"] = "f" * 64
    value = _evidence([first, second])
    _redigest(value)
    assert not validate_snapshot_evidence(value).valid

    value = _evidence()
    value["payload"]["snapshot"]["allocated_regular_file_bytes_known_sum"] = 0  # type: ignore[index]
    _redigest(value)
    assert not validate_snapshot_evidence(value).valid


def test_v2_payload_identity_is_not_yet_a_stat_diff_field() -> None:
    left = _evidence()
    right = _evidence()
    right_entry = right["payload"]["entries"][0]  # type: ignore[index]
    right_entry["payload_observation"]["digest"] = "b" * 64
    right["payload"]["snapshot"]["entries"] = right["payload"]["entries"]  # type: ignore[index]
    right_snapshot = right["payload"]["snapshot"]  # type: ignore[index]
    right_snapshot["entries_digest"] = snapshot_v2_entries_digest(right["payload"]["entries"])  # type: ignore[index]
    right_snapshot["snapshot_digest"] = snapshot_v2_metadata_digest(right_snapshot)
    _redigest(right)

    left_model = snapshot_v2_from_valid_evidence(left, "left.json")
    right_model = snapshot_v2_from_valid_evidence(right, "right.json")
    diff = compute_snapshot_diff(snapshot_v2_stat_view(left_model), snapshot_v2_stat_view(right_model))

    assert diff.summary.modified_count == 0
    assert diff.summary.unchanged_count == 1


def test_v2_parser_requires_validated_document() -> None:
    value = _evidence()
    UUID(_REUSE_SOURCE)
    assert snapshot_v2_from_valid_evidence(value, "memory.json").snapshot_id == _SNAPSHOT_ID
