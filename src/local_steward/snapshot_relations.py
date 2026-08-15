"""Deterministic, on-demand relations between two verified Snapshot facts."""

from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256

from .errors import RelationError
from .evidence import canonical_json
from .models import (
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemObjectType,
    FilesystemObservationStatus,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    PayloadObservationProvenance,
    PayloadObservationStatus,
    RelationAmbiguityGroup,
    RelationCertainty,
    RelationItem,
    RelationKind,
    RelationSet,
    SnapshotEntryReference,
    StewardConfig,
)
from .snapshots import get_snapshot, list_snapshots, verify_snapshot


RELATION_SCHEMA_VERSION = 1
RELATION_ALGORITHM = "cross_snapshot_relation"
RELATION_ALGORITHM_VERSION = 1
RELATION_DIGEST_DOMAIN = "local_steward.cross_snapshot_relation.v1"


def _reference_sort_key(reference: SnapshotEntryReference) -> tuple[str, str, bytes]:
    return (
        reference.snapshot_id,
        reference.scope_id,
        reference.relative_path.encode("utf-8", "surrogateescape"),
    )


def _entry_sort_key(entry: FilesystemEntry | FilesystemEntryV2) -> tuple[str, bytes]:
    return (entry.scope_id, entry.relative_path.encode("utf-8", "surrogateescape"))


def _reference(entry: FilesystemEntry | FilesystemEntryV2) -> SnapshotEntryReference:
    return SnapshotEntryReference(entry.snapshot_id, entry.scope_id, entry.relative_path)


def _reference_data(reference: SnapshotEntryReference) -> dict[str, str]:
    return {
        "snapshot_id": reference.snapshot_id,
        "scope_id": reference.scope_id,
        "relative_path": reference.relative_path,
    }


def _ordered_references(
    entries: Iterable[FilesystemEntry | FilesystemEntryV2],
) -> tuple[SnapshotEntryReference, ...]:
    return tuple(sorted((_reference(entry) for entry in entries), key=_reference_sort_key))


def _item_content(
    kind: RelationKind,
    certainty: RelationCertainty,
    reason_codes: tuple[str, ...],
    ambiguity_group_id: str | None,
    source_entries: tuple[SnapshotEntryReference, ...],
    target_entries: tuple[SnapshotEntryReference, ...],
) -> dict[str, object]:
    return {
        "kind": kind.value,
        "certainty": certainty.value,
        "reason_codes": list(reason_codes),
        "ambiguity_group_id": ambiguity_group_id,
        "source_entries": [_reference_data(item) for item in source_entries],
        "target_entries": [_reference_data(item) for item in target_entries],
    }


def _ambiguity_group_id(
    source_entries: tuple[SnapshotEntryReference, ...],
    target_entries: tuple[SnapshotEntryReference, ...],
) -> str:
    return sha256(
        canonical_json(
            {
                "source_entries": [_reference_data(item) for item in source_entries],
                "target_entries": [_reference_data(item) for item in target_entries],
            }
        )
    ).hexdigest()


def _relation_item(
    kind: RelationKind,
    certainty: RelationCertainty,
    source_entries: tuple[SnapshotEntryReference, ...],
    target_entries: tuple[SnapshotEntryReference, ...],
    ambiguity_group_id: str | None = None,
) -> RelationItem:
    # The protocol intentionally defines no secondary reason-code taxonomy.
    # Reusing the frozen relation kind supplies a stable machine criterion
    # without inventing a parallel classification layer.
    reason_codes = (kind.value,)
    content = _item_content(
        kind,
        certainty,
        reason_codes,
        ambiguity_group_id,
        source_entries,
        target_entries,
    )
    return RelationItem(
        sha256(canonical_json(content)).hexdigest(),
        kind,
        certainty,
        reason_codes,
        ambiguity_group_id,
        source_entries,
        target_entries,
    )


def _item_sort_key(item: RelationItem) -> tuple[object, ...]:
    return (
        item.kind.value,
        item.certainty.value,
        tuple(_reference_sort_key(reference) for reference in item.source_entries),
        tuple(_reference_sort_key(reference) for reference in item.target_entries),
        item.ambiguity_group_id or "",
    )


