from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.change_semantics import (
    change_events_from_snapshot_diff,
    summarize_change_events,
)
from local_steward.models import (
    ChangeEventSummary,
    ChangeEventType,
    SnapshotDiff,
    SnapshotDiffSummary,
)
from local_steward.snapshot_diff import compute_snapshot_diff

from .test_snapshot_queries import snapshot_fixture


def _snapshot_with_entries(snapshot, entries, snapshot_id: str):
    return replace(snapshot, snapshot_id=snapshot_id, entries=tuple(entries))


def test_added_diff_item_becomes_file_created(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    created = snapshot.entries[-1]
    left = _snapshot_with_entries(snapshot, snapshot.entries[:-1], "left")
    right = _snapshot_with_entries(snapshot, snapshot.entries, "right")

    events = change_events_from_snapshot_diff(compute_snapshot_diff(left, right))

    event = next(event for event in events if event.relative_path == created.relative_path)
    assert event.event_type == ChangeEventType.FILE_CREATED
    assert event.left_entry is None and event.right_entry == created
    assert event.size_delta is None and event.hash_changed is None
    assert not event.metadata_changed


def test_removed_diff_item_becomes_file_deleted(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    removed = snapshot.entries[-1]
    left = _snapshot_with_entries(snapshot, snapshot.entries, "left")
    right = _snapshot_with_entries(snapshot, snapshot.entries[:-1], "right")

    events = change_events_from_snapshot_diff(compute_snapshot_diff(left, right))

    event = next(event for event in events if event.relative_path == removed.relative_path)
    assert event.event_type == ChangeEventType.FILE_DELETED
    assert event.left_entry == removed and event.right_entry is None
    assert event.size_delta is None and event.hash_changed is None
    assert not event.metadata_changed


def test_modified_diff_item_becomes_file_modified(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    original = snapshot.entries[0]
    changed = replace(
        original,
        size_bytes=(original.size_bytes or 0) + 9,
        mode=(original.mode or 0) + 1,
    )
    right = _snapshot_with_entries(snapshot, (changed, *snapshot.entries[1:]), "right")

    events = change_events_from_snapshot_diff(compute_snapshot_diff(snapshot, right))

    event = next(event for event in events if event.relative_path == original.relative_path)
    assert event.event_type == ChangeEventType.FILE_MODIFIED
    assert event.left_entry == original and event.right_entry == changed
    assert event.size_delta == 9 and event.hash_changed is None
    assert event.metadata_changed


def test_unchanged_and_empty_diff_produce_no_events(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    empty = SnapshotDiff("left", "right", (), SnapshotDiffSummary(0, 0, 0, 0, 0))

    assert change_events_from_snapshot_diff(compute_snapshot_diff(snapshot, snapshot)) == ()
    assert change_events_from_snapshot_diff(empty) == ()
    assert summarize_change_events(()) == ChangeEventSummary(0, 0, 0, 0)


def test_multiple_events_are_stably_sorted_and_summarized(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    removed, modified, unchanged = snapshot.entries[:3]
    changed = replace(modified, size_bytes=(modified.size_bytes or 0) + 1)
    created = replace(unchanged, relative_path="z-created.txt", entry_id="created")
    left = _snapshot_with_entries(snapshot, (unchanged, modified, removed), "left")
    right = _snapshot_with_entries(snapshot, (created, unchanged, changed), "right")

    events = change_events_from_snapshot_diff(compute_snapshot_diff(left, right))

    assert [event.relative_path for event in events] == sorted(
        event.relative_path for event in events
    )
    assert [event.event_type for event in events] == [
        ChangeEventType.FILE_DELETED,
        ChangeEventType.FILE_MODIFIED,
        ChangeEventType.FILE_CREATED,
    ]
    assert summarize_change_events(events) == ChangeEventSummary(1, 1, 1, 3)


def test_diff_item_order_does_not_change_events(tmp_path: Path) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    left = _snapshot_with_entries(snapshot, snapshot.entries[:-1], "left")
    right = _snapshot_with_entries(snapshot, snapshot.entries, "right")
    diff = compute_snapshot_diff(left, right)

    assert change_events_from_snapshot_diff(diff) == change_events_from_snapshot_diff(
        replace(diff, items=tuple(reversed(diff.items)))
    )


def test_change_semantics_does_not_access_live_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, snapshot = snapshot_fixture(tmp_path)
    diff = compute_snapshot_diff(snapshot, snapshot)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("live filesystem access is forbidden")

    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)

    assert change_events_from_snapshot_diff(diff) == ()


def test_change_semantics_does_not_mutate_persistent_state(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    database = config.paths.data_dir / "state.db"
    database_before = database.read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }

    change_events_from_snapshot_diff(compute_snapshot_diff(snapshot, snapshot))

    assert database.read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before
