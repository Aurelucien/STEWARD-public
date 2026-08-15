"""Pure coverage for exact payload duplicate and storage analysis."""

from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.duplicate_analysis import (
    canonical_duplicate_analysis,
    compute_snapshot_duplicate_analysis,
    compute_verified_snapshot_duplicate_analysis,
)
from local_steward.database import database_path
from local_steward.errors import DuplicateAnalysisError
from local_steward.models import (
    DuplicateStorageKnowledgeStatus,
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemObjectType,
    FilesystemObservationStatus,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    PayloadHashPolicy,
    PayloadObservation,
    PayloadObservationProvenance,
    PayloadObservationStatus,
    ScanBudget,
    SnapshotConsistency,
    SnapshotStatus,
)
from local_steward.payload_hashing import PayloadLocality, default_payload_hash_policy
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot

from .test_protocol_completion import prepared_config


def _entry(
    snapshot_id: str,
    path: str,
    *,
    scope_id: str = "managed",
    device_id: int | None = 1,
    inode: int | None = 10,
    size_bytes: int | None = 10,
    object_type: FilesystemObjectType = FilesystemObjectType.REGULAR_FILE,
    status: FilesystemObservationStatus = FilesystemObservationStatus.OBSERVED,
    excluded: bool = False,
    link_count: int | None = 1,
) -> FilesystemEntry:
    return FilesystemEntry(
        f"{snapshot_id}:{scope_id}:{path}",
        snapshot_id,
        scope_id,
        path,
        object_type,
        device_id,
        inode,
        0o644,
        1,
        2,
        size_bytes,
        20,
        30,
        5,
        link_count,
        None,
        True,
        False,
        False,
        status,
        None,
        None,
        excluded,
    )


def _v2(
    entry: FilesystemEntry,
    digest: str | None,
    *,
    reused: bool = False,
    payload_status: PayloadObservationStatus | None = None,
    failure_code: str | None = None,
    allocated_size_bytes: int | None = None,
) -> FilesystemEntryV2:
    success = digest is not None
    status = payload_status or (
        PayloadObservationStatus.EMPTY_FILE_HASHED
        if success and entry.size_bytes == 0
        else PayloadObservationStatus.HASHED if success else PayloadObservationStatus.UNSUPPORTED
    )
    payload = PayloadObservation(
        status,
        "sha256" if success else None,
        1 if success else None,
        digest,
        entry.size_bytes if success else None,
        (
            PayloadObservationProvenance.REUSED_FROM_VERIFIED_SNAPSHOT
            if reused and success
            else PayloadObservationProvenance.DIRECT_READ if success else None
        ),
        "source" if reused and success else None,
        failure_code,
        None,
    )
    return FilesystemEntryV2(*entry.__getstate__(), allocated_size_bytes, payload)


def _snapshot(
    entries: tuple[FilesystemEntry | FilesystemEntryV2, ...], *, v2: bool = True
) -> FilesystemSnapshot | FilesystemSnapshotV2:
    shared = (
        "snapshot",
        "run",
        "2026-01-01T00:00:01.000000Z",
        "2026-01-01T00:00:00.000000Z",
        "2026-01-01T00:00:01.000000Z",
        SnapshotStatus.COMPLETE,
        SnapshotConsistency.BEST_EFFORT_POINT_IN_TIME,
        "config",
        ("managed",),
        ScanBudget(),
        len(entries),
        len(entries),
        0,
        0,
        sum(entry.size_bytes or 0 for entry in entries),
        1,
    )
    if not v2:
        return FilesystemSnapshot(*shared, "entries", "snapshot", None, None, entries)  # type: ignore[arg-type]
    return FilesystemSnapshotV2(
        2,
        *shared,
        PayloadHashPolicy("sha256", 1, None, None, None, 4096, False, False),
        0,
        len(entries),
        (),
        "entries",
        "snapshot",
        None,
        None,
        entries,  # type: ignore[arg-type]
    )