def canonical_relation_set(relation_set: RelationSet) -> bytes:
    """Return the frozen canonical bytes excluding the derived set digest."""
    return canonical_json(
        {
            "domain": RELATION_DIGEST_DOMAIN,
            "relation_schema_version": relation_set.relation_schema_version,
            "algorithm": relation_set.algorithm,
            "algorithm_version": relation_set.algorithm_version,
            "base_snapshot_id": relation_set.base_snapshot_id,
            "target_snapshot_id": relation_set.target_snapshot_id,
            "relations": [
                {
                    "relation_id": item.relation_id,
                    **_item_content(
                        item.kind,
                        item.certainty,
                        item.reason_codes,
                        item.ambiguity_group_id,
                        item.source_entries,
                        item.target_entries,
                    ),
                }
                for item in relation_set.relations
            ],
            "ambiguity_groups": [
                {
                    "ambiguity_group_id": group.ambiguity_group_id,
                    "source_entries": [_reference_data(item) for item in group.source_entries],
                    "target_entries": [_reference_data(item) for item in group.target_entries],
                }
                for group in relation_set.ambiguity_groups
            ],
        }
    )


def _relation_set(
    base_snapshot_id: str,
    target_snapshot_id: str,
    relations: Iterable[RelationItem],
    groups: Iterable[RelationAmbiguityGroup],
) -> RelationSet:
    ordered_relations = tuple(sorted(relations, key=_item_sort_key))
    ordered_groups = tuple(
        sorted(
            groups,
            key=lambda group: (
                tuple(_reference_sort_key(item) for item in group.source_entries),
                tuple(_reference_sort_key(item) for item in group.target_entries),
                group.ambiguity_group_id,
            ),
        )
    )
    provisional = RelationSet(
        RELATION_SCHEMA_VERSION,
        RELATION_ALGORITHM,
        RELATION_ALGORITHM_VERSION,
        base_snapshot_id,
        target_snapshot_id,
        ordered_relations,
        ordered_groups,
        "",
    )
    return RelationSet(
        RELATION_SCHEMA_VERSION,
        RELATION_ALGORITHM,
        RELATION_ALGORITHM_VERSION,
        base_snapshot_id,
        target_snapshot_id,
        ordered_relations,
        ordered_groups,
        sha256(canonical_relation_set(provisional)).hexdigest(),
    )


def _entries_by_location(
    entries: Iterable[FilesystemEntry | FilesystemEntryV2], snapshot_id: str
) -> dict[tuple[str, str], FilesystemEntry | FilesystemEntryV2]:
    result: dict[tuple[str, str], FilesystemEntry | FilesystemEntryV2] = {}
    for entry in entries:
        if entry.snapshot_id != snapshot_id:
            raise RelationError("RELATION_INVALID: entry snapshot identity does not match input")
        key = (entry.scope_id, entry.relative_path)
        if key in result:
            raise RelationError("RELATION_INVALID: duplicate scoped location in Snapshot")
        result[key] = entry
    return result


def _successful_payload_digest(
    entry: FilesystemEntry | FilesystemEntryV2, *, reused_payloads_verified: bool
) -> str | None:
    if not isinstance(entry, FilesystemEntryV2):
        return None
    payload = entry.payload_observation
    if payload.status not in {
        PayloadObservationStatus.HASHED,
        PayloadObservationStatus.EMPTY_FILE_HASHED,
    }:
        return None
    if payload.algorithm != "sha256" or payload.algorithm_version != 1:
        return None
    if not isinstance(payload.digest, str) or len(payload.digest) != 64:
        return None
    if any(character not in "0123456789abcdef" for character in payload.digest):
        return None
    if payload.bytes_hashed != entry.size_bytes:
        return None
    if payload.provenance == PayloadObservationProvenance.DIRECT_READ:
        return payload.digest
    if (
        payload.provenance == PayloadObservationProvenance.REUSED_FROM_VERIFIED_SNAPSHOT
        and reused_payloads_verified
        and payload.reused_from_snapshot_id is not None
    ):
        return payload.digest
    return None


