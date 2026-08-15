"""Read-only views over complete storage structure and growth results."""

from __future__ import annotations

from typing import TypeVar, cast

from .errors import GrowthError, StructureError
from .models import (
    GrowthQueryResult,
    GrowthRank,
    PathAggregateNode,
    PathGrowthNode,
    PathViewRoot,
    StewardConfig,
    StructureQueryResult,
    StructureRank,
)
from .storage_growth import compute_verified_snapshot_growth
from .storage_structure import compute_verified_snapshot_structure


DEFAULT_STORAGE_QUERY_LIMIT = 100
MAX_STORAGE_QUERY_LIMIT = 1_000

StructureNode = PathAggregateNode
GrowthNode = PathGrowthNode
Node = TypeVar("Node")


def _canonical_key(node: StructureNode | GrowthNode) -> tuple[str, bytes]:
    return (node.scope_id, node.relative_directory_path.encode("utf-8", "surrogateescape"))


def _validate_page(limit: int, offset: int, error: type[StructureError] | type[GrowthError]) -> None:
    code = error.code
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_STORAGE_QUERY_LIMIT:
        raise error(f"{code}: limit must be an integer from 1 through 1000")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise error(f"{code}: offset must be a non-negative integer")


def _validate_view_options(
    depth: int | None,
    min_bytes: int | None,
    rank: StructureRank | GrowthRank | None,
    error: type[StructureError] | type[GrowthError],
) -> None:
    code = error.code
    if depth is not None and (isinstance(depth, bool) or not isinstance(depth, int) or depth < 0):
        raise error(f"{code}: depth must be a non-negative integer")
    if min_bytes is not None and (
        isinstance(min_bytes, bool) or not isinstance(min_bytes, int) or min_bytes < 0
    ):
        raise error(f"{code}: min-bytes must be a non-negative integer")
    if min_bytes is not None and rank is None:
        raise error(f"{code}: min-bytes requires a rank")


def _path_parts(path: str, error: type[StructureError] | type[GrowthError]) -> tuple[str, ...]:
    if not isinstance(path, str):
        raise error(f"{error.code}: path-prefix must be a scoped relative path")
    if path == ".":
        return ()
    if not path or path.startswith("/"):
        raise error(f"{error.code}: path-prefix must be a scoped relative path")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise error(f"{error.code}: path-prefix hierarchy is invalid")
    return parts


def _is_descendant(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root + "/")


def _relative_depth(path: str, root: str) -> int:
    path_depth = len(_path_parts(path, StructureError))
    root_depth = len(_path_parts(root, StructureError))
    return path_depth - root_depth


def _selected_scopes(
    nodes: tuple[StructureNode, ...] | tuple[GrowthNode, ...],
    scope: str | None,
    error: type[StructureError] | type[GrowthError],
) -> tuple[str, ...]:
    available = tuple(sorted({node.scope_id for node in nodes}))
    if scope is None:
        return available
    if not isinstance(scope, str) or scope not in available:
        raise error(f"{error.code}: scope is not present in the complete result")
    return (scope,)


def _effective_roots(
    nodes: tuple[StructureNode, ...] | tuple[GrowthNode, ...],
    scopes: tuple[str, ...],
    path_prefix: str | None,
    error: type[StructureError] | type[GrowthError],
) -> tuple[PathViewRoot, ...]:
    path = "." if path_prefix is None else path_prefix
    _path_parts(path, error)
    locations = {(node.scope_id, node.relative_directory_path) for node in nodes}
    missing = [scope_id for scope_id in scopes if (scope_id, path) not in locations]
    if missing:
        raise error(f"{error.code}: path-prefix is not present in the selected scope")
    return tuple(PathViewRoot(scope_id, path) for scope_id in scopes)


def _depth_nodes(
    nodes: tuple[StructureNode, ...] | tuple[GrowthNode, ...],
    roots: tuple[PathViewRoot, ...],
    depth: int | None,
) -> tuple[StructureNode | GrowthNode, ...]:
    root_by_scope = {root.scope_id: root.relative_directory_path for root in roots}
    return tuple(
        node
        for node in nodes
        if node.scope_id in root_by_scope
        and _is_descendant(node.relative_directory_path, root_by_scope[node.scope_id])
        and (
            depth is None
            or _relative_depth(node.relative_directory_path, root_by_scope[node.scope_id]) <= depth
        )
    )


def _page(nodes: tuple[Node, ...], limit: int, offset: int) -> tuple[tuple[Node, ...], bool, int | None]:
    page = nodes[offset : offset + limit]
    has_more = offset + len(page) < len(nodes)
    return page, has_more, offset + len(page) if has_more else None


def _structure_nodes(
    candidates: tuple[StructureNode, ...], rank: StructureRank | None, min_bytes: int | None
) -> tuple[StructureNode, ...]:
    if rank is None:
        return tuple(sorted(candidates, key=_canonical_key))
    eligible = tuple(
        node
        for node in candidates
        if node.relative_directory_path != "." and node.recursive_known_logical_bytes > 0
    )
    if min_bytes is not None:
        eligible = tuple(node for node in eligible if node.recursive_known_logical_bytes >= min_bytes)
    return tuple(
        sorted(
            eligible,
            key=lambda node: (-node.recursive_known_logical_bytes, *_canonical_key(node)),
        )
    )


