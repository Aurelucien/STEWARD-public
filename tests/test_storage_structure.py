"""Pure protocol coverage for single-Snapshot Path View structure analysis."""

from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.database import database_path
from local_steward.errors import StructureError
from local_steward.models import (
    DuplicateStorageKnowledgeStatus,
    FilesystemObjectType,
    FilesystemObservationStatus,
)
from local_steward.payload_hashing import PayloadLocality, default_payload_hash_policy
from local_steward.scan_budget import make_budget
from local_steward.storage_structure import (
    canonical_storage_structure,
    compute_snapshot_structure,
    compute_verified_snapshot_structure,
)
from local_steward.snapshots import create_snapshot

from .test_duplicate_analysis import _entry, _snapshot, _v2
from .test_protocol_completion import prepared_config


def _node(result, scope_id: str, path: str):
    return next(
        node
        for node in result.path_nodes
        if node.scope_id == scope_id and node.relative_directory_path == path
    )


def test_path_view_direct_recursive_metrics_and_coverage() -> None:
    entries = (
        _entry("snapshot", ".", object_type=FilesystemObjectType.DIRECTORY, size_bytes=99),
        _entry("snapshot", "top", size_bytes=5),
        _entry("snapshot", "zero", size_bytes=0),
        _entry("snapshot", "docs", object_type=FilesystemObjectType.DIRECTORY, size_bytes=80),
        _entry("snapshot", "docs/paper", size_bytes=10),
        _entry(
            "snapshot",
            "docs/missing",
            size_bytes=None,
            status=FilesystemObservationStatus.IO_ERROR,
        ),
        _entry("snapshot", "docs/link", object_type=FilesystemObjectType.SYMLINK, size_bytes=20),
        _entry("snapshot", "docs/fifo", object_type=FilesystemObjectType.FIFO, size_bytes=1),
        _entry("snapshot", "docs/excluded", size_bytes=7, excluded=True),
    )
    result = compute_snapshot_structure(_snapshot(entries))
    root = _node(result, "managed", ".")
    docs = _node(result, "managed", "docs")
    assert root.direct_regular_file_count == 2
    assert root.recursive_regular_file_count == 4
    assert root.direct_known_logical_bytes == 5
    assert root.recursive_known_logical_bytes == 15
    assert root.recursive_unknown_size_regular_count == 1
    assert root.recursive_directory_count == 2
    assert docs.direct_regular_file_count == 2
    assert docs.direct_known_logical_bytes == 10
    assert docs.direct_unknown_size_regular_count == 1
    assert docs.recursive_symlink_count == 1 and docs.recursive_special_object_count == 1
    assert result.coverage.known_logical_bytes == 15
    assert result.coverage.known_size_regular_file_count == 3
    assert result.coverage.unknown_size_regular_file_count == 1
    assert result.coverage.excluded_entry_count == 1
    assert result.coverage.metadata_failed_entry_count == 1
    assert not result.coverage.complete
    assert result.scope_summaries[0].recursive_known_logical_bytes == root.recursive_known_logical_bytes
    assert result.physical_boundary.allocation_status == DuplicateStorageKnowledgeStatus.UNKNOWN
    assert result.physical_boundary.reclaimable_bytes is None
    assert result.physical_boundary.object_aware_capacity_status == DuplicateStorageKnowledgeStatus.UNKNOWN


def test_derived_prefixes_and_input_permutation_are_deterministic() -> None:
    entries = (
        _entry("snapshot", "a/b/c.txt", size_bytes=3),
        _entry("snapshot", "unicodé/file.txt", size_bytes=4),
        _entry("snapshot", ".", object_type=FilesystemObjectType.DIRECTORY),
    )
    first = compute_snapshot_structure(_snapshot(entries))
    second = compute_snapshot_structure(_snapshot(tuple(reversed(entries))))
    assert _node(first, "managed", "a").observed_directory_entry is False
    assert _node(first, "managed", "a/b").recursive_known_logical_bytes == 3
    assert canonical_storage_structure(first) == canonical_storage_structure(second)
    assert first.structure_digest == second.structure_digest
    assert [node.path_node_id for node in first.path_nodes] == [node.path_node_id for node in second.path_nodes]