def _object_hint_equal(
    base: FilesystemEntry | FilesystemEntryV2,
    target: FilesystemEntry | FilesystemEntryV2,
) -> bool:
    if (
        base.device_id is None
        or base.inode is None
        or target.device_id is None
        or target.inode is None
    ):
        return False
    if (base.device_id, base.inode) != (target.device_id, target.inode):
        return False
    return not (
        base.birthtime_ns is not None
        and target.birthtime_ns is not None
        and base.birthtime_ns != target.birthtime_ns
    )


def _metadata_changed(
    base: FilesystemEntry | FilesystemEntryV2,
    target: FilesystemEntry | FilesystemEntryV2,
) -> bool:
    fields = (
        "mode",
        "uid",
        "gid",
        "size_bytes",
        "mtime_ns",
        "ctime_ns",
        "link_count",
        "readable",
        "writable",
        "executable",
        "observation_status",
        "error_code",
        "error_message",
        "excluded",
    )
    return any(getattr(base, field) != getattr(target, field) for field in fields)


def _same_location_relation(
    base: FilesystemEntry | FilesystemEntryV2,
    target: FilesystemEntry | FilesystemEntryV2,
    *,
    reused_payloads_verified: bool,
) -> RelationItem | None:
    if (
        base.observation_status != FilesystemObservationStatus.OBSERVED
        or target.observation_status != FilesystemObservationStatus.OBSERVED
    ):
        return None
    source_entries = (_reference(base),)
    target_entries = (_reference(target),)
    if base.object_type != target.object_type:
        return _relation_item(
            RelationKind.SAME_LOCATION_TYPE_CHANGED,
            RelationCertainty.FACT,
            source_entries,
            target_entries,
        )
    if (
        base.object_type == FilesystemObjectType.SYMLINK
        and base.symlink_target_raw != target.symlink_target_raw
    ):
        return _relation_item(
            RelationKind.SAME_LOCATION_SYMLINK_TARGET_CHANGED,
            RelationCertainty.FACT,
            source_entries,
            target_entries,
        )
    if base.object_type == FilesystemObjectType.REGULAR_FILE and not _object_hint_equal(base, target):
        return _relation_item(
            RelationKind.SAME_LOCATION_OBJECT_HINT_CHANGED,
            RelationCertainty.FACT,
            source_entries,
            target_entries,
        )
    base_payload = _successful_payload_digest(
        base, reused_payloads_verified=reused_payloads_verified
    )
    target_payload = _successful_payload_digest(
        target, reused_payloads_verified=reused_payloads_verified
    )
    if base.object_type == FilesystemObjectType.REGULAR_FILE:
        if base_payload is None or target_payload is None:
            return _relation_item(
                RelationKind.SAME_LOCATION_CONTENT_UNKNOWN,
                RelationCertainty.UNKNOWN,
                source_entries,
                target_entries,
            )
        if base_payload != target_payload:
            return _relation_item(
                RelationKind.SAME_LOCATION_CONTENT_CHANGED,
                RelationCertainty.FACT,
                source_entries,
                target_entries,
            )
    if _metadata_changed(base, target):
        return _relation_item(
            RelationKind.SAME_LOCATION_METADATA_CHANGED,
            RelationCertainty.FACT,
            source_entries,
            target_entries,
        )
    return _relation_item(
        RelationKind.SAME_LOCATION_CONTINUITY,
        RelationCertainty.FACT,
        source_entries,
        target_entries,
    )


def _transition_eligible(entry: FilesystemEntry | FilesystemEntryV2) -> bool:
    return (
        entry.object_type == FilesystemObjectType.REGULAR_FILE
        and entry.observation_status == FilesystemObservationStatus.OBSERVED
        and not entry.excluded
        and entry.device_id is not None
        and entry.inode is not None
        and entry.birthtime_ns is not None
    )


