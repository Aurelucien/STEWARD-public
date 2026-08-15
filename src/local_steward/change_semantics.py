"""Read-only semantic projection from SnapshotDiff facts to file events."""

from collections.abc import Iterable

from .models import (
    ChangeEvent,
    ChangeEventSummary,
    ChangeEventType,
    SnapshotDiff,
    SnapshotDiffChangeType,
    SnapshotDiffItem,
)


# Snapshot Entry v0.1 records no content hash.  hash_changed is therefore None
# rather than a misleading false value until a future Snapshot fact records one.
_METADATA_FIELDS = frozenset(
    {
        "object_type",
        "device_id",
        "inode",
        "mode",
        "uid",
        "gid",
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
    }
)


def _location_sort_key(event: ChangeEvent) -> tuple[str, bytes]:
    return (event.scope_id, event.relative_path.encode("utf-8", "surrogateescape"))


def _size_delta(item: SnapshotDiffItem) -> int | None:
    if item.left_entry is None or item.right_entry is None:
        return None
    if item.left_entry.size_bytes is None or item.right_entry.size_bytes is None:
        return None
    return item.right_entry.size_bytes - item.left_entry.size_bytes


def _event(item: SnapshotDiffItem) -> ChangeEvent | None:
    event_type = {
        SnapshotDiffChangeType.ADDED: ChangeEventType.FILE_CREATED,
        SnapshotDiffChangeType.REMOVED: ChangeEventType.FILE_DELETED,
        SnapshotDiffChangeType.MODIFIED: ChangeEventType.FILE_MODIFIED,
    }.get(item.change_type)
    if event_type is None:
        return None
    return ChangeEvent(
        item.scope_id,
        item.relative_path,
        event_type,
        item.left_entry,
        item.right_entry,
        _size_delta(item),
        None,
        bool(_METADATA_FIELDS.intersection(item.changed_fields)),
    )


def change_events_from_snapshot_diff(snapshot_diff: SnapshotDiff) -> tuple[ChangeEvent, ...]:
    """Map SnapshotDiff facts without filesystem, process, or storage access."""
    events = tuple(event for item in snapshot_diff.items if (event := _event(item)) is not None)
    return tuple(sorted(events, key=_location_sort_key))


def summarize_change_events(events: Iterable[ChangeEvent]) -> ChangeEventSummary:
    """Return deterministic counts for an already-derived event collection."""
    values = tuple(events)
    return ChangeEventSummary(
        created_count=sum(event.event_type == ChangeEventType.FILE_CREATED for event in values),
        deleted_count=sum(event.event_type == ChangeEventType.FILE_DELETED for event in values),
        modified_count=sum(event.event_type == ChangeEventType.FILE_MODIFIED for event in values),
        event_count=len(values),
    )