def test_distinct_object_hints_form_an_exact_payload_duplicate_group() -> None:
    digest = "a" * 64
    result = compute_snapshot_duplicate_analysis(
        _snapshot(
            (
                _v2(_entry("snapshot", "a", inode=10), digest),
                _v2(_entry("snapshot", "b", inode=11), digest),
            )
        )
    )
    assert len(result.payload_equality_groups) == 1
    group = result.payload_equality_groups[0]
    assert group.is_exact_duplicate
    assert group.known_storage_unit_count == 2
    assert group.unknown_storage_unit_count == 0
    assert group.logical_redundant_bytes == 10
    assert result.physical_storage.reclaimable_bytes is None
    assert result.physical_storage.reclaimable_status == DuplicateStorageKnowledgeStatus.UNKNOWN


def test_hard_link_aliases_are_one_storage_unit_not_duplicate_storage() -> None:
    digest = "a" * 64
    result = compute_snapshot_duplicate_analysis(
        _snapshot(
            (
                _v2(_entry("snapshot", "a", inode=10, link_count=2), digest),
                _v2(_entry("snapshot", "b", inode=10, link_count=2), digest),
            )
        )
    )
    group = result.payload_equality_groups[0]
    assert not group.is_exact_duplicate
    assert group.known_storage_unit_count == 1
    assert group.logical_redundant_bytes is None
    assert len(result.hard_link_alias_sets) == 1
    assert result.coverage.alias_path_count == 2


def test_unknown_object_hints_preserve_payload_equality_without_physical_claim() -> None:
    digest = "a" * 64
    result = compute_snapshot_duplicate_analysis(
        _snapshot(
            (
                _v2(_entry("snapshot", "a", device_id=None, inode=None), digest),
                _v2(_entry("snapshot", "b", device_id=None, inode=None), digest),
            )
        )
    )
    group = result.payload_equality_groups[0]
    assert not group.is_exact_duplicate
    assert group.known_storage_unit_count == 0
    assert group.unknown_storage_unit_count == 2
    assert group.logical_redundant_bytes is None


def test_direct_and_repository_verified_reused_payloads_have_equal_strength() -> None:
    digest = "a" * 64
    snapshot = _snapshot(
        (
            _v2(_entry("snapshot", "direct", inode=10), digest),
            _v2(_entry("snapshot", "reused", inode=11), digest, reused=True),
        )
    )
    assert compute_snapshot_duplicate_analysis(snapshot).payload_equality_groups == ()
    result = compute_snapshot_duplicate_analysis(snapshot, reused_payloads_verified=True)
    assert result.payload_equality_groups[0].is_exact_duplicate


def test_v1_and_unknown_payloads_are_coverage_not_unique_claims() -> None:
    v1 = _snapshot((_entry("snapshot", "legacy"),), v2=False)
    result = compute_snapshot_duplicate_analysis(v1)
    assert result.payload_equality_groups == ()
    assert result.coverage.payload_unknown_regular_entry_count == 1
    assert result.coverage.payload_unknown_reason_counts[0].code == "V1_PAYLOAD_UNAVAILABLE"

    skipped = _snapshot(
        (_v2(_entry("snapshot", "skipped"), None, payload_status=PayloadObservationStatus.FILE_TOO_LARGE),)
    )
    skipped_result = compute_snapshot_duplicate_analysis(skipped)
    assert skipped_result.coverage.payload_unknown_reason_counts[0].code == "FILE_TOO_LARGE"


def test_zero_byte_group_and_scope_overlap_are_deterministic() -> None:
    digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    snapshot = _snapshot(
        (
            _v2(_entry("snapshot", "a", scope_id="one", inode=10, size_bytes=0), digest),
            _v2(_entry("snapshot", "b", scope_id="two", inode=11, size_bytes=0), digest),
        )
    )
    result = compute_snapshot_duplicate_analysis(snapshot)
    group = result.payload_equality_groups[0]
    assert group.is_exact_duplicate
    assert group.logical_redundant_bytes == 0
    assert group.path_logical_bytes == 0
    reversed_result = compute_snapshot_duplicate_analysis(replace(snapshot, entries=tuple(reversed(snapshot.entries))))
    assert canonical_duplicate_analysis(result) == canonical_duplicate_analysis(reversed_result)
    assert result.analysis_digest == reversed_result.analysis_digest


