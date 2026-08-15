"""Pure, shared Projection Entry fact extraction; never reads a filesystem."""

from local_steward.models import (
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemObjectType,
    PayloadObservationStatus,
    SnapshotEntryReference,
)

from .models import (
    EntryMetadataFacts,
    EntryObjectHintFacts,
    EntryPayloadFacts,
    EntrySizeFacts,
    EntrySizeState,
    EntrySourceSide,
    ExplicitEntryAnchor,
    PayloadFactState,
    ResultLocalReference,
    SelectionReason,
)
from .validation import normalize_reasons


Entry = FilesystemEntry | FilesystemEntryV2


def entry_reference(entry: Entry) -> SnapshotEntryReference:
    return SnapshotEntryReference(entry.snapshot_id, entry.scope_id, entry.relative_path)


def _payload(entry: Entry) -> EntryPayloadFacts:
    if not isinstance(entry, FilesystemEntryV2):
        return EntryPayloadFacts(PayloadFactState.UNSUPPORTED)
    observation = entry.payload_observation
    if observation.status in {PayloadObservationStatus.HASHED, PayloadObservationStatus.EMPTY_FILE_HASHED}:
        return EntryPayloadFacts(
            PayloadFactState.VERIFIED,
            observation.algorithm,
            observation.algorithm_version,
            observation.digest,
            observation.provenance,
            observation.reused_from_snapshot_id,
            observation.failure_code,
        )
    if entry.object_type != FilesystemObjectType.REGULAR_FILE:
        return EntryPayloadFacts(PayloadFactState.UNSUPPORTED)
    return EntryPayloadFacts(
        PayloadFactState.UNKNOWN,
        failure_code=observation.failure_code or observation.status.value,
    )


def extract_entry_anchor(
    entry: Entry,
    *,
    source_side: EntrySourceSide,
    reasons: tuple[SelectionReason, ...],
    result_references: tuple[ResultLocalReference, ...] = (),
    include_object_hint: bool = False,
) -> ExplicitEntryAnchor:
    """Extract the bounded model fact groups from one already-verified Entry."""
    metadata = EntryMetadataFacts(
        entry.observation_status,
        entry.mode,
        entry.uid,
        entry.gid,
        entry.mtime_ns,
        entry.ctime_ns,
        entry.birthtime_ns,
        entry.symlink_target_raw,
        entry.readable,
        entry.writable,
        entry.executable,
        entry.excluded,
        entry.error_code,
    )
    size = EntrySizeFacts(
        EntrySizeState.KNOWN if entry.size_bytes is not None else EntrySizeState.UNKNOWN,
        entry.size_bytes,
    )
    object_hint = None
    if include_object_hint and (entry.device_id is not None or entry.inode is not None):
        object_hint = EntryObjectHintFacts(entry.device_id, entry.inode, entry.link_count)
    return ExplicitEntryAnchor(
        entry_reference(entry),
        source_side,
        entry.object_type,
        metadata,
        size,
        _payload(entry),
        object_hint,
        normalize_reasons(reasons),
        tuple(sorted(result_references, key=lambda item: (item.namespace.result_kind.value, item.result_local_id))),
    )