def _transition_key(entry: FilesystemEntry | FilesystemEntryV2) -> tuple[int, int, int]:
    assert entry.device_id is not None
    assert entry.inode is not None
    assert entry.birthtime_ns is not None
    return (entry.device_id, entry.inode, entry.birthtime_ns)


def _transition_relations(
    base_entries: Iterable[FilesystemEntry | FilesystemEntryV2],
    target_entries: Iterable[FilesystemEntry | FilesystemEntryV2],
    base_snapshot_entries: Iterable[FilesystemEntry | FilesystemEntryV2],
    target_snapshot_entries: Iterable[FilesystemEntry | FilesystemEntryV2],
    *,
    reused_payloads_verified: bool,
) -> tuple[tuple[RelationItem, ...], tuple[RelationAmbiguityGroup, ...]]:
    base_by_hint: dict[tuple[int, int, int], list[FilesystemEntry | FilesystemEntryV2]] = {}
    target_by_hint: dict[tuple[int, int, int], list[FilesystemEntry | FilesystemEntryV2]] = {}
    for entry in base_entries:
        if _transition_eligible(entry):
            base_by_hint.setdefault(_transition_key(entry), []).append(entry)
    for entry in target_entries:
        if _transition_eligible(entry):
            target_by_hint.setdefault(_transition_key(entry), []).append(entry)
    base_aliases: dict[tuple[int, int], list[FilesystemEntry | FilesystemEntryV2]] = {}
    target_aliases: dict[tuple[int, int], list[FilesystemEntry | FilesystemEntryV2]] = {}
    for entry in base_snapshot_entries:
        if entry.device_id is not None and entry.inode is not None:
            base_aliases.setdefault((entry.device_id, entry.inode), []).append(entry)
    for entry in target_snapshot_entries:
        if entry.device_id is not None and entry.inode is not None:
            target_aliases.setdefault((entry.device_id, entry.inode), []).append(entry)
    relations: list[RelationItem] = []
    groups: list[RelationAmbiguityGroup] = []
    for hint in sorted(set(base_by_hint) & set(target_by_hint)):
        sources = tuple(sorted(base_by_hint[hint], key=_entry_sort_key))
        targets = tuple(sorted(target_by_hint[hint], key=_entry_sort_key))
        source_references = _ordered_references(sources)
        target_references = _ordered_references(targets)
        source_digests = {
            _successful_payload_digest(item, reused_payloads_verified=reused_payloads_verified)
            for item in sources
        }
        target_digests = {
            _successful_payload_digest(item, reused_payloads_verified=reused_payloads_verified)
            for item in targets
        }
        if None in source_digests or None in target_digests:
            continue
        all_digests = source_digests | target_digests
        source_aliases = tuple(
            sorted(base_aliases[(hint[0], hint[1])], key=_entry_sort_key)
        )
        target_aliases_for_hint = tuple(
            sorted(target_aliases[(hint[0], hint[1])], key=_entry_sort_key)
        )
        aliases = len(source_aliases) > 1 or len(target_aliases_for_hint) > 1
        if aliases or len(all_digests) != 1:
            component_sources = _ordered_references(source_aliases) if aliases else source_references
            component_targets = (
                _ordered_references(target_aliases_for_hint) if aliases else target_references
            )
            group_id = _ambiguity_group_id(component_sources, component_targets)
            groups.append(
                RelationAmbiguityGroup(group_id, component_sources, component_targets)
            )
            relations.append(
                _relation_item(
                    RelationKind.AMBIGUOUS_LOCATION_TRANSITION,
                    RelationCertainty.AMBIGUOUS,
                    component_sources,
                    component_targets,
                    group_id,
                )
            )
            continue
        source = sources[0]
        target = targets[0]
        kind = (
            RelationKind.RENAME_CANDIDATE
            if source.scope_id == target.scope_id
            else RelationKind.CROSS_SCOPE_TRANSITION_CANDIDATE
        )
        relations.append(
            _relation_item(
                kind,
                RelationCertainty.CANDIDATE,
                source_references,
                target_references,
            )
        )
    return tuple(relations), tuple(groups)


