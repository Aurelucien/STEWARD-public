"""Pure protocol coverage for directional Path View storage growth."""

from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.database import database_path
from local_steward.errors import GrowthError
from local_steward.models import (
    DuplicateStorageKnowledgeStatus,
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemObjectType,
    FilesystemObservationStatus,
    GrowthContributionKind,
)
from local_steward.payload_hashing import PayloadLocality, default_payload_hash_policy
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot
from local_steward.storage_growth import (
    canonical_storage_growth,
    compute_snapshot_growth,
    compute_verified_snapshot_growth,
)

from .test_duplicate_analysis import _entry, _snapshot
from .test_protocol_completion import prepared_config


Entry = FilesystemEntry | FilesystemEntryV2


def _at(
    snapshot_id: str,
    entries: tuple[Entry, ...],
    *,
    started_at: str,
    completed_at: str,
    scope_ids: tuple[str, ...] = ("managed",),
    v2: bool = True,
):
    prepared = tuple(
        replace(entry, snapshot_id=snapshot_id, entry_id=f"{snapshot_id}:{entry.scope_id}:{entry.relative_path}")
        for entry in entries
    )
    snapshot = _snapshot(prepared, v2=v2)
    return replace(
        snapshot,
        snapshot_id=snapshot_id,
        created_at=completed_at,
        started_at=started_at,
        completed_at=completed_at,
        scope_ids=scope_ids,
        entries=prepared,
    )


def _pair(
    base_entries: tuple[Entry, ...], target_entries: tuple[Entry, ...], *, v2: bool = True
):
    return (
        _at(
            "base",
            base_entries,
            started_at="2026-01-01T00:00:00.000000Z",
            completed_at="2026-01-01T00:00:01.000000Z",
            v2=v2,
        ),
        _at(
            "target",
            target_entries,
            started_at="2026-01-01T00:00:02.000000Z",
            completed_at="2026-01-01T00:00:03.000000Z",
            v2=v2,
        ),
    )


def _node(result, scope_id: str, path: str):
    return next(
        item
        for item in result.path_nodes
        if item.scope_id == scope_id and item.relative_directory_path == path
    )


def test_leaf_attribution_and_global_identity() -> None:
    base, target = _pair(
        (
            _entry("base", ".", object_type=FilesystemObjectType.DIRECTORY),
            _entry("base", "removed", size_bytes=7),
            _entry("base", "larger", size_bytes=3),
            _entry("base", "smaller", size_bytes=9),
            _entry("base", "same", size_bytes=4),
        ),
        (
            _entry("target", ".", object_type=FilesystemObjectType.DIRECTORY),
            _entry("target", "added", size_bytes=5),
            _entry("target", "larger", size_bytes=8),
            _entry("target", "smaller", size_bytes=2),
            _entry("target", "same", size_bytes=4),
        ),
    )
    result = compute_snapshot_growth(base, target)
    assert [item.kind for item in result.contributions] == [
        GrowthContributionKind.ADDED_LOCATION,
        GrowthContributionKind.SAME_LOCATION_SIZE_INCREASE,
        GrowthContributionKind.REMOVED_LOCATION,
        GrowthContributionKind.SAME_LOCATION_SIZE_UNCHANGED,
        GrowthContributionKind.SAME_LOCATION_SIZE_DECREASE,
    ]
    root = _node(result, "managed", ".")
    assert root.recursive_added_logical_bytes == 5
    assert root.recursive_removed_logical_bytes == 7
    assert root.recursive_same_location_increase_bytes == 5
    assert root.recursive_same_location_decrease_bytes == 7
    assert root.recursive_known_net_logical_delta == -4
    assert result.coverage.known_net_logical_delta == -4
    assert result.coverage.decomposition_complete
    assert root.decomposition_complete


def test_nested_direct_recursive_and_cross_directory_transfer() -> None:
    base, target = _pair(
        (
            _entry("base", ".", object_type=FilesystemObjectType.DIRECTORY),
            _entry("base", "old/file", size_bytes=6),
            _entry("base", "stable/file", size_bytes=2),
        ),
        (
            _entry("target", ".", object_type=FilesystemObjectType.DIRECTORY),
            _entry("target", "new/file", size_bytes=6),
            _entry("target", "stable/file", size_bytes=2),
        ),
    )
    result = compute_snapshot_growth(base, target)
    assert _node(result, "managed", "old").recursive_removed_logical_bytes == 6
    assert _node(result, "managed", "new").recursive_added_logical_bytes == 6
    assert _node(result, "managed", ".").recursive_known_net_logical_delta == 0
    assert _node(result, "managed", "stable").recursive_same_location_unchanged_count == 1


