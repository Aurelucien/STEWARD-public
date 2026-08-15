"""Deterministic, on-demand Path View structure analysis for one Snapshot."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from hashlib import sha256

from .errors import StructureError
from .evidence import canonical_json
from .models import (
    DuplicateStorageKnowledgeStatus,
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemObjectType,
    FilesystemObservationStatus,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    PathAggregateNode,
    ScopeStructureSummary,
    SnapshotEntryReference,
    StorageStructureResult,
    StructureCoverageSummary,
    StructureLimitation,
    StructurePhysicalBoundary,
    StewardConfig,
)
from .snapshots import get_snapshot, verify_snapshot


STRUCTURE_SCHEMA_VERSION = 1
STRUCTURE_ALGORITHM = "storage_structure"
STRUCTURE_ALGORITHM_VERSION = 1
STRUCTURE_DIGEST_DOMAIN = "local_steward.storage_structure.v1"

Entry = FilesystemEntry | FilesystemEntryV2
ObjectHint = tuple[int, int]


@dataclass(slots=True)
class _NodeBuilder:
    scope_id: str
    path: str
    observed_directory_entry: bool = False
    direct_regular_file_count: int = 0
    direct_known_logical_bytes: int = 0
    direct_unknown_size_regular_count: int = 0
    direct_directory_count: int = 0
    direct_symlink_count: int = 0
    direct_special_object_count: int = 0
    children: set[str] = field(default_factory=set)


def _reference(entry: Entry) -> SnapshotEntryReference:
    return SnapshotEntryReference(entry.snapshot_id, entry.scope_id, entry.relative_path)


def _reference_key(reference: SnapshotEntryReference) -> tuple[str, str, bytes]:
    return (
        reference.snapshot_id,
        reference.scope_id,
        reference.relative_path.encode("utf-8", "surrogateescape"),
    )


def _entry_key(entry: Entry) -> tuple[str, bytes]:
    return (entry.scope_id, entry.relative_path.encode("utf-8", "surrogateescape"))


def _reference_data(reference: SnapshotEntryReference) -> dict[str, str]:
    return {
        "snapshot_id": reference.snapshot_id,
        "scope_id": reference.scope_id,
        "relative_path": reference.relative_path,
    }


def _node_id(snapshot_id: str, scope_id: str, path: str) -> str:
    return sha256(
        canonical_json(
            {
                "structure_schema_version": STRUCTURE_SCHEMA_VERSION,
                "algorithm": STRUCTURE_ALGORITHM,
                "algorithm_version": STRUCTURE_ALGORITHM_VERSION,
                "snapshot_id": snapshot_id,
                "scope_id": scope_id,
                "relative_directory_path": path,
            }
        )
    ).hexdigest()


def _node_key(node: PathAggregateNode) -> tuple[str, bytes]:
    return (node.scope_id, node.relative_directory_path.encode("utf-8", "surrogateescape"))


def _valid_path(path: str) -> tuple[str, ...]:
    if path == ".":
        return ()
    if not path or path.startswith("/"):
        raise StructureError("STRUCTURE_INVALID: relative path must be scope-relative")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise StructureError("STRUCTURE_INVALID: relative path hierarchy is invalid")
    return parts


def _path(parts: tuple[str, ...]) -> str:
    return "/".join(parts) if parts else "."


def _parent(path: str) -> str:
    parts = _valid_path(path)
    return _path(parts[:-1])


def _known_regular_size(entry: Entry) -> int | None:
    if entry.object_type != FilesystemObjectType.REGULAR_FILE:
        return None
    if entry.observation_status != FilesystemObservationStatus.OBSERVED:
        return None
    if entry.size_bytes is None:
        return None
    if entry.size_bytes < 0:
        raise StructureError("STRUCTURE_INVALID: regular-file size must be non-negative")
    return entry.size_bytes


def _is_special(entry: Entry) -> bool:
    return entry.object_type not in {
        FilesystemObjectType.REGULAR_FILE,
        FilesystemObjectType.DIRECTORY,
        FilesystemObjectType.SYMLINK,
    }


def _node_data(node: PathAggregateNode) -> dict[str, object]:
    return {
        "path_node_id": node.path_node_id,
        "snapshot_id": node.snapshot_id,
        "scope_id": node.scope_id,
        "relative_directory_path": node.relative_directory_path,
        "observed_directory_entry": node.observed_directory_entry,
        "direct_regular_file_count": node.direct_regular_file_count,
        "recursive_regular_file_count": node.recursive_regular_file_count,
        "direct_known_logical_bytes": node.direct_known_logical_bytes,
        "recursive_known_logical_bytes": node.recursive_known_logical_bytes,
        "direct_unknown_size_regular_count": node.direct_unknown_size_regular_count,
        "recursive_unknown_size_regular_count": node.recursive_unknown_size_regular_count,
        "direct_directory_count": node.direct_directory_count,
        "recursive_directory_count": node.recursive_directory_count,
        "direct_symlink_count": node.direct_symlink_count,
        "recursive_symlink_count": node.recursive_symlink_count,
        "direct_special_object_count": node.direct_special_object_count,
        "recursive_special_object_count": node.recursive_special_object_count,
    }


def _scope_data(summary: ScopeStructureSummary) -> dict[str, object]:
    return {
        "snapshot_id": summary.snapshot_id,
        "scope_id": summary.scope_id,
        "root_node_id": summary.root_node_id,
        "recursive_regular_file_count": summary.recursive_regular_file_count,
        "recursive_known_logical_bytes": summary.recursive_known_logical_bytes,
        "recursive_unknown_size_regular_count": summary.recursive_unknown_size_regular_count,
        "recursive_directory_count": summary.recursive_directory_count,
        "recursive_symlink_count": summary.recursive_symlink_count,
        "recursive_special_object_count": summary.recursive_special_object_count,
    }


def _coverage_data(coverage: StructureCoverageSummary) -> dict[str, object]:
    return {
        "total_entry_count": coverage.total_entry_count,
        "regular_file_entry_count": coverage.regular_file_entry_count,
        "known_size_regular_file_count": coverage.known_size_regular_file_count,
        "unknown_size_regular_file_count": coverage.unknown_size_regular_file_count,
        "known_logical_bytes": coverage.known_logical_bytes,
        "directory_entry_count": coverage.directory_entry_count,
        "symlink_entry_count": coverage.symlink_entry_count,
        "special_object_entry_count": coverage.special_object_entry_count,
        "excluded_entry_count": coverage.excluded_entry_count,
        "metadata_failed_entry_count": coverage.metadata_failed_entry_count,
        "scope_overlap_object_hint_count": coverage.scope_overlap_object_hint_count,
        "repeated_known_object_hint_path_count": coverage.repeated_known_object_hint_path_count,
        "object_hint_unavailable_entry_count": coverage.object_hint_unavailable_entry_count,
        "complete": coverage.complete,
    }


def _limitation_data(limitation: StructureLimitation) -> dict[str, object]:
    return {
        "code": limitation.code,
        "entries": [_reference_data(entry) for entry in limitation.entries],
    }


def _physical_data(boundary: StructurePhysicalBoundary) -> dict[str, object]:
    return {
        "allocation_status": boundary.allocation_status.value,
        "physical_block_sharing_status": boundary.physical_block_sharing_status.value,
        "reclaimable_bytes": boundary.reclaimable_bytes,
        "reclaimable_status": boundary.reclaimable_status.value,
        "object_aware_capacity_status": boundary.object_aware_capacity_status.value,
    }


def canonical_storage_structure(result: StorageStructureResult) -> bytes:
    """Return complete canonical bytes excluding the derived structure digest."""
    return canonical_json(
        {
            "domain": STRUCTURE_DIGEST_DOMAIN,
            "structure_schema_version": result.structure_schema_version,
            "algorithm": result.algorithm,
            "algorithm_version": result.algorithm_version,
            "snapshot_id": result.snapshot_id,
            "scope_summaries": [_scope_data(item) for item in result.scope_summaries],
            "path_nodes": [_node_data(item) for item in result.path_nodes],
            "coverage": _coverage_data(result.coverage),
            "limitations": [_limitation_data(item) for item in result.limitations],
            "physical_boundary": _physical_data(result.physical_boundary),
        }
    )


def _validate_entries(entries: tuple[Entry, ...], snapshot_id: str, scope_ids: tuple[str, ...]) -> None:
    locations: set[tuple[str, str]] = set()
    entry_by_location: dict[tuple[str, str], Entry] = {}
    known_scopes = set(scope_ids)
    for entry in entries:
        if entry.snapshot_id != snapshot_id:
            raise StructureError("STRUCTURE_INVALID: entry snapshot identity does not match input")
        if entry.scope_id not in known_scopes:
            raise StructureError("STRUCTURE_INVALID: entry scope is not declared by Snapshot")
        _valid_path(entry.relative_path)
        location = (entry.scope_id, entry.relative_path)
        if location in locations:
            raise StructureError("STRUCTURE_INVALID: duplicate scoped Entry reference")
        locations.add(location)
        entry_by_location[location] = entry
        if entry.size_bytes is not None and entry.size_bytes < 0:
            raise StructureError("STRUCTURE_INVALID: Entry size must be non-negative")
    for (scope_id, path), entry in entry_by_location.items():
        for length in range(1, len(_valid_path(path))):
            prefix = _path(_valid_path(path)[:length])
            ancestor = entry_by_location.get((scope_id, prefix))
            if ancestor is not None and ancestor.object_type != FilesystemObjectType.DIRECTORY:
                raise StructureError("STRUCTURE_INVALID: non-directory Entry has descendants")
        if path == "." and entry.object_type != FilesystemObjectType.DIRECTORY:
            raise StructureError("STRUCTURE_INVALID: scope root Entry must be a directory")


def _ensure_node(nodes: dict[str, _NodeBuilder], scope_id: str, path: str) -> _NodeBuilder:
    node = nodes.get(path)
    if node is None:
        node = _NodeBuilder(scope_id, path)
        nodes[path] = node
        if path != ".":
            nodes[_parent(path)].children.add(path)
    return node


def _scope_nodes(snapshot_id: str, scope_id: str, entries: Iterable[Entry]) -> tuple[PathAggregateNode, ...]:
    builders: dict[str, _NodeBuilder] = {".": _NodeBuilder(scope_id, ".")}
    values = tuple(sorted(entries, key=_entry_key))
    for entry in values:
        if entry.excluded:
            continue
        parts = _valid_path(entry.relative_path)
        for length in range(len(parts)):
            _ensure_node(builders, scope_id, _path(parts[:length]))
        if entry.object_type == FilesystemObjectType.DIRECTORY:
            node = _ensure_node(builders, scope_id, entry.relative_path)
            node.observed_directory_entry = True
            # Directory Entries are self facts of their aggregate node.  This
            # preserves recursive = direct + child-recursive for every node.
            node.direct_directory_count += 1
            continue
        parent = _ensure_node(builders, scope_id, _path(parts[:-1]))
        if entry.object_type == FilesystemObjectType.REGULAR_FILE:
            parent.direct_regular_file_count += 1
            size = _known_regular_size(entry)
            if size is None:
                parent.direct_unknown_size_regular_count += 1
            else:
                parent.direct_known_logical_bytes += size
        elif entry.object_type == FilesystemObjectType.SYMLINK:
            parent.direct_symlink_count += 1
        elif _is_special(entry):
            parent.direct_special_object_count += 1

    resolved: dict[str, PathAggregateNode] = {}

    def build(path: str) -> PathAggregateNode:
        existing = resolved.get(path)
        if existing is not None:
            return existing
        builder = builders[path]
        children = tuple(
            build(child)
            for child in sorted(builder.children, key=lambda item: item.encode("utf-8", "surrogateescape"))
        )
        node = PathAggregateNode(
            _node_id(snapshot_id, scope_id, path),
            snapshot_id,
            scope_id,
            path,
            builder.observed_directory_entry,
            builder.direct_regular_file_count,
            builder.direct_regular_file_count + sum(item.recursive_regular_file_count for item in children),
            builder.direct_known_logical_bytes,
            builder.direct_known_logical_bytes + sum(item.recursive_known_logical_bytes for item in children),
            builder.direct_unknown_size_regular_count,
            builder.direct_unknown_size_regular_count + sum(item.recursive_unknown_size_regular_count for item in children),
            builder.direct_directory_count,
            builder.direct_directory_count + sum(item.recursive_directory_count for item in children),
            builder.direct_symlink_count,
            builder.direct_symlink_count + sum(item.recursive_symlink_count for item in children),
            builder.direct_special_object_count,
            builder.direct_special_object_count + sum(item.recursive_special_object_count for item in children),
        )
        resolved[path] = node
        return node

    build(".")
    return tuple(sorted(resolved.values(), key=_node_key))


def _coverage(entries: tuple[Entry, ...]) -> tuple[StructureCoverageSummary, tuple[StructureLimitation, ...]]:
    regular = [entry for entry in entries if entry.object_type == FilesystemObjectType.REGULAR_FILE]
    active = [entry for entry in entries if not entry.excluded]
    known_regular = [entry for entry in active if _known_regular_size(entry) is not None]
    unknown_regular = [
        entry
        for entry in active
        if entry.object_type == FilesystemObjectType.REGULAR_FILE and _known_regular_size(entry) is None
    ]
    metadata_failed = [
        entry for entry in active if entry.observation_status != FilesystemObservationStatus.OBSERVED
    ]
    hint_members: dict[ObjectHint, list[Entry]] = defaultdict(list)
    unavailable_hints: list[Entry] = []
    for entry in active:
        if entry.object_type != FilesystemObjectType.REGULAR_FILE:
            continue
        if entry.device_id is None or entry.inode is None:
            unavailable_hints.append(entry)
        else:
            hint_members[(entry.device_id, entry.inode)].append(entry)
    repeated = [members for members in hint_members.values() if len(members) >= 2]
    overlap = [members for members in repeated if len({entry.scope_id for entry in members}) >= 2]
    limitations = [
        StructureLimitation(
            "SCOPE_OVERLAP_OBJECT_HINT",
            tuple(sorted((_reference(entry) for entry in members), key=_reference_key)),
        )
        for members in overlap
    ]
    if unavailable_hints:
        limitations.append(
            StructureLimitation(
                "OBJECT_HINT_UNAVAILABLE",
                tuple(sorted((_reference(entry) for entry in unavailable_hints), key=_reference_key)),
            )
        )
    ordered_limitations = tuple(
        sorted(
            limitations,
            key=lambda item: (item.code, tuple(_reference_key(entry) for entry in item.entries)),
        )
    )
    return (
        StructureCoverageSummary(
            len(entries),
            len(regular),
            len(known_regular),
            len(unknown_regular),
            sum(_known_regular_size(entry) or 0 for entry in known_regular),
            sum(entry.object_type == FilesystemObjectType.DIRECTORY for entry in entries),
            sum(entry.object_type == FilesystemObjectType.SYMLINK for entry in entries),
            sum(_is_special(entry) for entry in entries),
            sum(entry.excluded for entry in entries),
            len(metadata_failed),
            len(overlap),
            sum(len(members) for members in repeated),
            len(unavailable_hints),
            not unknown_regular and not metadata_failed,
        ),
        ordered_limitations,
    )


def _result(
    snapshot_id: str,
    scope_summaries: tuple[ScopeStructureSummary, ...],
    nodes: tuple[PathAggregateNode, ...],
    coverage: StructureCoverageSummary,
    limitations: tuple[StructureLimitation, ...],
) -> StorageStructureResult:
    boundary = StructurePhysicalBoundary(
        DuplicateStorageKnowledgeStatus.UNKNOWN,
        DuplicateStorageKnowledgeStatus.UNKNOWN,
        None,
        DuplicateStorageKnowledgeStatus.UNKNOWN,
        DuplicateStorageKnowledgeStatus.UNKNOWN,
    )
    provisional = StorageStructureResult(
        STRUCTURE_SCHEMA_VERSION,
        STRUCTURE_ALGORITHM,
        STRUCTURE_ALGORITHM_VERSION,
        snapshot_id,
        scope_summaries,
        nodes,
        coverage,
        limitations,
        boundary,
        "",
    )
    return StorageStructureResult(
        STRUCTURE_SCHEMA_VERSION,
        STRUCTURE_ALGORITHM,
        STRUCTURE_ALGORITHM_VERSION,
        snapshot_id,
        scope_summaries,
        nodes,
        coverage,
        limitations,
        boundary,
        sha256(canonical_storage_structure(provisional)).hexdigest(),
    )


def compute_snapshot_structure(
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
) -> StorageStructureResult:
    """Compute an in-memory Path View from one already-validated Snapshot fact."""
    entries: tuple[Entry, ...] = snapshot.entries
    _validate_entries(entries, snapshot.snapshot_id, snapshot.scope_ids)
    by_scope: dict[str, list[Entry]] = {scope_id: [] for scope_id in snapshot.scope_ids}
    for entry in entries:
        by_scope[entry.scope_id].append(entry)
    nodes = tuple(
        node
        for scope_id in sorted(by_scope)
        for node in _scope_nodes(snapshot.snapshot_id, scope_id, by_scope[scope_id])
    )
    by_node = {(node.scope_id, node.relative_directory_path): node for node in nodes}
    summaries = tuple(
        ScopeStructureSummary(
            snapshot.snapshot_id,
            scope_id,
            by_node[(scope_id, ".")].path_node_id,
            by_node[(scope_id, ".")].recursive_regular_file_count,
            by_node[(scope_id, ".")].recursive_known_logical_bytes,
            by_node[(scope_id, ".")].recursive_unknown_size_regular_count,
            by_node[(scope_id, ".")].recursive_directory_count,
            by_node[(scope_id, ".")].recursive_symlink_count,
            by_node[(scope_id, ".")].recursive_special_object_count,
        )
        for scope_id in sorted(by_scope)
    )
    coverage, limitations = _coverage(entries)
    if sum(item.recursive_known_logical_bytes for item in summaries) != coverage.known_logical_bytes:
        raise StructureError("STRUCTURE_INVALID: scope summary logical-byte aggregate mismatch")
    return _result(snapshot.snapshot_id, summaries, nodes, coverage, limitations)


def compute_verified_snapshot_structure(
    config: StewardConfig, snapshot_id: str
) -> StorageStructureResult:
    """Repository-verify and analyze one explicit persisted Snapshot Evidence fact."""
    try:
        verification = verify_snapshot(config, snapshot_id)
    except Exception as error:
        raise StructureError("STRUCTURE_INVALID: Snapshot ID must be available") from error
    if verification.status != "VALID":
        raise StructureError(
            "STRUCTURE_INVALID: structure analysis requires VALID Snapshot Evidence: " + snapshot_id
        )
    try:
        snapshot = get_snapshot(config, snapshot_id)
    except Exception as error:
        raise StructureError("STRUCTURE_INVALID: Snapshot ID must be available") from error
    return compute_snapshot_structure(snapshot)
