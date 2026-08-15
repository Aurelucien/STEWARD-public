from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.models import SnapshotDiffChangeType
from local_steward.snapshot_diff import compute_snapshot_diff, compute_verified_snapshot_diff

from .test_snapshot_queries import snapshot_fixture


def _snapshot_with_entries(snapshot, entries, snapshot_id: str):
    return replace(snapshot, snapshot_id=snapshot_id, entries=tuple(entries))


def _types(diff):
    return {item.relative_path: item.change_type for item in diff.items}


def test_diff_empty_snapshots(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    left = _snapshot_with_entries(snapshot, (), "left-empty")
    right = _snapshot_with_entries(snapshot, (), "right-empty")

    diff = compute_snapshot_diff(left, right)

    assert diff.items == ()
    assert diff.summary.item_count == 0


def test_diff_same_snapshot_is_unchanged(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)

    diff = compute_snapshot_diff(snapshot, snapshot)

    assert all(item.change_type == SnapshotDiffChangeType.UNCHANGED for item in diff.items)
    assert diff.summary.unchanged_count == len(snapshot.entries)


def test_diff_single_file_added_and_removed_are_symmetric(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    added_entry = snapshot.entries[-1]
    left = _snapshot_with_entries(snapshot, snapshot.entries[:-1], "left")
    right = _snapshot_with_entries(snapshot, snapshot.entries, "right")

    forward = compute_snapshot_diff(left, right)
    reverse = compute_snapshot_diff(right, left)

    assert _types(forward)[added_entry.relative_path] == SnapshotDiffChangeType.ADDED
    assert _types(reverse)[added_entry.relative_path] == SnapshotDiffChangeType.REMOVED
    assert forward.summary.added_count == reverse.summary.removed_count == 1


def test_diff_single_file_modified_reports_recorded_fields(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    original = snapshot.entries[0]
    changed = replace(original, size_bytes=(original.size_bytes or 0) + 1, writable=not original.writable)
    right = _snapshot_with_entries(snapshot, (changed, *snapshot.entries[1:]), "right")

    diff = compute_snapshot_diff(snapshot, right)
    item = next(item for item in diff.items if item.relative_path == original.relative_path)

    assert item.change_type == SnapshotDiffChangeType.MODIFIED
    assert item.changed_fields == ("size_bytes", "writable")
    assert item.left_entry == original and item.right_entry == changed


def test_diff_multiple_changes_and_stable_path_order(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    removed, modified, unchanged = snapshot.entries[:3]
    changed = replace(modified, mode=(modified.mode or 0) + 1)
    added = replace(unchanged, relative_path="z-added.txt", entry_id="different-entry")
    left = _snapshot_with_entries(snapshot, (unchanged, modified, removed), "left")
    right = _snapshot_with_entries(snapshot, (added, unchanged, changed), "right")

    diff = compute_snapshot_diff(left, right)

    assert _types(diff) == {
        removed.relative_path: SnapshotDiffChangeType.REMOVED,
        modified.relative_path: SnapshotDiffChangeType.MODIFIED,
        unchanged.relative_path: SnapshotDiffChangeType.UNCHANGED,
        "z-added.txt": SnapshotDiffChangeType.ADDED,
    }
    assert [item.relative_path for item in diff.items] == sorted(
        item.relative_path for item in diff.items
    )
    assert diff.summary.added_count == diff.summary.removed_count == diff.summary.modified_count == 1
    assert diff.summary.unchanged_count == 1


def test_diff_input_entry_order_does_not_change_result(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    right = _snapshot_with_entries(snapshot, tuple(reversed(snapshot.entries)), "right")

    ordered = compute_snapshot_diff(snapshot, right)
    shuffled = compute_snapshot_diff(
        _snapshot_with_entries(snapshot, tuple(reversed(snapshot.entries)), "left"),
        _snapshot_with_entries(snapshot, snapshot.entries, "right"),
    )

    assert ordered.items == shuffled.items
    assert ordered.summary == shuffled.summary


def test_diff_large_entry_set_is_stable(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    source = snapshot.entries[0]
    entries = tuple(
        replace(source, relative_path=f"many/{number:04d}.txt", entry_id=f"entry-{number}")
        for number in range(1_000)
    )
    left = _snapshot_with_entries(snapshot, tuple(reversed(entries)), "left")
    right = _snapshot_with_entries(snapshot, entries, "right")

    first = compute_snapshot_diff(left, right)
    second = compute_snapshot_diff(left, right)

    assert first == second
    assert first.summary.unchanged_count == first.summary.item_count == 1_000
    assert [item.relative_path for item in first.items] == sorted(
        item.relative_path for item in first.items
    )


def test_diff_does_not_access_live_filesystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("live filesystem access is forbidden")

    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)

    assert compute_snapshot_diff(snapshot, snapshot).summary.unchanged_count == len(snapshot.entries)


def test_diff_does_not_mutate_database_or_evidence(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    database = config.paths.data_dir / "state.db"
    database_before = database.read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }

    compute_snapshot_diff(snapshot, snapshot)

    assert database.read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before


def test_verified_diff_uses_existing_snapshot_validation(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)

    diff = compute_verified_snapshot_diff(config, snapshot.snapshot_id, snapshot.snapshot_id)

    assert diff.left_snapshot_id == diff.right_snapshot_id == snapshot.snapshot_id
    assert diff.summary.unchanged_count == len(snapshot.entries)
