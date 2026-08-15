"""Pure protocol coverage for cross-Snapshot relation analysis."""

from dataclasses import replace
from hashlib import sha256

import pytest

from local_steward.errors import RelationError
from local_steward.models import (
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
    RelationCertainty,
    RelationKind,
    ScanBudget,
    SnapshotConsistency,
    SnapshotStatus,
)
from local_steward.snapshot_relations import (
    canonical_relation_set,
    compute_snapshot_relations,
)


def _entry(
    snapshot_id: str,
    path: str,
    *,
    scope_id: str = "managed",
    object_type: FilesystemObjectType = FilesystemObjectType.REGULAR_FILE,
    device_id: int | None = 1,
    inode: int | None = 10,
    birthtime_ns: int | None = 5,
    digest: str | None = None,
    link_count: int = 1,
    mode: int = 0o644,
    symlink_target_raw: str | None = None,
) -> FilesystemEntry:
    return FilesystemEntry(
        f"{snapshot_id}:{scope_id}:{path}",
        snapshot_id,
        scope_id,
        path,
        object_type,
        device_id,
        inode,
        mode,
        1,
        2,
        10,
        20,
        30,
        birthtime_ns,
        link_count,
        symlink_target_raw,
        True,
        False,
        False,
        FilesystemObservationStatus.OBSERVED,
        None,
        None,
        False,
    )


def _v2(entry: FilesystemEntry, digest: str | None, *, reused: bool = False) -> FilesystemEntryV2:
    success = digest is not None
    payload = PayloadObservation(
        PayloadObservationStatus.HASHED if success else PayloadObservationStatus.UNSUPPORTED,
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
        None,
        None,
    )
    return FilesystemEntryV2(*entry.__getstate__(), None, payload)