def test_payload_size_and_object_hint_conflicts_are_explicit() -> None:
    digest = "a" * 64
    size_conflict = compute_snapshot_duplicate_analysis(
        _snapshot(
            (
                _v2(_entry("snapshot", "a", size_bytes=10), digest),
                _v2(_entry("snapshot", "b", inode=11, size_bytes=11), digest),
            )
        )
    )
    assert size_conflict.payload_equality_groups == ()
    assert [item.code for item in size_conflict.integrity_conflicts] == ["PAYLOAD_SIZE_CONFLICT"]

    object_conflict = compute_snapshot_duplicate_analysis(
        _snapshot(
            (
                _v2(_entry("snapshot", "a", inode=10, link_count=2), "a" * 64),
                _v2(_entry("snapshot", "b", inode=10, link_count=2), "b" * 64),
            )
        )
    )
    assert [item.code for item in object_conflict.integrity_conflicts] == ["OBJECT_HINT_PAYLOAD_CONFLICT"]


def test_link_count_and_allocation_conflicts_are_explicit_without_reclaimable_estimate() -> None:
    digest = "a" * 64
    result = compute_snapshot_duplicate_analysis(
        _snapshot(
            (
                _v2(_entry("snapshot", "a", inode=10, link_count=1), digest, allocated_size_bytes=16),
                _v2(_entry("snapshot", "b", inode=10, link_count=1), digest, allocated_size_bytes=32),
            )
        )
    )
    assert [item.code for item in result.integrity_conflicts] == [
        "ALLOCATED_SIZE_CONFLICT",
        "LINK_COUNT_CONFLICT",
    ]
    assert result.physical_storage.reclaimable_bytes is None


def test_non_regular_entries_do_not_enter_payload_groups() -> None:
    digest = "a" * 64
    symlink = _v2(
        _entry("snapshot", "link", object_type=FilesystemObjectType.SYMLINK),
        digest,
    )
    directory = _v2(
        _entry("snapshot", "directory", object_type=FilesystemObjectType.DIRECTORY),
        digest,
    )
    result = compute_snapshot_duplicate_analysis(_snapshot((symlink, directory)))
    assert result.payload_equality_groups == ()
    assert result.coverage.total_regular_entry_count == 0


def test_duplicate_scoped_location_is_rejected_by_pure_input_boundary() -> None:
    first = _v2(_entry("snapshot", "same", inode=10), "a" * 64)
    second = _v2(_entry("snapshot", "same", inode=11), "a" * 64)
    with pytest.raises(DuplicateAnalysisError, match="DUPLICATE_INVALID"):
        compute_snapshot_duplicate_analysis(_snapshot((first, second)))


def test_digest_covers_membership_classification_and_conflicts() -> None:
    digest = "a" * 64
    normal = compute_snapshot_duplicate_analysis(
        _snapshot(
            (
                _v2(_entry("snapshot", "a", inode=10), digest),
                _v2(_entry("snapshot", "b", inode=11), digest),
            )
        )
    )
    unknown = compute_snapshot_duplicate_analysis(
        _snapshot(
            (
                _v2(_entry("snapshot", "a", inode=10), digest),
                _v2(_entry("snapshot", "b", device_id=None, inode=None), digest),
            )
        )
    )
    assert normal.analysis_digest != unknown.analysis_digest
    conflict = compute_snapshot_duplicate_analysis(
        _snapshot(
            (
                _v2(_entry("snapshot", "a", inode=10), digest),
                _v2(_entry("snapshot", "b", inode=10), "b" * 64),
            )
        )
    )
    assert normal.analysis_digest != conflict.analysis_digest


def test_verified_loader_uses_valid_snapshot_evidence_without_writing(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    observed = tmp_path / "observed"
    observed.mkdir()
    (observed / "a.txt").write_text("same", encoding="utf-8")
    (observed / "b.txt").write_text("same", encoding="utf-8")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=observed),))
    snapshot = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    database_before = database_path(config).read_bytes()
    evidence_before = tuple(sorted(path.read_bytes() for path in config.paths.evidence_dir.rglob("*.json")))
    result = compute_verified_snapshot_duplicate_analysis(config, snapshot.snapshot_id)
    assert result.snapshot_id == snapshot.snapshot_id
    assert result.payload_equality_groups[0].is_exact_duplicate
    assert database_path(config).read_bytes() == database_before
    assert tuple(sorted(path.read_bytes() for path in config.paths.evidence_dir.rglob("*.json"))) == evidence_before