def _growth_metric(node: GrowthNode, rank: GrowthRank) -> int | None:
    if rank == GrowthRank.NET_GROWTH:
        return node.recursive_known_net_logical_delta if node.recursive_known_net_logical_delta > 0 else None
    if rank == GrowthRank.NET_SHRINK:
        return -node.recursive_known_net_logical_delta if node.recursive_known_net_logical_delta < 0 else None
    if rank == GrowthRank.ADDED:
        return node.recursive_added_logical_bytes if node.recursive_added_logical_bytes > 0 else None
    return node.recursive_removed_logical_bytes if node.recursive_removed_logical_bytes > 0 else None


def _growth_nodes(
    candidates: tuple[GrowthNode, ...], rank: GrowthRank | None, min_bytes: int | None
) -> tuple[GrowthNode, ...]:
    if rank is None:
        return tuple(sorted(candidates, key=_canonical_key))
    eligible = tuple(
        (node, metric)
        for node in candidates
        if node.relative_directory_path != "."
        for metric in (_growth_metric(node, rank),)
        if metric is not None and (min_bytes is None or metric >= min_bytes)
    )
    ordered = sorted(eligible, key=lambda item: (-item[1], *_canonical_key(item[0])))
    return tuple(item[0] for item in ordered)


def query_verified_snapshot_structure(
    config: StewardConfig,
    snapshot_id: str,
    *,
    scope: str | None = None,
    path_prefix: str | None = None,
    depth: int | None = None,
    rank: StructureRank | None = None,
    min_bytes: int | None = None,
    limit: int = DEFAULT_STORAGE_QUERY_LIMIT,
    offset: int = 0,
) -> StructureQueryResult:
    """Compute once, then return a read-only deterministic structure view."""
    _validate_page(limit, offset, StructureError)
    _validate_view_options(depth, min_bytes, rank, StructureError)
    if rank is not None and not isinstance(rank, StructureRank):
        raise StructureError("STRUCTURE_INVALID: unsupported structure rank")
    result = compute_verified_snapshot_structure(config, snapshot_id)
    scopes = _selected_scopes(result.path_nodes, scope, StructureError)
    roots = _effective_roots(result.path_nodes, scopes, path_prefix, StructureError)
    candidates = cast(tuple[StructureNode, ...], _depth_nodes(result.path_nodes, roots, depth))
    selected = _structure_nodes(candidates, rank, min_bytes)
    page, has_more, next_offset = _page(selected, limit, offset)
    return StructureQueryResult(
        result.structure_schema_version,
        result.algorithm,
        result.algorithm_version,
        result.snapshot_id,
        result.structure_digest,
        len(result.path_nodes),
        len(selected),
        len(page),
        scope,
        path_prefix,
        depth,
        rank,
        min_bytes,
        roots,
        tuple(page),
        result.scope_summaries,
        result.coverage,
        result.limitations,
        result.physical_boundary,
        limit,
        offset,
        has_more,
        next_offset,
    )


def query_verified_snapshot_growth(
    config: StewardConfig,
    base_snapshot_id: str,
    target_snapshot_id: str,
    *,
    scope: str | None = None,
    path_prefix: str | None = None,
    depth: int | None = None,
    rank: GrowthRank | None = None,
    min_bytes: int | None = None,
    limit: int = DEFAULT_STORAGE_QUERY_LIMIT,
    offset: int = 0,
) -> GrowthQueryResult:
    """Compute once, then return a read-only deterministic growth view."""
    _validate_page(limit, offset, GrowthError)
    _validate_view_options(depth, min_bytes, rank, GrowthError)
    if rank is not None and not isinstance(rank, GrowthRank):
        raise GrowthError("GROWTH_INVALID: unsupported growth rank")
    result = compute_verified_snapshot_growth(config, base_snapshot_id, target_snapshot_id)
    scopes = _selected_scopes(result.path_nodes, scope, GrowthError)
    roots = _effective_roots(result.path_nodes, scopes, path_prefix, GrowthError)
    candidates = cast(tuple[GrowthNode, ...], _depth_nodes(result.path_nodes, roots, depth))
    selected = _growth_nodes(candidates, rank, min_bytes)
    page, has_more, next_offset = _page(selected, limit, offset)
    return GrowthQueryResult(
        result.growth_schema_version,
        result.algorithm,
        result.algorithm_version,
        result.base_snapshot_id,
        result.target_snapshot_id,
        result.growth_digest,
        len(result.path_nodes),
        len(selected),
        len(page),
        scope,
        path_prefix,
        depth,
        rank,
        min_bytes,
        roots,
        tuple(page),
        result.scope_summaries,
        result.contributions,
        result.coverage,
        result.physical_boundary,
        limit,
        offset,
        has_more,
        next_offset,
    )
