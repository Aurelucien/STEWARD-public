"""Deterministic, on-demand exact payload grouping for one verified Snapshot."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from hashlib import sha256

from .errors import DuplicateAnalysisError
from .evidence import canonical_json
from .models import (
    CoverageSummary,
    DuplicateAnalysisResult,
    DuplicateStorageKnowledgeStatus,
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemObjectType,
    FilesystemObservationStatus,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    HardLinkAliasSet,
    IntegrityConflict,
    PayloadEqualityGroup,
    PayloadObservationProvenance,
    PayloadObservationStatus,
    PayloadUnknownReasonCount,
    PhysicalStorageSummary,
    SnapshotEntryReference,
    StorageUnit,
    StewardConfig,
)
from .snapshots import get_snapshot, verify_snapshot


ANALYSIS_SCHEMA_VERSION = 1
ANALYSIS_ALGORITHM = "exact_payload_duplicate_storage"
ANALYSIS_ALGORITHM_VERSION = 1
ANALYSIS_DIGEST_DOMAIN = "local_steward.exact_payload_duplicate_storage.v1"

Entry = FilesystemEntry | FilesystemEntryV2
PayloadKey = tuple[str, int, str, int]
ObjectHint = tuple[int, int]


def _reference(entry: Entry) -> SnapshotEntryReference:
    return SnapshotEntryReference(entry.snapshot_id, entry.scope_id, entry.relative_path)


def _reference_key(reference: SnapshotEntryReference) -> tuple[str, str, bytes]:
    return (
        reference.snapshot_id,
        reference.scope_id,
        reference.relative_path.encode("utf-8", "surrogateescape"),
    )


def _references(entries: Iterable[Entry]) -> tuple[SnapshotEntryReference, ...]:
    return tuple(sorted((_reference(entry) for entry in entries), key=_reference_key))


def _reference_data(reference: SnapshotEntryReference) -> dict[str, str]:
    return {
        "snapshot_id": reference.snapshot_id,
        "scope_id": reference.scope_id,
        "relative_path": reference.relative_path,
    }


def _object_hint(entry: Entry) -> ObjectHint | None:
    if entry.device_id is None or entry.inode is None:
        return None
    return (entry.device_id, entry.inode)


def _observed_regular(entry: Entry) -> bool:
    return (
        entry.object_type == FilesystemObjectType.REGULAR_FILE
        and entry.observation_status == FilesystemObservationStatus.OBSERVED
    )


def _payload_key(entry: Entry, *, reused_payloads_verified: bool) -> PayloadKey | None:
    if not isinstance(entry, FilesystemEntryV2) or not _observed_regular(entry) or entry.excluded:
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
    if entry.size_bytes is None or payload.bytes_hashed != entry.size_bytes:
        return None
    if payload.provenance == PayloadObservationProvenance.DIRECT_READ:
        return (payload.algorithm, payload.algorithm_version, payload.digest, entry.size_bytes)
    if (
        payload.provenance == PayloadObservationProvenance.REUSED_FROM_VERIFIED_SNAPSHOT
        and reused_payloads_verified
        and payload.reused_from_snapshot_id is not None
    ):
        return (payload.algorithm, payload.algorithm_version, payload.digest, entry.size_bytes)
    return None


def _unknown_reason(entry: Entry, *, reused_payloads_verified: bool) -> str:
    if not isinstance(entry, FilesystemEntryV2):
        return "V1_PAYLOAD_UNAVAILABLE"
    if entry.excluded:
        return "ENTRY_EXCLUDED"
    if entry.observation_status != FilesystemObservationStatus.OBSERVED:
        return f"METADATA_{entry.observation_status.value.upper()}"
    payload = entry.payload_observation
    if (
        payload.status in {PayloadObservationStatus.HASHED, PayloadObservationStatus.EMPTY_FILE_HASHED}
        and payload.provenance == PayloadObservationProvenance.REUSED_FROM_VERIFIED_SNAPSHOT
        and not reused_payloads_verified
    ):
        return "SOURCE_INVALID_REUSED"
    if payload.failure_code is not None:
        return payload.failure_code
    if payload.status in {PayloadObservationStatus.HASHED, PayloadObservationStatus.EMPTY_FILE_HASHED}:
        return "MALFORMED_PAYLOAD_OBSERVATION"
    return payload.status.value


def _alias_content(device_id: int, inode: int, members: tuple[SnapshotEntryReference, ...]) -> dict[str, object]:
    return {
        "device_id": device_id,
        "inode": inode,
        "member_entries": [_reference_data(reference) for reference in members],
    }


def _alias_set(device_id: int, inode: int, entries: Iterable[Entry]) -> HardLinkAliasSet:
    members = _references(entries)
    return HardLinkAliasSet(
        sha256(canonical_json(_alias_content(device_id, inode, members))).hexdigest(),
        device_id,
        inode,
        members,
    )


def _storage_unit_data(unit: StorageUnit) -> dict[str, object]:
    return {
        "device_id": unit.device_id,
        "inode": unit.inode,
        "member_entries": [_reference_data(reference) for reference in unit.member_entries],
        "membership_known": unit.membership_known,
        "integrity_conflicted": unit.integrity_conflicted,
        "logical_size_bytes": unit.logical_size_bytes,
        "allocated_size_bytes": unit.allocated_size_bytes,
    }


def _group_content(group: PayloadEqualityGroup) -> dict[str, object]:
    return {
        "algorithm": group.algorithm,
        "algorithm_version": group.algorithm_version,
        "digest": group.digest,
        "logical_size_bytes": group.logical_size_bytes,
        "member_entries": [_reference_data(reference) for reference in group.member_entries],
        "alias_set_ids": list(group.alias_set_ids),
        "storage_units": [_storage_unit_data(unit) for unit in group.storage_units],
        "known_storage_unit_count": group.known_storage_unit_count,
        "unknown_storage_unit_count": group.unknown_storage_unit_count,
        "is_exact_duplicate": group.is_exact_duplicate,
        "path_logical_bytes": group.path_logical_bytes,
        "known_unit_logical_bytes": group.known_unit_logical_bytes,
        "logical_redundant_bytes": group.logical_redundant_bytes,
    }


def _conflict_data(conflict: IntegrityConflict) -> dict[str, object]:
    return {"code": conflict.code, "entries": [_reference_data(item) for item in conflict.entries]}


def _coverage_data(coverage: CoverageSummary) -> dict[str, object]:
    return {
        "total_entry_count": coverage.total_entry_count,
        "total_regular_entry_count": coverage.total_regular_entry_count,
        "payload_analyzable_regular_entry_count": coverage.payload_analyzable_regular_entry_count,
        "payload_unknown_regular_entry_count": coverage.payload_unknown_regular_entry_count,
        "analyzable_logical_bytes": coverage.analyzable_logical_bytes,
        "unknown_logical_bytes": coverage.unknown_logical_bytes,
        "payload_status_counts": [
            {"code": item.code, "count": item.count}
            for item in coverage.payload_status_counts
        ],
        "payload_unknown_reason_counts": [
            {"code": item.code, "count": item.count}
            for item in coverage.payload_unknown_reason_counts
        ],
        "alias_path_count": coverage.alias_path_count,
        "known_storage_unit_count": coverage.known_storage_unit_count,
        "unknown_storage_unit_membership_count": coverage.unknown_storage_unit_membership_count,
    }


def _physical_data(physical: PhysicalStorageSummary) -> dict[str, object]:
    return {
        "allocated_size_bytes_known_sum": physical.allocated_size_bytes_known_sum,
        "allocated_size_known_unit_count": physical.allocated_size_known_unit_count,
        "allocated_size_unknown_unit_count": physical.allocated_size_unknown_unit_count,
        "allocation_status": physical.allocation_status.value,
        "physical_block_sharing_status": physical.physical_block_sharing_status.value,
        "reclaimable_bytes": physical.reclaimable_bytes,
        "reclaimable_status": physical.reclaimable_status.value,
    }


def canonical_duplicate_analysis(result: DuplicateAnalysisResult) -> bytes:
    """Return complete canonical result bytes excluding the derived result digest."""
    return canonical_json(
        {
            "domain": ANALYSIS_DIGEST_DOMAIN,
            "analysis_schema_version": result.analysis_schema_version,
            "algorithm": result.algorithm,
            "algorithm_version": result.algorithm_version,
            "snapshot_id": result.snapshot_id,
            "payload_equality_groups": [
                {"payload_group_id": group.payload_group_id, **_group_content(group)}
                for group in result.payload_equality_groups
            ],
            "hard_link_alias_sets": [
                {
                    "alias_set_id": alias.alias_set_id,
                    **_alias_content(alias.device_id, alias.inode, alias.member_entries),
                }
                for alias in result.hard_link_alias_sets
            ],
            "coverage": _coverage_data(result.coverage),
            "physical_storage": _physical_data(result.physical_storage),
            "integrity_conflicts": [_conflict_data(item) for item in result.integrity_conflicts],
        }
    )


def _conflict_sort_key(conflict: IntegrityConflict) -> tuple[str, tuple[tuple[str, str, bytes], ...]]:
    return (conflict.code, tuple(_reference_key(item) for item in conflict.entries))


def _group_sort_key(group: PayloadEqualityGroup) -> tuple[object, ...]:
    return (
        group.algorithm,
        group.algorithm_version,
        group.digest,
        group.logical_size_bytes,
        tuple(_reference_key(item) for item in group.member_entries),
    )


def _entry_maps(entries: tuple[Entry, ...], snapshot_id: str) -> None:
    references = set()
    for entry in entries:
        if entry.snapshot_id != snapshot_id:
            raise DuplicateAnalysisError(
                "DUPLICATE_INVALID: entry snapshot identity does not match input"
            )
        key = (entry.scope_id, entry.relative_path)
        if key in references:
            raise DuplicateAnalysisError(
                "DUPLICATE_INVALID: duplicate scoped location in Snapshot"
            )
        references.add(key)


def _analysis_result(
    snapshot_id: str,
    groups: Iterable[PayloadEqualityGroup],
    aliases: Iterable[HardLinkAliasSet],
    coverage: CoverageSummary,
    physical: PhysicalStorageSummary,
    conflicts: Iterable[IntegrityConflict],
) -> DuplicateAnalysisResult:
    ordered_groups = tuple(sorted(groups, key=_group_sort_key))
    ordered_aliases = tuple(
        sorted(
            aliases,
            key=lambda item: (item.device_id, item.inode, tuple(_reference_key(x) for x in item.member_entries)),
        )
    )
    ordered_conflicts = tuple(sorted(conflicts, key=_conflict_sort_key))
    provisional = DuplicateAnalysisResult(
        ANALYSIS_SCHEMA_VERSION,
        ANALYSIS_ALGORITHM,
        ANALYSIS_ALGORITHM_VERSION,
        snapshot_id,
        ordered_groups,
        ordered_aliases,
        coverage,
        physical,
        ordered_conflicts,
        "",
    )
    return DuplicateAnalysisResult(
        ANALYSIS_SCHEMA_VERSION,
        ANALYSIS_ALGORITHM,
        ANALYSIS_ALGORITHM_VERSION,
        snapshot_id,
        ordered_groups,
        ordered_aliases,
        coverage,
        physical,
        ordered_conflicts,
        sha256(canonical_duplicate_analysis(provisional)).hexdigest(),
    )


def compute_snapshot_duplicate_analysis(
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
    *,
    reused_payloads_verified: bool = False,
) -> DuplicateAnalysisResult:
    """Compute one in-memory analysis from an already validated Snapshot fact.

    This pure function performs no repository, filesystem, or SQLite access.
    Persisted callers must use :func:`compute_verified_snapshot_duplicate_analysis`.
    """
    entries: tuple[Entry, ...] = snapshot.entries
    _entry_maps(entries, snapshot.snapshot_id)

    regular_entries = tuple(entry for entry in entries if entry.object_type == FilesystemObjectType.REGULAR_FILE)
    observed_regular = tuple(entry for entry in regular_entries if _observed_regular(entry) and not entry.excluded)
    payload_keys = {
        _reference(entry): _payload_key(entry, reused_payloads_verified=reused_payloads_verified)
        for entry in regular_entries
    }

    unknown_reasons: Counter[str] = Counter()
    payload_statuses: Counter[str] = Counter()
    analyzable = []
    unknown_logical_bytes = 0
    for entry in regular_entries:
        if isinstance(entry, FilesystemEntryV2):
            payload_statuses[entry.payload_observation.status.value] += 1
        else:
            payload_statuses["V1_PAYLOAD_UNAVAILABLE"] += 1
        key = payload_keys[_reference(entry)]
        if key is not None:
            analyzable.append(entry)
        else:
            unknown_reasons[_unknown_reason(entry, reused_payloads_verified=reused_payloads_verified)] += 1
            unknown_logical_bytes += entry.size_bytes or 0

    by_hint: dict[ObjectHint, list[Entry]] = defaultdict(list)
    for entry in observed_regular:
        hint = _object_hint(entry)
        if hint is not None:
            by_hint[hint].append(entry)
    alias_sets = tuple(
        _alias_set(hint[0], hint[1], members)
        for hint, members in sorted(by_hint.items())
        if len(members) >= 2
    )
    aliases_by_hint = {
        (item.device_id, item.inode): item
        for item in alias_sets
    }

    conflicts: list[IntegrityConflict] = []
    conflicted_hints: set[ObjectHint] = set()
    for hint, members in sorted(by_hint.items()):
        if len(members) < 2:
            continue
        references = _references(members)
        known_payloads = {
            payload_keys[_reference(member)] for member in members if payload_keys[_reference(member)] is not None
        }
        if len(known_payloads) > 1:
            conflicts.append(IntegrityConflict("OBJECT_HINT_PAYLOAD_CONFLICT", references))
            conflicted_hints.add(hint)
        sizes = {member.size_bytes for member in members if member.size_bytes is not None}
        if len(sizes) > 1:
            conflicts.append(IntegrityConflict("OBJECT_HINT_SIZE_CONFLICT", references))
            conflicted_hints.add(hint)
        counts = {member.link_count for member in members if member.link_count is not None}
        if any(count < len(members) for count in counts):
            conflicts.append(IntegrityConflict("LINK_COUNT_CONFLICT", references))
            conflicted_hints.add(hint)
        allocations = {member.allocated_size_bytes for member in members if isinstance(member, FilesystemEntryV2) and member.allocated_size_bytes is not None}
        if len(allocations) > 1:
            conflicts.append(IntegrityConflict("ALLOCATED_SIZE_CONFLICT", references))
            conflicted_hints.add(hint)

    by_digest: dict[tuple[str, int, str], list[Entry]] = defaultdict(list)
    for entry in analyzable:
        key = payload_keys[_reference(entry)]
        assert key is not None
        by_digest[key[:3]].append(entry)
    payload_size_conflict_entries: set[SnapshotEntryReference] = set()
    for _digest_key, members in sorted(by_digest.items()):
        digest_sizes: set[int] = set()
        for member in members:
            member_key = payload_keys[_reference(member)]
            assert member_key is not None
            digest_sizes.add(member_key[3])
        if len(digest_sizes) > 1:
            references = _references(members)
            conflicts.append(IntegrityConflict("PAYLOAD_SIZE_CONFLICT", references))
            payload_size_conflict_entries.update(references)

    by_payload: dict[PayloadKey, list[Entry]] = defaultdict(list)
    for entry in analyzable:
        if _reference(entry) not in payload_size_conflict_entries:
            key = payload_keys[_reference(entry)]
            assert key is not None
            by_payload[key].append(entry)

    groups: list[PayloadEqualityGroup] = []
    for key, members in sorted(by_payload.items()):
        if len(members) < 2:
            continue
        algorithm, algorithm_version, digest, logical_size = key
        members = sorted(members, key=lambda item: _reference_key(_reference(item)))
        known_by_hint: dict[ObjectHint, list[Entry]] = defaultdict(list)
        unknown_members: list[Entry] = []
        for member in members:
            hint = _object_hint(member)
            if hint is None:
                unknown_members.append(member)
            else:
                known_by_hint[hint].append(member)
        units: list[StorageUnit] = []
        alias_ids: set[str] = set()
        for hint, unit_members in sorted(known_by_hint.items()):
            alias = aliases_by_hint.get(hint)
            if alias is not None:
                alias_ids.add(alias.alias_set_id)
                unit_references = alias.member_entries
            else:
                unit_references = _references(unit_members)
            allocations = {
                member.allocated_size_bytes
                for member in unit_members
                if isinstance(member, FilesystemEntryV2) and member.allocated_size_bytes is not None
            }
            allocation = next(iter(allocations)) if len(allocations) == 1 and len(unit_members) == len(unit_references) else None
            units.append(
                StorageUnit(
                    hint[0],
                    hint[1],
                    unit_references,
                    True,
                    hint in conflicted_hints,
                    logical_size,
                    allocation,
                )
            )
        for member in sorted(unknown_members, key=lambda item: _reference_key(_reference(item))):
            units.append(
                StorageUnit(
                    None,
                    None,
                    (_reference(member),),
                    False,
                    False,
                    logical_size,
                    None,
                )
            )
        units.sort(
            key=lambda unit: (
                not unit.membership_known,
                unit.device_id if unit.device_id is not None else -1,
                unit.inode if unit.inode is not None else -1,
                tuple(_reference_key(item) for item in unit.member_entries),
            )
        )
        known_units = tuple(unit for unit in units if unit.membership_known and not unit.integrity_conflicted)
        unknown_count = sum(not unit.membership_known for unit in units)
        has_conflict = any(unit.integrity_conflicted for unit in units)
        group_without_id = PayloadEqualityGroup(
            "",
            algorithm,
            algorithm_version,
            digest,
            logical_size,
            _references(members),
            tuple(sorted(alias_ids)),
            tuple(units),
            len(known_units),
            unknown_count,
            len(known_units) >= 2,
            logical_size * len(members),
            logical_size * len(known_units),
            logical_size * (len(known_units) - 1)
            if len(known_units) >= 2 and unknown_count == 0 and not has_conflict
            else None,
        )
        groups.append(
            PayloadEqualityGroup(
                sha256(canonical_json(_group_content(group_without_id))).hexdigest(),
                group_without_id.algorithm,
                group_without_id.algorithm_version,
                group_without_id.digest,
                group_without_id.logical_size_bytes,
                group_without_id.member_entries,
                group_without_id.alias_set_ids,
                group_without_id.storage_units,
                group_without_id.known_storage_unit_count,
                group_without_id.unknown_storage_unit_count,
                group_without_id.is_exact_duplicate,
                group_without_id.path_logical_bytes,
                group_without_id.known_unit_logical_bytes,
                group_without_id.logical_redundant_bytes,
            )
        )

    known_units_all: dict[ObjectHint, list[Entry]] = defaultdict(list)
    unknown_memberships = 0
    for entry in observed_regular:
        hint = _object_hint(entry)
        if hint is None:
            unknown_memberships += 1
        else:
            known_units_all[hint].append(entry)
    unit_allocations: list[int | None] = []
    for hint, members in known_units_all.items():
        if hint in conflicted_hints:
            unit_allocations.append(None)
            continue
        values = {
            member.allocated_size_bytes
            for member in members
            if isinstance(member, FilesystemEntryV2) and member.allocated_size_bytes is not None
        }
        unit_allocations.append(next(iter(values)) if len(values) == 1 and len(values) == len(members) else None)
    unit_allocations.extend([None] * unknown_memberships)
    coverage = CoverageSummary(
        len(entries),
        len(regular_entries),
        len(analyzable),
        len(regular_entries) - len(analyzable),
        sum(entry.size_bytes or 0 for entry in analyzable),
        unknown_logical_bytes,
        tuple(PayloadUnknownReasonCount(code, count) for code, count in sorted(payload_statuses.items())),
        tuple(PayloadUnknownReasonCount(code, count) for code, count in sorted(unknown_reasons.items())),
        sum(len(alias.member_entries) for alias in alias_sets),
        len(known_units_all),
        unknown_memberships,
    )
    physical = PhysicalStorageSummary(
        sum(value for value in unit_allocations if value is not None),
        sum(value is not None for value in unit_allocations),
        sum(value is None for value in unit_allocations),
        DuplicateStorageKnowledgeStatus.UNKNOWN,
        DuplicateStorageKnowledgeStatus.UNKNOWN,
        None,
        DuplicateStorageKnowledgeStatus.UNKNOWN,
    )
    return _analysis_result(snapshot.snapshot_id, groups, alias_sets, coverage, physical, conflicts)


def compute_verified_snapshot_duplicate_analysis(
    config: StewardConfig, snapshot_id: str
) -> DuplicateAnalysisResult:
    """Repository-verify and analyze one explicit persisted Snapshot Evidence fact."""
    try:
        verification = verify_snapshot(config, snapshot_id)
    except Exception as error:
        raise DuplicateAnalysisError(
            "DUPLICATE_INVALID: Snapshot ID must be available"
        ) from error
    if verification.status != "VALID":
        raise DuplicateAnalysisError(
            "DUPLICATE_INVALID: duplicate analysis requires VALID Snapshot Evidence: " + snapshot_id
        )
    try:
        snapshot = get_snapshot(config, snapshot_id)
    except Exception as error:
        raise DuplicateAnalysisError(
            "DUPLICATE_INVALID: Snapshot ID must be available"
        ) from error
    return compute_snapshot_duplicate_analysis(snapshot, reused_payloads_verified=True)