def test_path_view_reports_scope_overlap_without_deduplicating_paths() -> None:
    entries = (
        _entry("snapshot", ".", scope_id="one", object_type=FilesystemObjectType.DIRECTORY),
        _entry("snapshot", "shared", scope_id="one", device_id=1, inode=10, size_bytes=8),
        _entry("snapshot", ".", scope_id="two", object_type=FilesystemObjectType.DIRECTORY),
        _entry("snapshot", "shared", scope_id="two", device_id=1, inode=10, size_bytes=8),
    )
    snapshot = replace(_snapshot(entries), scope_ids=("one", "two"))
    result = compute_snapshot_structure(snapshot)
    assert _node(result, "one", ".").recursive_known_logical_bytes == 8
    assert _node(result, "two", ".").recursive_known_logical_bytes == 8
    assert result.coverage.known_logical_bytes == 16
    assert result.coverage.scope_overlap_object_hint_count == 1
    assert result.coverage.repeated_known_object_hint_path_count == 2
    assert result.limitations[0].code == "SCOPE_OVERLAP_OBJECT_HINT"
    assert len(result.limitations[0].entries) == 2


def test_hard_link_paths_and_duplicate_payloads_remain_path_observations() -> None:
    entries = (
        _entry("snapshot", ".", object_type=FilesystemObjectType.DIRECTORY),
        _v2(_entry("snapshot", "hard-a", inode=10, size_bytes=6), "a" * 64),
        _v2(_entry("snapshot", "hard-b", inode=10, size_bytes=6), "a" * 64),
        _v2(_entry("snapshot", "same-payload", inode=11, size_bytes=6), "a" * 64),
    )
    result = compute_snapshot_structure(_snapshot(entries))
    root = _node(result, "managed", ".")
    assert root.recursive_regular_file_count == 3
    assert root.recursive_known_logical_bytes == 18
    assert result.coverage.repeated_known_object_hint_path_count == 2
    assert result.coverage.scope_overlap_object_hint_count == 0


def test_unknown_object_hints_are_limitations_not_deduplication() -> None:
    snapshot = _snapshot(
        (
            _entry("snapshot", ".", object_type=FilesystemObjectType.DIRECTORY),
            _entry("snapshot", "unknown", device_id=None, inode=None, size_bytes=2),
        )
    )
    result = compute_snapshot_structure(snapshot)
    assert result.coverage.object_hint_unavailable_entry_count == 1
    assert result.limitations[0].code == "OBJECT_HINT_UNAVAILABLE"
    assert _node(result, "managed", ".").recursive_known_logical_bytes == 2


@pytest.mark.parametrize("path", ["/absolute", "../escape", "a//b", "a/../b"])
def test_path_hierarchy_errors_are_rejected(path: str) -> None:
    snapshot = _snapshot((_entry("snapshot", path),))
    with pytest.raises(StructureError, match="STRUCTURE_INVALID"):
        compute_snapshot_structure(snapshot)


def test_duplicate_locations_and_non_directory_prefixes_are_rejected() -> None:
    duplicate = _snapshot((_entry("snapshot", "same"), _entry("snapshot", "same", inode=11)))
    with pytest.raises(StructureError, match="DUPLICATE_INVALID|STRUCTURE_INVALID"):
        compute_snapshot_structure(duplicate)
    prefix = _snapshot((_entry("snapshot", "file"), _entry("snapshot", "file/child")))
    with pytest.raises(StructureError, match="STRUCTURE_INVALID"):
        compute_snapshot_structure(prefix)
    negative = _snapshot((_entry("snapshot", "bad", size_bytes=-1),))
    with pytest.raises(StructureError, match="STRUCTURE_INVALID"):
        compute_snapshot_structure(negative)


def test_v1_and_verified_v2_loaders_are_read_only(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    root = tmp_path / "observed"
    root.mkdir()
    (root / "a.txt").write_text("same", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "b.txt").write_text("same", encoding="utf-8")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=root),))
    v1 = create_snapshot(config, (), make_budget())
    v2 = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    database_before = database_path(config).read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }
    assert compute_verified_snapshot_structure(config, v1.snapshot_id).coverage.known_logical_bytes == 8
    assert compute_verified_snapshot_structure(config, v2.snapshot_id).coverage.known_logical_bytes == 8
    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before