def _snapshot(
    snapshot_id: str,
    entries: tuple[FilesystemEntry | FilesystemEntryV2, ...],
    *,
    v2: bool = False,
    started_at: str = "2026-01-01T00:00:00.000000Z",
    completed_at: str = "2026-01-01T00:00:01.000000Z",
) -> FilesystemSnapshot | FilesystemSnapshotV2:
    shared = (
        snapshot_id,
        "run",
        completed_at,
        started_at,
        completed_at,
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
        return FilesystemSnapshot(*shared, "entries", "snapshot", None, None, entries)
    return FilesystemSnapshotV2(
        2,
        *shared,
        PayloadHashPolicy("sha256", 1, None, None, None, 4096, False, False),
        0,
        0,
        (),
        "entries",
        "snapshot",
        None,
        None,
        entries,  # type: ignore[arg-type]
    )


def _pair(
    base_entries: tuple[FilesystemEntry | FilesystemEntryV2, ...],
    target_entries: tuple[FilesystemEntry | FilesystemEntryV2, ...],
    *,
    v2: bool = False,
) -> tuple[FilesystemSnapshot | FilesystemSnapshotV2, FilesystemSnapshot | FilesystemSnapshotV2]:
    base = _snapshot("base", base_entries, v2=v2)
    target = _snapshot(
        "target",
        target_entries,
        v2=v2,
        started_at="2026-01-01T00:00:01.000000Z",
        completed_at="2026-01-01T00:00:02.000000Z",
    )
    return base, target


def _only_relation(*args: object, **kwargs: object):
    relation_set = compute_snapshot_relations(*args, **kwargs)  # type: ignore[arg-type]
    assert len(relation_set.relations) == 1
    return relation_set.relations[0]


def test_v1_same_location_is_content_unknown() -> None:
    base, target = _pair((_entry("base", "a"),), (_entry("target", "a"),))
    relation = _only_relation(base, target)
    assert relation.kind == RelationKind.SAME_LOCATION_CONTENT_UNKNOWN
    assert relation.certainty == RelationCertainty.UNKNOWN


def test_same_location_v2_taxonomy() -> None:
    digest_a = "a" * 64
    digest_b = "b" * 64
    base_entry = _entry("base", "a")
    target_entry = _entry("target", "a")
    base, target = _pair((_v2(base_entry, digest_a),), (_v2(target_entry, digest_a),), v2=True)
    assert _only_relation(base, target).kind == RelationKind.SAME_LOCATION_CONTINUITY

    changed_mode = replace(target_entry, mode=0o600)
    base, target = _pair((_v2(base_entry, digest_a),), (_v2(changed_mode, digest_a),), v2=True)
    assert _only_relation(base, target).kind == RelationKind.SAME_LOCATION_METADATA_CHANGED

    base, target = _pair((_v2(base_entry, digest_a),), (_v2(target_entry, digest_b),), v2=True)
    assert _only_relation(base, target).kind == RelationKind.SAME_LOCATION_CONTENT_CHANGED

    changed_hint = replace(target_entry, inode=11)
    base, target = _pair((_v2(base_entry, digest_a),), (_v2(changed_hint, digest_a),), v2=True)
    assert _only_relation(base, target).kind == RelationKind.SAME_LOCATION_OBJECT_HINT_CHANGED


def test_type_and_symlink_changes_are_direct_facts() -> None:
    base, target = _pair(
        (_entry("base", "a"),),
        (_entry("target", "a", object_type=FilesystemObjectType.DIRECTORY),),
    )
    assert _only_relation(base, target).kind == RelationKind.SAME_LOCATION_TYPE_CHANGED
    base, target = _pair(
        (_entry("base", "a", object_type=FilesystemObjectType.SYMLINK, symlink_target_raw="one"),),
        (_entry("target", "a", object_type=FilesystemObjectType.SYMLINK, symlink_target_raw="two"),),
    )
    assert _only_relation(base, target).kind == RelationKind.SAME_LOCATION_SYMLINK_TARGET_CHANGED


def test_unique_transition_candidate_and_cross_scope_candidate() -> None:
    digest = "a" * 64
    base_entry = _v2(_entry("base", "old"), digest)
    target_entry = _v2(_entry("target", "new"), digest)
    base, target = _pair((base_entry,), (target_entry,), v2=True)
    assert _only_relation(base, target).kind == RelationKind.RENAME_CANDIDATE

    target_cross = _v2(_entry("target", "new", scope_id="reference"), digest)
    base, target = _pair((base_entry,), (target_cross,), v2=True)
    assert _only_relation(base, target).kind == RelationKind.CROSS_SCOPE_TRANSITION_CANDIDATE


def test_identical_payload_without_matching_object_hint_is_not_a_transition() -> None:
    digest = "a" * 64
    base, target = _pair(
        (_v2(_entry("base", "old", inode=10), digest),),
        (_v2(_entry("target", "new", inode=11), digest),),
        v2=True,
    )
    assert compute_snapshot_relations(base, target).relations == ()


def test_hard_link_component_is_ambiguous_and_never_greedily_paired() -> None:
    digest = "a" * 64
    base, target = _pair(
        (
            _v2(_entry("base", "old-a", link_count=2), digest),
            _v2(_entry("base", "old-b", link_count=2), digest),
        ),
        (_v2(_entry("target", "new", link_count=2), digest),),
        v2=True,
    )
    relation_set = compute_snapshot_relations(base, target)
    relation = _only_relation(base, target)
    assert relation.kind == RelationKind.AMBIGUOUS_LOCATION_TRANSITION
    assert relation.certainty == RelationCertainty.AMBIGUOUS
    assert len(relation.source_entries) == 2
    assert relation.ambiguity_group_id == relation_set.ambiguity_groups[0].ambiguity_group_id


def test_hard_link_alias_outside_added_removed_sets_still_blocks_a_candidate() -> None:
    digest = "a" * 64
    base, target = _pair(
        (
            _v2(_entry("base", "kept", link_count=2), digest),
            _v2(_entry("base", "old", link_count=2), digest),
        ),
        (
            _v2(_entry("target", "kept", link_count=2), digest),
            _v2(_entry("target", "new", link_count=2), digest),
        ),
        v2=True,
    )
    relation_set = compute_snapshot_relations(base, target)
    ambiguous = next(
        relation
        for relation in relation_set.relations
        if relation.kind == RelationKind.AMBIGUOUS_LOCATION_TRANSITION
    )
    assert [item.relative_path for item in ambiguous.source_entries] == ["kept", "old"]
    assert [item.relative_path for item in ambiguous.target_entries] == ["kept", "new"]
    assert not any(item.kind == RelationKind.RENAME_CANDIDATE for item in relation_set.relations)


def test_conflicting_payload_for_same_object_hint_is_ambiguous() -> None:
    base, target = _pair(
        (_v2(_entry("base", "old"), "a" * 64),),
        (_v2(_entry("target", "new"), "b" * 64),),
        v2=True,
    )
    assert _only_relation(base, target).kind == RelationKind.AMBIGUOUS_LOCATION_TRANSITION


def test_non_adjacent_pairs_retain_direct_facts_but_not_transitions() -> None:
    digest = "a" * 64
    base, target = _pair(
        (_v2(_entry("base", "old"), digest),),
        (_v2(_entry("target", "new"), digest),),
        v2=True,
    )
    assert compute_snapshot_relations(base, target, adjacent_valid_snapshots=False).relations == ()


def test_reused_payload_requires_repository_aware_validation() -> None:
    digest = "a" * 64
    base, target = _pair(
        (_v2(_entry("base", "a"), digest, reused=True),),
        (_v2(_entry("target", "a"), digest, reused=True),),
        v2=True,
    )
    assert _only_relation(base, target).kind == RelationKind.SAME_LOCATION_CONTENT_UNKNOWN
    assert _only_relation(base, target, reused_payloads_verified=True).kind == RelationKind.SAME_LOCATION_CONTINUITY


def test_direction_and_same_snapshot_are_rejected() -> None:
    base, target = _pair((_entry("base", "a"),), (_entry("target", "a"),))
    with pytest.raises(RelationError, match="RELATION_INVALID"):
        compute_snapshot_relations(base, base)
    with pytest.raises(RelationError, match="RELATION_INVALID"):
        compute_snapshot_relations(target, base)


def test_canonical_bytes_and_digest_do_not_depend_on_entry_order() -> None:
    digest = "a" * 64
    first_base, first_target = _pair(
        (
            _v2(_entry("base", "b"), digest),
            _v2(_entry("base", "a", inode=11), digest),
        ),
        (
            _v2(_entry("target", "d"), digest),
            _v2(_entry("target", "c", inode=11), digest),
        ),
        v2=True,
    )
    second_base = replace(first_base, entries=tuple(reversed(first_base.entries)))
    second_target = replace(first_target, entries=tuple(reversed(first_target.entries)))
    first = compute_snapshot_relations(first_base, first_target)
    second = compute_snapshot_relations(second_base, second_target)
    assert canonical_relation_set(first) == canonical_relation_set(second)
    assert first.relation_set_digest == second.relation_set_digest
    altered = replace(first, relations=tuple(reversed(first.relations)))
    assert canonical_relation_set(altered) != canonical_relation_set(first)


def test_relation_set_digest_covers_kind_certainty_and_ambiguity_membership() -> None:
    digest = "a" * 64
    base, target = _pair(
        (
            _v2(_entry("base", "old-a"), digest),
            _v2(_entry("base", "old-b"), digest),
        ),
        (_v2(_entry("target", "new"), digest),),
        v2=True,
    )
    relation_set = compute_snapshot_relations(base, target)
    relation = relation_set.relations[0]
    changed_kind = replace(relation, kind=RelationKind.RENAME_CANDIDATE)
    changed_certainty = replace(relation, certainty=RelationCertainty.CANDIDATE)
    changed_membership = replace(relation, source_entries=relation.source_entries[:1])
    for changed_relation in (changed_kind, changed_certainty, changed_membership):
        changed_set = replace(relation_set, relations=(changed_relation,))
        assert sha256(canonical_relation_set(changed_set)).hexdigest() != relation_set.relation_set_digest