def _validate_pair(
    base: FilesystemSnapshot | FilesystemSnapshotV2,
    target: FilesystemSnapshot | FilesystemSnapshotV2,
) -> None:
    if base.snapshot_id == target.snapshot_id:
        raise RelationError("RELATION_INVALID: base and target Snapshot IDs must be distinct")
    if base.completed_at > target.started_at:
        raise RelationError("RELATION_INVALID: base Snapshot must not follow target Snapshot")


def compute_snapshot_relations(
    base: FilesystemSnapshot | FilesystemSnapshotV2,
    target: FilesystemSnapshot | FilesystemSnapshotV2,
    *,
    adjacent_valid_snapshots: bool = True,
    reused_payloads_verified: bool = False,
) -> RelationSet:
    """Compute in-memory relations from a directional pair of validated facts.

    This pure adapter intentionally does not validate repositories.  Callers
    using persisted facts must use :func:`compute_verified_snapshot_relations`.
    """
    _validate_pair(base, target)
    base_locations = _entries_by_location(base.entries, base.snapshot_id)
    target_locations = _entries_by_location(target.entries, target.snapshot_id)
    relations: list[RelationItem] = []
    for location in sorted(set(base_locations) & set(target_locations), key=lambda item: (item[0], item[1].encode("utf-8", "surrogateescape"))):
        relation = _same_location_relation(
            base_locations[location],
            target_locations[location],
            reused_payloads_verified=reused_payloads_verified,
        )
        if relation is not None:
            relations.append(relation)
    groups: tuple[RelationAmbiguityGroup, ...] = ()
    if adjacent_valid_snapshots:
        transitions, groups = _transition_relations(
            (entry for location, entry in base_locations.items() if location not in target_locations),
            (entry for location, entry in target_locations.items() if location not in base_locations),
            base_locations.values(),
            target_locations.values(),
            reused_payloads_verified=reused_payloads_verified,
        )
        relations.extend(transitions)
    return _relation_set(base.snapshot_id, target.snapshot_id, relations, groups)


def _are_adjacent_valid_snapshots(
    config: StewardConfig, base_snapshot_id: str, target_snapshot_id: str
) -> bool:
    summaries = list_snapshots(config, limit=None)
    valid = [
        item
        for item in summaries
        if verify_snapshot(config, item.snapshot_id).status == "VALID"
    ]
    valid.sort(key=lambda item: (item.created_at, item.snapshot_id))
    pairs = zip(valid, valid[1:])
    return any(
        base.snapshot_id == base_snapshot_id and target.snapshot_id == target_snapshot_id
        for base, target in pairs
    )


def compute_verified_snapshot_relations(
    config: StewardConfig, base_snapshot_id: str, target_snapshot_id: str
) -> RelationSet:
    """Load, repository-verify, and relate two explicit persisted Snapshots."""
    if base_snapshot_id == target_snapshot_id:
        raise RelationError("RELATION_INVALID: base and target Snapshot IDs must be distinct")
    try:
        base_verification = verify_snapshot(config, base_snapshot_id)
        target_verification = verify_snapshot(config, target_snapshot_id)
    except Exception as error:
        raise RelationError("RELATION_INVALID: both Snapshot IDs must be available") from error
    invalid = tuple(
        snapshot_id
        for snapshot_id, verification in (
            (base_snapshot_id, base_verification),
            (target_snapshot_id, target_verification),
        )
        if verification.status != "VALID"
    )
    if invalid:
        raise RelationError(
            "RELATION_INVALID: relation requires VALID Snapshot Evidence: " + ", ".join(invalid)
        )
    try:
        base = get_snapshot(config, base_snapshot_id)
        target = get_snapshot(config, target_snapshot_id)
    except Exception as error:
        raise RelationError("RELATION_INVALID: both Snapshot IDs must be available") from error
    return compute_snapshot_relations(
        base,
        target,
        adjacent_valid_snapshots=_are_adjacent_valid_snapshots(
            config, base_snapshot_id, target_snapshot_id
        ),
        reused_payloads_verified=True,
    )
