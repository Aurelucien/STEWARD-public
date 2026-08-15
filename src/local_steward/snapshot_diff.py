"""Deterministic, read-only differences between validated Snapshot facts."""

from collections.abc import Iterable

from .errors import DiffError
from .models import (
    FilesystemEntry,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    SnapshotDiff,
    SnapshotDiffChangeType,
    SnapshotDiffItem,
    SnapshotDiffSummary,
    StewardConfig,
)
from .snapshots import get_snapshot, snapshot_v2_stat_view, verify_snapshot


# Snapshot and Entry IDs identify a particular persisted fact.  They necessarily
# differ between two otherwise equivalent Snapshots, so they are not observations
# to compare.  (scope_id, relative_path) is the matching key; every other
# recorded Entry fact is compared in this fixed order.
_COMPARABLE_ENTRY_FIELDS = (
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
)


def _location_key(entry: FilesystemEntry) -> tuple[str, str]:
    return (entry.scope_id, entry.relative_path)


def _location_sort_key(location: tuple[str, str]) -> tuple[str, bytes]:
    """Use the Snapshot Evidence ordering for scoped paths with surrogate escapes."""
    return (location[0], location[1].encode("utf-8", "surrogateescape"))


def _entries_by_location(
    entries: Iterable[FilesystemEntry], snapshot_id: str
) -> dict[tuple[str, str], FilesystemEntry]:
    by_location: dict[tuple[str, str], FilesystemEntry] = {}
    for entry in entries:
        location = _location_key(entry)
        if location in by_location:
            raise DiffError(
                "snapshot "
                f"{snapshot_id} has duplicate scoped location: {entry.scope_id}:{entry.relative_path}"
            )
        by_location[location] = entry
    return by_location


def _changed_fields(left: FilesystemEntry, right: FilesystemEntry) -> tuple[str, ...]:
    return tuple(
        field
        for field in _COMPARABLE_ENTRY_FIELDS
        if getattr(left, field) != getattr(right, field)
    )


def compute_snapshot_diff(
    left_snapshot: FilesystemSnapshot, right_snapshot: FilesystemSnapshot
) -> SnapshotDiff:
    """Compare Snapshot Entry facts only, without filesystem or storage access."""
    left_entries = _entries_by_location(left_snapshot.entries, left_snapshot.snapshot_id)
    right_entries = _entries_by_location(right_snapshot.entries, right_snapshot.snapshot_id)
    items: list[SnapshotDiffItem] = []

    for scope_id, relative_path in sorted(
        set(left_entries) | set(right_entries), key=_location_sort_key
    ):
        location = (scope_id, relative_path)
        left_entry = left_entries.get(location)
        right_entry = right_entries.get(location)
        if left_entry is None:
            change_type = SnapshotDiffChangeType.ADDED
            changed_fields: tuple[str, ...] = ()
        elif right_entry is None:
            change_type = SnapshotDiffChangeType.REMOVED
            changed_fields = ()
        else:
            changed_fields = _changed_fields(left_entry, right_entry)
            change_type = (
                SnapshotDiffChangeType.MODIFIED
                if changed_fields
                else SnapshotDiffChangeType.UNCHANGED
            )
        items.append(
            SnapshotDiffItem(
                scope_id,
                relative_path,
                change_type,
                changed_fields,
                left_entry,
                right_entry,
            )
        )

    item_values = tuple(items)
    return SnapshotDiff(
        left_snapshot.snapshot_id,
        right_snapshot.snapshot_id,
        item_values,
        SnapshotDiffSummary(
            added_count=sum(
                item.change_type == SnapshotDiffChangeType.ADDED for item in item_values
            ),
            removed_count=sum(
                item.change_type == SnapshotDiffChangeType.REMOVED for item in item_values
            ),
            modified_count=sum(
                item.change_type == SnapshotDiffChangeType.MODIFIED for item in item_values
            ),
            unchanged_count=sum(
                item.change_type == SnapshotDiffChangeType.UNCHANGED for item in item_values
            ),
            item_count=len(item_values),
        ),
    )


def compute_verified_snapshot_diff(
    config: StewardConfig, left_snapshot_id: str, right_snapshot_id: str
) -> SnapshotDiff:
    """Load and compare only Snapshot facts that pass the existing verifier."""
    invalid_ids = tuple(
        snapshot_id
        for snapshot_id in (left_snapshot_id, right_snapshot_id)
        if verify_snapshot(config, snapshot_id).status != "VALID"
    )
    if invalid_ids:
        raise DiffError(
            "snapshot diff requires VALID Snapshot Evidence: " + ", ".join(invalid_ids)
        )
    left = get_snapshot(config, left_snapshot_id)
    right = get_snapshot(config, right_snapshot_id)
    return compute_snapshot_diff(
        snapshot_v2_stat_view(left) if isinstance(left, FilesystemSnapshotV2) else left,
        snapshot_v2_stat_view(right) if isinstance(right, FilesystemSnapshotV2) else right,
    )