def test_unknown_sizes_are_not_zero_and_make_decomposition_incomplete() -> None:
    base, target = _pair(
        (_entry("base", "known", size_bytes=4), _entry("base", "unknown", size_bytes=None)),
        (
            _entry("target", "known", size_bytes=None, status=FilesystemObservationStatus.IO_ERROR),
            _entry("target", "added", size_bytes=3),
        ),
    )
    result = compute_snapshot_growth(base, target)
    root = _node(result, "managed", ".")
    assert root.recursive_unknown_size_contribution_count == 2
    assert root.recursive_added_logical_bytes == 3
    assert not root.decomposition_complete
    assert not result.coverage.decomposition_complete
    assert all(
        item.known_byte_delta is None
        for item in result.contributions
        if item.kind == GrowthContributionKind.SIZE_UNKNOWN
    )


def test_non_regular_and_excluded_entries_do_not_contribute_logical_bytes() -> None:
    base, target = _pair(
        (
            _entry("base", "item", size_bytes=4),
            _entry("base", "link", object_type=FilesystemObjectType.SYMLINK, size_bytes=10),
            _entry("base", "excluded", size_bytes=7, excluded=True),
        ),
        (
            _entry("target", "item", object_type=FilesystemObjectType.DIRECTORY, size_bytes=99),
            _entry("target", "link", object_type=FilesystemObjectType.SYMLINK, size_bytes=2),
            _entry("target", "excluded", size_bytes=1, excluded=True),
        ),
    )
    result = compute_snapshot_growth(base, target)
    assert result.contributions[0].kind == GrowthContributionKind.REMOVED_LOCATION
    assert result.contributions[0].known_byte_delta == -4
    assert result.coverage.known_net_logical_delta == -4


def test_multi_scope_overlap_and_hard_links_remain_path_view_facts() -> None:
    base_entries = (
        _entry("base", ".", scope_id="one", object_type=FilesystemObjectType.DIRECTORY),
        _entry("base", "hard-a", scope_id="one", inode=10, size_bytes=4),
        _entry("base", "hard-b", scope_id="one", inode=10, size_bytes=4),
        _entry("base", ".", scope_id="two", object_type=FilesystemObjectType.DIRECTORY),
        _entry("base", "shared", scope_id="two", inode=10, size_bytes=4),
    )
    target_entries = tuple(replace(item, snapshot_id="target") for item in base_entries) + (
        _entry("target", "new", scope_id="two", inode=20, size_bytes=3),
    )
    base = _at(
        "base",
        base_entries,
        started_at="2026-01-01T00:00:00.000000Z",
        completed_at="2026-01-01T00:00:01.000000Z",
        scope_ids=("one", "two"),
    )
    target = _at(
        "target",
        target_entries,
        started_at="2026-01-01T00:00:02.000000Z",
        completed_at="2026-01-01T00:00:03.000000Z",
        scope_ids=("one", "two"),
    )
    result = compute_snapshot_growth(base, target)
    assert _node(result, "one", ".").recursive_target_known_logical_bytes == 8
    assert _node(result, "two", ".").recursive_target_known_logical_bytes == 7
    assert result.coverage.target_scope_overlap_object_hint_count == 1
    assert result.coverage.known_net_logical_delta == 3


def test_v1_v2_permutations_and_digest_are_deterministic() -> None:
    base, target = _pair(
        (_entry("base", "a", size_bytes=1), _entry("base", "nested/b", size_bytes=2)),
        (_entry("target", "a", size_bytes=3), _entry("target", "nested/c", size_bytes=2)),
        v2=False,
    )
    first = compute_snapshot_growth(base, target)
    second = compute_snapshot_growth(
        replace(base, entries=tuple(reversed(base.entries))),
        replace(target, entries=tuple(reversed(target.entries))),
    )
    assert canonical_storage_growth(first) == canonical_storage_growth(second)
    assert first.growth_digest == second.growth_digest
    assert [item.growth_contribution_id for item in first.contributions] == [
        item.growth_contribution_id for item in second.contributions
    ]


def test_same_and_reverse_pairs_are_rejected() -> None:
    base, target = _pair((_entry("base", "a", size_bytes=1),), (_entry("target", "a", size_bytes=2),))
    with pytest.raises(GrowthError, match="GROWTH_INVALID"):
        compute_snapshot_growth(base, base)
    with pytest.raises(GrowthError, match="GROWTH_INVALID"):
        compute_snapshot_growth(target, base)


def test_verified_loader_is_read_only(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    root = tmp_path / "observed"
    root.mkdir()
    (root / "a.txt").write_text("first", encoding="utf-8")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=root),))
    base = create_snapshot(config, (), make_budget())
    (root / "a.txt").write_text("second value", encoding="utf-8")
    target = create_snapshot(
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
    result = compute_verified_snapshot_growth(config, base.snapshot_id, target.snapshot_id)
    assert result.coverage.known_net_logical_delta == len("second value") - len("first")
    assert result.physical_boundary.allocation_status == DuplicateStorageKnowledgeStatus.UNKNOWN
    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before
