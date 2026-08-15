"""Deterministic, on-demand Path View growth for an ordered Snapshot pair."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256

from .errors import GrowthError, StructureError
from .evidence import canonical_json
from .models import (
    DuplicateStorageKnowledgeStatus,
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemObjectType,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    GrowthContribution,
    GrowthContributionKind,
    GrowthCoverageSummary,
    PathAggregateNode,
    PathGrowthNode,
    ScopeGrowthSummary,
    SnapshotEntryReference,
    StorageGrowthResult,
    StorageStructureResult,
    StructurePhysicalBoundary,
    StewardConfig,
)
from .snapshots import get_snapshot, verify_snapshot
from .storage_structure import (
    _known_regular_size,
    _parent,
    _reference,
    _reference_data,
    compute_snapshot_structure,
)


GROWTH_SCHEMA_VERSION = 1
GROWTH_ALGORITHM = "storage_growth"
GROWTH_ALGORITHM_VERSION = 1
GROWTH_DIGEST_DOMAIN = "local_steward.storage_growth.v1"

Entry = FilesystemEntry | FilesystemEntryV2
Snapshot = FilesystemSnapshot | FilesystemSnapshotV2
Location = tuple[str, str]


@dataclass(slots=True)
class _Metrics:
    known_net_logical_delta: int = 0
    added_logical_bytes: int = 0
    added_location_count: int = 0
    removed_logical_bytes: int = 0
    removed_location_count: int = 0
    same_location_increase_bytes: int = 0
    same_location_increase_count: int = 0
    same_location_decrease_bytes: int = 0
    same_location_decrease_count: int = 0
    same_location_unchanged_count: int = 0
    unknown_size_contribution_count: int = 0

    def add(self, other: _Metrics) -> None:
        self.known_net_logical_delta += other.known_net_logical_delta
        self.added_logical_bytes += other.added_logical_bytes
        self.added_location_count += other.added_location_count
        self.removed_logical_bytes += other.removed_logical_bytes
        self.removed_location_count += other.removed_location_count
        self.same_location_increase_bytes += other.same_location_increase_bytes
        self.same_location_increase_count += other.same_location_increase_count
        self.same_location_decrease_bytes += other.same_location_decrease_bytes
        self.same_location_decrease_count += other.same_location_decrease_count
        self.same_location_unchanged_count += other.same_location_unchanged_count
        self.unknown_size_contribution_count += other.unknown_size_contribution_count


@dataclass(slots=True)
class _NodeBuilder:
    children: set[str] = field(default_factory=set)
    direct: _Metrics = field(default_factory=_Metrics)


@dataclass(frozen=True, slots=True)
class _LocatedContribution:
    scope_id: str
    relative_path: str
    contribution: GrowthContribution


def _location_key(location: Location) -> tuple[str, bytes]:
    return (location[0], location[1].encode("utf-8", "surrogateescape"))


def _node_key(node: PathGrowthNode) -> tuple[str, bytes]:
    return (node.scope_id, node.relative_directory_path.encode("utf-8", "surrogateescape"))


def _growth_node_id(base_snapshot_id: str, target_snapshot_id: str, scope_id: str, path: str) -> str:
    return sha256(
        canonical_json(
            {
                "growth_schema_version": GROWTH_SCHEMA_VERSION,
                "algorithm": GROWTH_ALGORITHM,
                "algorithm_version": GROWTH_ALGORITHM_VERSION,
                "base_snapshot_id": base_snapshot_id,
                "target_snapshot_id": target_snapshot_id,
                "scope_id": scope_id,
                "relative_directory_path": path,
            }
        )
    ).hexdigest()


def _contribution_data(contribution: GrowthContribution, *, include_id: bool = True) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": contribution.kind.value,
        "entry_references": [_reference_data(item) for item in contribution.entry_references],
        "known_byte_delta": contribution.known_byte_delta,
        "reason_codes": list(contribution.reason_codes),
    }
    if include_id:
        data = {"growth_contribution_id": contribution.growth_contribution_id, **data}
    return data


def _contribution_id(
    kind: GrowthContributionKind,
    references: tuple[SnapshotEntryReference, ...],
    known_byte_delta: int | None,
    reason_codes: tuple[str, ...],
) -> str:
    provisional = GrowthContribution("", kind, references, known_byte_delta, reason_codes)
    return sha256(
        canonical_json(
            {
                "growth_schema_version": GROWTH_SCHEMA_VERSION,
                "algorithm": GROWTH_ALGORITHM,
                "algorithm_version": GROWTH_ALGORITHM_VERSION,
                "contribution": _contribution_data(provisional, include_id=False),
            }
        )
    ).hexdigest()


def _contribution(
    kind: GrowthContributionKind,
    base: Entry | None,
    target: Entry | None,
    known_byte_delta: int | None,
    reason_codes: tuple[str, ...] = (),
) -> GrowthContribution:
    references = tuple(
        reference
        for reference in (_reference(base) if base is not None else None, _reference(target) if target is not None else None)
        if reference is not None
    )
    return GrowthContribution(
        _contribution_id(kind, references, known_byte_delta, reason_codes),
        kind,
        references,
        known_byte_delta,
        reason_codes,
    )


def _node_data(node: PathGrowthNode) -> dict[str, object]:
    return {
        "growth_node_id": node.growth_node_id,
        "base_snapshot_id": node.base_snapshot_id,
        "target_snapshot_id": node.target_snapshot_id,
        "scope_id": node.scope_id,
        "relative_directory_path": node.relative_directory_path,
        "direct_base_known_logical_bytes": node.direct_base_known_logical_bytes,
        "recursive_base_known_logical_bytes": node.recursive_base_known_logical_bytes,
        "direct_target_known_logical_bytes": node.direct_target_known_logical_bytes,
        "recursive_target_known_logical_bytes": node.recursive_target_known_logical_bytes,
        "direct_known_net_logical_delta": node.direct_known_net_logical_delta,
        "recursive_known_net_logical_delta": node.recursive_known_net_logical_delta,
        "direct_added_logical_bytes": node.direct_added_logical_bytes,
        "recursive_added_logical_bytes": node.recursive_added_logical_bytes,
        "direct_added_location_count": node.direct_added_location_count,
        "recursive_added_location_count": node.recursive_added_location_count,
        "direct_removed_logical_bytes": node.direct_removed_logical_bytes,
        "recursive_removed_logical_bytes": node.recursive_removed_logical_bytes,
        "direct_removed_location_count": node.direct_removed_location_count,
        "recursive_removed_location_count": node.recursive_removed_location_count,
        "direct_same_location_increase_bytes": node.direct_same_location_increase_bytes,
        "recursive_same_location_increase_bytes": node.recursive_same_location_increase_bytes,
        "direct_same_location_increase_count": node.direct_same_location_increase_count,
        "recursive_same_location_increase_count": node.recursive_same_location_increase_count,
        "direct_same_location_decrease_bytes": node.direct_same_location_decrease_bytes,
        "recursive_same_location_decrease_bytes": node.recursive_same_location_decrease_bytes,
        "direct_same_location_decrease_count": node.direct_same_location_decrease_count,
        "recursive_same_location_decrease_count": node.recursive_same_location_decrease_count,
        "direct_same_location_unchanged_count": node.direct_same_location_unchanged_count,
        "recursive_same_location_unchanged_count": node.recursive_same_location_unchanged_count,
        "direct_unknown_size_contribution_count": node.direct_unknown_size_contribution_count,
        "recursive_unknown_size_contribution_count": node.recursive_unknown_size_contribution_count,
        "decomposition_complete": node.decomposition_complete,
    }


def _scope_data(summary: ScopeGrowthSummary) -> dict[str, object]:
    return {
        "base_snapshot_id": summary.base_snapshot_id,
        "target_snapshot_id": summary.target_snapshot_id,
        "scope_id": summary.scope_id,
        "root_node_id": summary.root_node_id,
        "base_recursive_known_logical_bytes": summary.base_recursive_known_logical_bytes,
        "target_recursive_known_logical_bytes": summary.target_recursive_known_logical_bytes,
        "known_net_logical_delta": summary.known_net_logical_delta,
        "added_logical_bytes": summary.added_logical_bytes,
        "added_location_count": summary.added_location_count,
        "removed_logical_bytes": summary.removed_logical_bytes,
        "removed_location_count": summary.removed_location_count,
        "same_location_increase_bytes": summary.same_location_increase_bytes,
        "same_location_increase_count": summary.same_location_increase_count,
        "same_location_decrease_bytes": summary.same_location_decrease_bytes,
        "same_location_decrease_count": summary.same_location_decrease_count,
        "same_location_unchanged_count": summary.same_location_unchanged_count,
        "unknown_size_contribution_count": summary.unknown_size_contribution_count,
        "decomposition_complete": summary.decomposition_complete,
    }


def _coverage_data(coverage: GrowthCoverageSummary) -> dict[str, object]:
    return {
        "base_total_entry_count": coverage.base_total_entry_count,
        "target_total_entry_count": coverage.target_total_entry_count,
        "base_known_size_regular_file_count": coverage.base_known_size_regular_file_count,
        "target_known_size_regular_file_count": coverage.target_known_size_regular_file_count,
        "base_unknown_size_regular_file_count": coverage.base_unknown_size_regular_file_count,
        "target_unknown_size_regular_file_count": coverage.target_unknown_size_regular_file_count,
        "co_present_comparable_regular_location_count": coverage.co_present_comparable_regular_location_count,
        "added_known_size_location_count": coverage.added_known_size_location_count,
        "added_unknown_size_location_count": coverage.added_unknown_size_location_count,
        "removed_known_size_location_count": coverage.removed_known_size_location_count,
        "removed_unknown_size_location_count": coverage.removed_unknown_size_location_count,
        "same_location_known_size_comparable_count": coverage.same_location_known_size_comparable_count,
        "same_location_unknown_size_count": coverage.same_location_unknown_size_count,
        "unknown_size_contribution_count": coverage.unknown_size_contribution_count,
        "base_scope_overlap_object_hint_count": coverage.base_scope_overlap_object_hint_count,
        "target_scope_overlap_object_hint_count": coverage.target_scope_overlap_object_hint_count,
        "known_net_logical_delta": coverage.known_net_logical_delta,
        "decomposition_complete": coverage.decomposition_complete,
    }


def _physical_data(boundary: StructurePhysicalBoundary) -> dict[str, object]:
    return {
        "allocation_status": boundary.allocation_status.value,
        "physical_block_sharing_status": boundary.physical_block_sharing_status.value,
        "reclaimable_bytes": boundary.reclaimable_bytes,
        "reclaimable_status": boundary.reclaimable_status.value,
        "object_aware_capacity_status": boundary.object_aware_capacity_status.value,
    }


def canonical_storage_growth(result: StorageGrowthResult) -> bytes:
    """Return complete canonical bytes excluding the derived growth digest."""
    return canonical_json(
        {
            "domain": GROWTH_DIGEST_DOMAIN,
            "growth_schema_version": result.growth_schema_version,
            "algorithm": result.algorithm,
            "algorithm_version": result.algorithm_version,
            "base_snapshot_id": result.base_snapshot_id,
            "target_snapshot_id": result.target_snapshot_id,
            "scope_summaries": [_scope_data(item) for item in result.scope_summaries],
            "path_nodes": [_node_data(item) for item in result.path_nodes],
            "contributions": [_contribution_data(item) for item in result.contributions],
            "coverage": _coverage_data(result.coverage),
            "physical_boundary": _physical_data(result.physical_boundary),
        }
    )


def _validate_pair(base: Snapshot, target: Snapshot) -> None:
    if base.snapshot_id == target.snapshot_id:
        raise GrowthError("GROWTH_INVALID: base and target Snapshot IDs must be distinct")
    if base.completed_at > target.started_at:
        raise GrowthError("GROWTH_INVALID: base Snapshot must not follow target Snapshot")


def _regular_entries(snapshot: Snapshot) -> dict[Location, Entry]:
    return {
        (entry.scope_id, entry.relative_path): entry
        for entry in snapshot.entries
        if not entry.excluded and entry.object_type == FilesystemObjectType.REGULAR_FILE
    }


def _classify(base: Entry | None, target: Entry | None) -> GrowthContribution:
    base_size = _known_regular_size(base) if base is not None else None
    target_size = _known_regular_size(target) if target is not None else None
    if base is None:
        if target_size is None:
            return _contribution(
                GrowthContributionKind.SIZE_UNKNOWN,
                None,
                target,
                None,
                ("SIZE_COVERAGE_INCOMPLETE",),
            )
        return _contribution(GrowthContributionKind.ADDED_LOCATION, None, target, target_size)
    if target is None:
        if base_size is None:
            return _contribution(
                GrowthContributionKind.SIZE_UNKNOWN,
                base,
                None,
                None,
                ("SIZE_COVERAGE_INCOMPLETE",),
            )
        return _contribution(GrowthContributionKind.REMOVED_LOCATION, base, None, -base_size)
    if base_size is None or target_size is None:
        return _contribution(
            GrowthContributionKind.SIZE_UNKNOWN,
            base,
            target,
            None,
            ("SIZE_COVERAGE_INCOMPLETE",),
        )
    if target_size > base_size:
        return _contribution(
            GrowthContributionKind.SAME_LOCATION_SIZE_INCREASE,
            base,
            target,
            target_size - base_size,
        )
    if target_size < base_size:
        return _contribution(
            GrowthContributionKind.SAME_LOCATION_SIZE_DECREASE,
            base,
            target,
            target_size - base_size,
        )
    return _contribution(GrowthContributionKind.SAME_LOCATION_SIZE_UNCHANGED, base, target, 0)


def _metrics_for(contribution: GrowthContribution) -> _Metrics:
    metrics = _Metrics()
    if contribution.kind == GrowthContributionKind.ADDED_LOCATION:
        metrics.added_location_count = 1
        metrics.added_logical_bytes = contribution.known_byte_delta or 0
        metrics.known_net_logical_delta = contribution.known_byte_delta or 0
    elif contribution.kind == GrowthContributionKind.REMOVED_LOCATION:
        metrics.removed_location_count = 1
        metrics.removed_logical_bytes = -(contribution.known_byte_delta or 0)
        metrics.known_net_logical_delta = contribution.known_byte_delta or 0
    elif contribution.kind == GrowthContributionKind.SAME_LOCATION_SIZE_INCREASE:
        metrics.same_location_increase_count = 1
        metrics.same_location_increase_bytes = contribution.known_byte_delta or 0
        metrics.known_net_logical_delta = contribution.known_byte_delta or 0
    elif contribution.kind == GrowthContributionKind.SAME_LOCATION_SIZE_DECREASE:
        metrics.same_location_decrease_count = 1
        metrics.same_location_decrease_bytes = -(contribution.known_byte_delta or 0)
        metrics.known_net_logical_delta = contribution.known_byte_delta or 0
    elif contribution.kind == GrowthContributionKind.SAME_LOCATION_SIZE_UNCHANGED:
        metrics.same_location_unchanged_count = 1
    else:
        metrics.unknown_size_contribution_count = 1
    return metrics


def _regular_counts(entries: dict[Location, Entry]) -> tuple[int, int]:
    known = sum(_known_regular_size(entry) is not None for entry in entries.values())
    return known, len(entries) - known


def _node_from(
    base_snapshot_id: str,
    target_snapshot_id: str,
    scope_id: str,
    path: str,
    direct: _Metrics,
    recursive: _Metrics,
    base_nodes: Mapping[Location, PathAggregateNode],
    target_nodes: Mapping[Location, PathAggregateNode],
    complete: bool,
) -> PathGrowthNode:
    base = base_nodes.get((scope_id, path))
    target = target_nodes.get((scope_id, path))
    direct_base = base.direct_known_logical_bytes if base is not None else 0
    recursive_base = base.recursive_known_logical_bytes if base is not None else 0
    direct_target = target.direct_known_logical_bytes if target is not None else 0
    recursive_target = target.recursive_known_logical_bytes if target is not None else 0
    return PathGrowthNode(
        _growth_node_id(base_snapshot_id, target_snapshot_id, scope_id, path),
        base_snapshot_id,
        target_snapshot_id,
        scope_id,
        path,
        direct_base,
        recursive_base,
        direct_target,
        recursive_target,
        direct.known_net_logical_delta,
        recursive.known_net_logical_delta,
        direct.added_logical_bytes,
        recursive.added_logical_bytes,
        direct.added_location_count,
        recursive.added_location_count,
        direct.removed_logical_bytes,
        recursive.removed_logical_bytes,
        direct.removed_location_count,
        recursive.removed_location_count,
        direct.same_location_increase_bytes,
        recursive.same_location_increase_bytes,
        direct.same_location_increase_count,
        recursive.same_location_increase_count,
        direct.same_location_decrease_bytes,
        recursive.same_location_decrease_bytes,
        direct.same_location_decrease_count,
        recursive.same_location_decrease_count,
        direct.same_location_unchanged_count,
        recursive.same_location_unchanged_count,
        direct.unknown_size_contribution_count,
        recursive.unknown_size_contribution_count,
        complete,
    )


def _scope_nodes(
    base: StorageStructureResult,
    target: StorageStructureResult,
    scope_id: str,
    contributions: tuple[_LocatedContribution, ...],
) -> tuple[PathGrowthNode, ...]:
    base_nodes = {(node.scope_id, node.relative_directory_path): node for node in base.path_nodes}
    target_nodes = {(node.scope_id, node.relative_directory_path): node for node in target.path_nodes}
    paths = {
        path
        for node_scope, path in set(base_nodes) | set(target_nodes)
        if node_scope == scope_id
    }
    paths.add(".")
    builders = {path: _NodeBuilder() for path in paths}
    for path in paths:
        if path != ".":
            builders[_parent(path)].children.add(path)
    for located in contributions:
        if located.scope_id != scope_id:
            continue
        parent = _parent(located.relative_path)
        builders[parent].direct.add(_metrics_for(located.contribution))

    resolved: dict[str, PathGrowthNode] = {}

    def build(path: str) -> PathGrowthNode:
        existing = resolved.get(path)
        if existing is not None:
            return existing
        builder = builders[path]
        recursive = _Metrics()
        recursive.add(builder.direct)
        children = tuple(
            build(child)
            for child in sorted(builder.children, key=lambda item: item.encode("utf-8", "surrogateescape"))
        )
        for child in children:
            recursive.add(
                _Metrics(
                    child.recursive_known_net_logical_delta,
                    child.recursive_added_logical_bytes,
                    child.recursive_added_location_count,
                    child.recursive_removed_logical_bytes,
                    child.recursive_removed_location_count,
                    child.recursive_same_location_increase_bytes,
                    child.recursive_same_location_increase_count,
                    child.recursive_same_location_decrease_bytes,
                    child.recursive_same_location_decrease_count,
                    child.recursive_same_location_unchanged_count,
                    child.recursive_unknown_size_contribution_count,
                )
            )
        complete = recursive.unknown_size_contribution_count == 0
        node = _node_from(
            base.snapshot_id,
            target.snapshot_id,
            scope_id,
            path,
            builder.direct,
            recursive,
            base_nodes,
            target_nodes,
            complete,
        )
        if node.recursive_known_net_logical_delta != (
            node.recursive_added_logical_bytes
            - node.recursive_removed_logical_bytes
            + node.recursive_same_location_increase_bytes
            - node.recursive_same_location_decrease_bytes
        ):
            raise GrowthError("GROWTH_INVALID: node growth decomposition mismatch")
        if node.decomposition_complete and node.recursive_known_net_logical_delta != (
            node.recursive_target_known_logical_bytes - node.recursive_base_known_logical_bytes
        ):
            raise GrowthError("GROWTH_INVALID: node target-minus-base identity mismatch")
        resolved[path] = node
        return node

    build(".")
    return tuple(sorted(resolved.values(), key=_node_key))


def _coverage(
    base_snapshot: Snapshot,
    target_snapshot: Snapshot,
    base_structure: StorageStructureResult,
    target_structure: StorageStructureResult,
    base_regular: dict[Location, Entry],
    target_regular: dict[Location, Entry],
    contributions: tuple[_LocatedContribution, ...],
) -> GrowthCoverageSummary:
    base_known, base_unknown = _regular_counts(base_regular)
    target_known, target_unknown = _regular_counts(target_regular)
    items = tuple(item.contribution for item in contributions)
    counts: defaultdict[GrowthContributionKind, int] = defaultdict(int)
    for item in items:
        counts[item.kind] += 1
    comparable = sum(
        _known_regular_size(base_regular[location]) is not None
        and _known_regular_size(target_regular[location]) is not None
        for location in set(base_regular) & set(target_regular)
    )
    same_unknown = sum(
        _known_regular_size(base_regular[location]) is None
        or _known_regular_size(target_regular[location]) is None
        for location in set(base_regular) & set(target_regular)
    )
    known_net = sum(item.known_byte_delta or 0 for item in items)
    unknown_count = counts[GrowthContributionKind.SIZE_UNKNOWN]
    return GrowthCoverageSummary(
        len(base_snapshot.entries),
        len(target_snapshot.entries),
        base_known,
        target_known,
        base_unknown,
        target_unknown,
        comparable,
        counts[GrowthContributionKind.ADDED_LOCATION],
        sum(
            1
            for item in items
            if item.kind == GrowthContributionKind.SIZE_UNKNOWN
            and len(item.entry_references) == 1
            and item.entry_references[0].snapshot_id == target_snapshot.snapshot_id
        ),
        counts[GrowthContributionKind.REMOVED_LOCATION],
        sum(
            1
            for item in items
            if item.kind == GrowthContributionKind.SIZE_UNKNOWN
            and len(item.entry_references) == 1
            and item.entry_references[0].snapshot_id == base_snapshot.snapshot_id
        ),
        comparable,
        same_unknown,
        unknown_count,
        base_structure.coverage.scope_overlap_object_hint_count,
        target_structure.coverage.scope_overlap_object_hint_count,
        known_net,
        unknown_count == 0,
    )


def _physical_boundary() -> StructurePhysicalBoundary:
    return StructurePhysicalBoundary(
        DuplicateStorageKnowledgeStatus.UNKNOWN,
        DuplicateStorageKnowledgeStatus.UNKNOWN,
        None,
        DuplicateStorageKnowledgeStatus.UNKNOWN,
        DuplicateStorageKnowledgeStatus.UNKNOWN,
    )


def _result(
    base_snapshot_id: str,
    target_snapshot_id: str,
    summaries: tuple[ScopeGrowthSummary, ...],
    nodes: tuple[PathGrowthNode, ...],
    contributions: tuple[GrowthContribution, ...],
    coverage: GrowthCoverageSummary,
) -> StorageGrowthResult:
    boundary = _physical_boundary()
    provisional = StorageGrowthResult(
        GROWTH_SCHEMA_VERSION,
        GROWTH_ALGORITHM,
        GROWTH_ALGORITHM_VERSION,
        base_snapshot_id,
        target_snapshot_id,
        summaries,
        nodes,
        contributions,
        coverage,
        boundary,
        "",
    )
    return StorageGrowthResult(
        GROWTH_SCHEMA_VERSION,
        GROWTH_ALGORITHM,
        GROWTH_ALGORITHM_VERSION,
        base_snapshot_id,
        target_snapshot_id,
        summaries,
        nodes,
        contributions,
        coverage,
        boundary,
        sha256(canonical_storage_growth(provisional)).hexdigest(),
    )


def compute_snapshot_growth(base_snapshot: Snapshot, target_snapshot: Snapshot) -> StorageGrowthResult:
    """Compute one pure directional Path View growth result from valid facts."""
    _validate_pair(base_snapshot, target_snapshot)
    try:
        base_structure = compute_snapshot_structure(base_snapshot)
        target_structure = compute_snapshot_structure(target_snapshot)
    except StructureError as error:
        raise GrowthError("GROWTH_INVALID: Snapshot structure is invalid") from error

    base_regular = _regular_entries(base_snapshot)
    target_regular = _regular_entries(target_snapshot)
    located = tuple(
        _LocatedContribution(location[0], location[1], _classify(base_regular.get(location), target_regular.get(location)))
        for location in sorted(set(base_regular) | set(target_regular), key=_location_key)
    )
    contributions = tuple(item.contribution for item in located)
    scope_ids = tuple(sorted(set(base_snapshot.scope_ids) | set(target_snapshot.scope_ids)))
    nodes = tuple(
        node
        for scope_id in scope_ids
        for node in _scope_nodes(base_structure, target_structure, scope_id, located)
    )
    by_node = {(node.scope_id, node.relative_directory_path): node for node in nodes}
    summaries = tuple(
        ScopeGrowthSummary(
            base_snapshot.snapshot_id,
            target_snapshot.snapshot_id,
            scope_id,
            by_node[(scope_id, ".")].growth_node_id,
            by_node[(scope_id, ".")].recursive_base_known_logical_bytes,
            by_node[(scope_id, ".")].recursive_target_known_logical_bytes,
            by_node[(scope_id, ".")].recursive_known_net_logical_delta,
            by_node[(scope_id, ".")].recursive_added_logical_bytes,
            by_node[(scope_id, ".")].recursive_added_location_count,
            by_node[(scope_id, ".")].recursive_removed_logical_bytes,
            by_node[(scope_id, ".")].recursive_removed_location_count,
            by_node[(scope_id, ".")].recursive_same_location_increase_bytes,
            by_node[(scope_id, ".")].recursive_same_location_increase_count,
            by_node[(scope_id, ".")].recursive_same_location_decrease_bytes,
            by_node[(scope_id, ".")].recursive_same_location_decrease_count,
            by_node[(scope_id, ".")].recursive_same_location_unchanged_count,
            by_node[(scope_id, ".")].recursive_unknown_size_contribution_count,
            by_node[(scope_id, ".")].decomposition_complete,
        )
        for scope_id in scope_ids
    )
    coverage = _coverage(
        base_snapshot,
        target_snapshot,
        base_structure,
        target_structure,
        base_regular,
        target_regular,
        located,
    )
    if sum(item.known_net_logical_delta for item in summaries) != coverage.known_net_logical_delta:
        raise GrowthError("GROWTH_INVALID: global growth aggregate mismatch")
    if coverage.decomposition_complete and coverage.known_net_logical_delta != (
        target_structure.coverage.known_logical_bytes - base_structure.coverage.known_logical_bytes
    ):
        raise GrowthError("GROWTH_INVALID: global target-minus-base identity mismatch")
    return _result(
        base_snapshot.snapshot_id,
        target_snapshot.snapshot_id,
        summaries,
        nodes,
        contributions,
        coverage,
    )


def compute_verified_snapshot_growth(
    config: StewardConfig, base_snapshot_id: str, target_snapshot_id: str
) -> StorageGrowthResult:
    """Repository-verify and analyze one explicit directional Snapshot pair."""
    if base_snapshot_id == target_snapshot_id:
        raise GrowthError("GROWTH_INVALID: base and target Snapshot IDs must be distinct")
    try:
        base_verification = verify_snapshot(config, base_snapshot_id)
        target_verification = verify_snapshot(config, target_snapshot_id)
    except Exception as error:
        raise GrowthError("GROWTH_INVALID: both Snapshot IDs must be available") from error
    invalid = tuple(
        snapshot_id
        for snapshot_id, verification in (
            (base_snapshot_id, base_verification),
            (target_snapshot_id, target_verification),
        )
        if verification.status != "VALID"
    )
    if invalid:
        raise GrowthError(
            "GROWTH_INVALID: growth analysis requires VALID Snapshot Evidence: " + ", ".join(invalid)
        )
    try:
        base = get_snapshot(config, base_snapshot_id)
        target = get_snapshot(config, target_snapshot_id)
    except Exception as error:
        raise GrowthError("GROWTH_INVALID: both Snapshot IDs must be available") from error
    return compute_snapshot_growth(base, target)
