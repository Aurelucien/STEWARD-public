"""Read-only filtering and pagination for verified cross-Snapshot relations."""

from __future__ import annotations

from .errors import RelationError
from .models import (
    RelationKind,
    RelationQueryResult,
    StewardConfig,
)
from .snapshot_relations import compute_verified_snapshot_relations


DEFAULT_RELATION_QUERY_LIMIT = 100
MAX_RELATION_QUERY_LIMIT = 1_000


def _validate_page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RELATION_QUERY_LIMIT:
        raise RelationError(
            "RELATION_INVALID: limit must be an integer from 1 through 1000"
        )
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise RelationError("RELATION_INVALID: offset must be a non-negative integer")


def query_verified_snapshot_relations(
    config: StewardConfig,
    base_snapshot_id: str,
    target_snapshot_id: str,
    *,
    kind: RelationKind | None = None,
    limit: int = DEFAULT_RELATION_QUERY_LIMIT,
    offset: int = 0,
) -> RelationQueryResult:
    """Return one canonical RelationItem page without persisting a result.

    The complete RelationSet is always verified and computed before filtering
    and pagination.  Its digest therefore always identifies the unfiltered,
    complete result rather than a query view.
    """
    _validate_page(limit, offset)
    if kind is not None and not isinstance(kind, RelationKind):
        raise RelationError("RELATION_INVALID: kind filter is unsupported")
    relation_set = compute_verified_snapshot_relations(
        config, base_snapshot_id, target_snapshot_id
    )
    selected = tuple(
        item
        for item in relation_set.relations
        if kind is None or item.kind == kind
    )
    page = selected[offset : offset + limit]
    group_ids = {item.ambiguity_group_id for item in page if item.ambiguity_group_id is not None}
    groups = tuple(
        group
        for group in relation_set.ambiguity_groups
        if group.ambiguity_group_id in group_ids
    )
    has_more = offset + len(page) < len(selected)
    return RelationQueryResult(
        relation_set.relation_schema_version,
        relation_set.algorithm,
        relation_set.algorithm_version,
        relation_set.base_snapshot_id,
        relation_set.target_snapshot_id,
        relation_set.relation_set_digest,
        len(relation_set.relations),
        len(selected),
        len(page),
        kind,
        page,
        groups,
        limit,
        offset,
        has_more,
        offset + len(page) if has_more else None,
    )
