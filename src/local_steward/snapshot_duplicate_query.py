"""Read-only filtering and pagination for verified duplicate/storage analysis."""

from __future__ import annotations

from .duplicate_analysis import compute_verified_snapshot_duplicate_analysis
from .errors import DuplicateAnalysisError
from .models import DuplicateAnalysisQueryResult, StewardConfig


DEFAULT_DUPLICATE_QUERY_LIMIT = 100
MAX_DUPLICATE_QUERY_LIMIT = 1_000


def _validate_page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_DUPLICATE_QUERY_LIMIT:
        raise DuplicateAnalysisError(
            "DUPLICATE_INVALID: limit must be an integer from 1 through 1000"
        )
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise DuplicateAnalysisError("DUPLICATE_INVALID: offset must be a non-negative integer")


def query_verified_snapshot_duplicates(
    config: StewardConfig,
    snapshot_id: str,
    *,
    only_exact: bool = False,
    limit: int = DEFAULT_DUPLICATE_QUERY_LIMIT,
    offset: int = 0,
) -> DuplicateAnalysisQueryResult:
    """Return one canonical duplicate-group page without persisting an analysis.

    Complete verification and analysis always precede filtering and pagination.
    The result digest consequently continues to identify the complete unfiltered
    analysis rather than this query view.
    """
    _validate_page(limit, offset)
    if not isinstance(only_exact, bool):
        raise DuplicateAnalysisError("DUPLICATE_INVALID: only_exact must be a boolean")
    analysis = compute_verified_snapshot_duplicate_analysis(config, snapshot_id)
    selected = tuple(
        group
        for group in analysis.payload_equality_groups
        if not only_exact or group.is_exact_duplicate
    )
    page = selected[offset : offset + limit]
    has_more = offset + len(page) < len(selected)
    return DuplicateAnalysisQueryResult(
        analysis.analysis_schema_version,
        analysis.algorithm,
        analysis.algorithm_version,
        analysis.snapshot_id,
        analysis.analysis_digest,
        len(analysis.payload_equality_groups),
        len(selected),
        len(page),
        only_exact,
        page,
        analysis.hard_link_alias_sets,
        analysis.coverage,
        analysis.physical_storage,
        analysis.integrity_conflicts,
        limit,
        offset,
        has_more,
        offset + len(page) if has_more else None,
    )
