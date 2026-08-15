"""Thin read-only wrappers over the stable Steward historical-query APIs."""

from __future__ import annotations

from time import monotonic_ns
from typing import Any, Callable, TypeVar

from ..errors import (
    DuplicateAnalysisError,
    GrowthError,
    RelationError,
    SnapshotBudgetError,
    SnapshotNotFoundError,
    SnapshotScopeError,
    StructureError,
)
from ..evidence import canonical_json
from ..models import (
    FilesystemObjectType,
    FilesystemObservationStatus,
    GrowthRank,
    RelationKind,
    StructureRank,
)
from ..observation_projection import (
    PairTrackingRequest,
    ProjectionPolicy,
    SnapshotDiagnosticRequest,
    build_pair_tracking_projection,
    build_snapshot_diagnostic_projection,
)
from ..snapshot_diff import compute_verified_snapshot_diff
from ..snapshot_duplicate_query import query_verified_snapshot_duplicates
from ..snapshot_relation_query import query_verified_snapshot_relations
from ..snapshots import (
    _snapshot_inventory_with_verification,
    _verified_snapshot_entries,
    list_snapshot_entries,
    list_snapshots,
    verify_snapshot,
)
from ..storage_query import query_verified_snapshot_growth, query_verified_snapshot_structure
from .models import (
    AgentToolEnvelope,
    AgentToolError,
    SourceKind,
    ToolExecutionContext,
    ToolResultStatus,
)
from .serialization import machine_result


T = TypeVar("T")


def _milliseconds(start_ns: int) -> int:
    return (monotonic_ns() - start_ns) // 1_000_000


def _verification(context: ToolExecutionContext, snapshot_id: str) -> Any:
    try:
        verification = verify_snapshot(context.config, snapshot_id)
    except SnapshotNotFoundError as error:
        raise AgentToolError("SNAPSHOT_NOT_FOUND", "the requested Snapshot does not exist") from error
    except Exception as error:
        raise AgentToolError("HISTORICAL_SOURCE_UNAVAILABLE", "Snapshot verification is unavailable") from error
    if verification.status != "VALID":
        raise AgentToolError("SNAPSHOT_NOT_VALID", "the requested Snapshot is not VALID")
    return verification


def _inventory_verification(context: ToolExecutionContext, snapshot_id: str) -> Any:
    """Inventory preserves INVALID facts without presenting them as usable inputs."""
    try:
        return verify_snapshot(context.config, snapshot_id)
    except Exception:
        return {"snapshot_id": snapshot_id, "status": "INVALID", "errors": [{"code": "HISTORICAL_SOURCE_UNAVAILABLE"}]}


def _validate_scope(context: ToolExecutionContext, snapshot_id: str, scope_id: str | None) -> None:
    if scope_id is None:
        return
    summaries = _safe_call(lambda: list_snapshots(context.config, limit=None))
    summary = next((item for item in summaries if item.snapshot_id == snapshot_id), None)
    if summary is None:
        raise AgentToolError("SNAPSHOT_NOT_FOUND", "the requested Snapshot does not exist")
    if not isinstance(scope_id, str) or scope_id not in summary.scope_ids:
        raise AgentToolError("INVALID_ARGUMENT", "scope_id is not present in the requested Snapshot")


def _validate_relative_path(path: str | None, *, field: str) -> None:
    if path is None:
        return
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise AgentToolError("INVALID_ARGUMENT", f"{field} must be a scoped relative path")
    if path == ".":
        return
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise AgentToolError("INVALID_ARGUMENT", f"{field} hierarchy is invalid")


def _safe_call(action: Callable[[], T]) -> T:
    try:
        return action()
    except AgentToolError:
        raise
    except SnapshotNotFoundError as error:
        raise AgentToolError("SNAPSHOT_NOT_FOUND", "the requested Snapshot does not exist") from error
    except (
        SnapshotBudgetError,
        SnapshotScopeError,
        StructureError,
        GrowthError,
        DuplicateAnalysisError,
        RelationError,
        ValueError,
    ) as error:
        raise AgentToolError("INVALID_ARGUMENT", "the historical query arguments are invalid") from error
    except Exception as error:
        raise AgentToolError("HISTORICAL_SOURCE_UNAVAILABLE", "the historical query is unavailable") from error


def _effective_page_limit(
    context: ToolExecutionContext, limit: int, offset: int
) -> tuple[int, bool]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
        raise AgentToolError("INVALID_ARGUMENT", "limit must be an integer from 1 through 1000")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise AgentToolError("INVALID_ARGUMENT", "offset must be a non-negative integer")
    effective, budget_truncated = context.budget.effective_limit(limit)
    assert effective is not None
    return effective, budget_truncated


def _envelope(
    context: ToolExecutionContext,
    *,
    tool_name: str,
    source_kind: SourceKind,
    snapshot_ids: tuple[str, ...],
    scope_id: str | None,
    result: Any,
    result_digest: str | None,
    examined: int | None,
    returned: int | None,
    limit: int | None,
    offset: int | None,
    truncated: bool,
    budget_truncated: bool,
    start_ns: int,
) -> AgentToolEnvelope:
    value = machine_result(result)
    serialized_bytes = len(canonical_json(value))
    elapsed_ms = _milliseconds(start_ns)
    context.budget.consume_result(
        items=returned or 0, serialized_bytes=serialized_bytes, elapsed_ms=elapsed_ms
    )
    warnings = ("RESULT_TRUNCATED_BY_BUDGET",) if budget_truncated else ()
    return AgentToolEnvelope(
        tool_name,
        source_kind,
        snapshot_ids,
        scope_id,
        value,
        result_digest,
        examined,
        returned,
        serialized_bytes,
        elapsed_ms,
        truncated or budget_truncated,
        limit,
        offset,
        warnings,
        ToolResultStatus.PARTIAL_RESULT if truncated or budget_truncated else ToolResultStatus.COMPLETE,
        context.budget.report(),
    )


def _start(context: ToolExecutionContext, *, depth: int | None = None) -> int:
    context.budget.begin_call(depth=depth)
    return monotonic_ns()


def steward_list_snapshots(
    context: ToolExecutionContext, *, limit: int = 50, offset: int = 0
) -> AgentToolEnvelope:
    """List historical Snapshot inventory with explicit verification states."""
    effective, budget_truncated = _effective_page_limit(context, limit, offset)
    start = _start(context)
    classified = _safe_call(
        lambda: _snapshot_inventory_with_verification(
            context.config, limit=offset + effective + 1
        )
    )
    page = classified[offset : offset + effective]
    inventory = tuple(
        {"snapshot": item, "verification": verification}
        for item, verification in page
    )
    has_more = len(classified) > offset + len(page)
    result = {"snapshots": inventory, "has_more": has_more}
    return _envelope(
        context, tool_name="steward_list_snapshots", source_kind=SourceKind.HISTORICAL_SNAPSHOT,
        snapshot_ids=tuple(item.snapshot_id for item, _ in page), scope_id=None, result=result,
        result_digest=None, examined=len(classified), returned=len(page), limit=effective, offset=offset,
        truncated=has_more, budget_truncated=budget_truncated, start_ns=start,
    )


def steward_inspect_snapshot(
    context: ToolExecutionContext,
    snapshot_id: str,
    *,
    scope_id: str | None = None,
    object_type: FilesystemObjectType | None = None,
    observation_status: FilesystemObservationStatus | None = None,
    path_prefix: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AgentToolEnvelope:
    """Return exactly the stable flat SnapshotEntryPage; no depth semantics exist."""
    effective, budget_truncated = _effective_page_limit(context, limit, offset)
    _validate_relative_path(path_prefix, field="path_prefix")
    if object_type is not None and not isinstance(object_type, FilesystemObjectType):
        raise AgentToolError("INVALID_ARGUMENT", "object_type is unsupported")
    if observation_status is not None and not isinstance(observation_status, FilesystemObservationStatus):
        raise AgentToolError("INVALID_ARGUMENT", "observation_status is unsupported")
    start = _start(context)
    verification, _snapshot, page = _safe_call(
        lambda: _verified_snapshot_entries(
            context.config,
            snapshot_id,
            scope_id,
            object_type,
            observation_status,
            path_prefix,
            effective,
            offset,
        )
    )
    if verification.status != "VALID":
        raise AgentToolError("SNAPSHOT_NOT_VALID", "the requested Snapshot is not VALID")
    result = {"verification": verification, "page": page}
    return _envelope(
        context, tool_name="steward_inspect_snapshot", source_kind=SourceKind.HISTORICAL_SNAPSHOT,
        snapshot_ids=(snapshot_id,), scope_id=scope_id, result=result, result_digest=None,
        examined=None, returned=page.returned_count, limit=page.limit, offset=page.offset,
        truncated=page.has_more, budget_truncated=budget_truncated, start_ns=start,
    )


def steward_inspect_structure(
    context: ToolExecutionContext, snapshot_id: str, *, scope_id: str | None = None,
    path_prefix: str | None = None, depth: int | None = None, rank: StructureRank | None = None,
    min_bytes: int | None = None, limit: int = 100, offset: int = 0,
) -> AgentToolEnvelope:
    effective, budget_truncated = _effective_page_limit(context, limit, offset)
    start = _start(context, depth=depth)
    result = _safe_call(lambda: query_verified_snapshot_structure(
        context.config, snapshot_id, scope=scope_id, path_prefix=path_prefix, depth=depth, rank=rank,
        min_bytes=min_bytes, limit=effective, offset=offset,
    ))
    return _envelope(context, tool_name="steward_inspect_structure", source_kind=SourceKind.DERIVED_STRUCTURE,
        snapshot_ids=(snapshot_id,), scope_id=scope_id, result=result, result_digest=result.structure_digest,
        examined=result.full_path_node_count, returned=result.returned_path_node_count, limit=result.limit,
        offset=result.offset, truncated=result.has_more, budget_truncated=budget_truncated, start_ns=start)


def steward_compare_snapshots(
    context: ToolExecutionContext, left_snapshot_id: str, right_snapshot_id: str
) -> AgentToolEnvelope:
    start = _start(context)
    result = _safe_call(lambda: compute_verified_snapshot_diff(context.config, left_snapshot_id, right_snapshot_id))
    return _envelope(context, tool_name="steward_compare_snapshots", source_kind=SourceKind.HISTORICAL_SNAPSHOT_PAIR,
        snapshot_ids=(left_snapshot_id, right_snapshot_id), scope_id=None, result=result, result_digest=None,
        examined=len(result.items), returned=len(result.items), limit=None, offset=None, truncated=False,
        budget_truncated=False, start_ns=start)


def steward_inspect_growth(
    context: ToolExecutionContext, base_snapshot_id: str, target_snapshot_id: str, *, scope_id: str | None = None,
    path_prefix: str | None = None, depth: int | None = None, rank: GrowthRank | None = None,
    min_bytes: int | None = None, limit: int = 100, offset: int = 0,
) -> AgentToolEnvelope:
    effective, budget_truncated = _effective_page_limit(context, limit, offset)
    start = _start(context, depth=depth)
    result = _safe_call(lambda: query_verified_snapshot_growth(
        context.config, base_snapshot_id, target_snapshot_id, scope=scope_id, path_prefix=path_prefix,
        depth=depth, rank=rank, min_bytes=min_bytes, limit=effective, offset=offset,
    ))
    return _envelope(context, tool_name="steward_inspect_growth", source_kind=SourceKind.DERIVED_GROWTH,
        snapshot_ids=(base_snapshot_id, target_snapshot_id), scope_id=scope_id, result=result,
        result_digest=result.growth_digest, examined=result.full_path_node_count,
        returned=result.returned_path_node_count, limit=result.limit, offset=result.offset,
        truncated=result.has_more, budget_truncated=budget_truncated, start_ns=start)


def steward_inspect_duplicates(
    context: ToolExecutionContext, snapshot_id: str, *, only_exact: bool = False,
    limit: int = 100, offset: int = 0,
) -> AgentToolEnvelope:
    effective, budget_truncated = _effective_page_limit(context, limit, offset)
    start = _start(context)
    result = _safe_call(lambda: query_verified_snapshot_duplicates(
        context.config, snapshot_id, only_exact=only_exact, limit=effective, offset=offset
    ))
    return _envelope(context, tool_name="steward_inspect_duplicates", source_kind=SourceKind.DERIVED_DUPLICATE,
        snapshot_ids=(snapshot_id,), scope_id=None, result=result, result_digest=result.analysis_digest,
        examined=result.payload_equality_group_count, returned=result.returned_payload_equality_group_count,
        limit=result.limit, offset=result.offset, truncated=result.has_more,
        budget_truncated=budget_truncated, start_ns=start)


def steward_inspect_relations(
    context: ToolExecutionContext, base_snapshot_id: str, target_snapshot_id: str, *,
    kind: RelationKind | None = None, limit: int = 100, offset: int = 0,
) -> AgentToolEnvelope:
    effective, budget_truncated = _effective_page_limit(context, limit, offset)
    start = _start(context)
    result = _safe_call(lambda: query_verified_snapshot_relations(
        context.config, base_snapshot_id, target_snapshot_id, kind=kind, limit=effective, offset=offset
    ))
    return _envelope(context, tool_name="steward_inspect_relations", source_kind=SourceKind.DERIVED_RELATION,
        snapshot_ids=(base_snapshot_id, target_snapshot_id), scope_id=None, result=result,
        result_digest=result.relation_set_digest, examined=result.relation_item_count,
        returned=result.returned_relation_item_count, limit=result.limit, offset=result.offset,
        truncated=result.has_more, budget_truncated=budget_truncated, start_ns=start)


def steward_project_snapshot(
    context: ToolExecutionContext, request: SnapshotDiagnosticRequest | PairTrackingRequest,
    policy: ProjectionPolicy,
) -> AgentToolEnvelope:
    start = _start(context)
    snapshot_ids: tuple[str, ...]
    scope_id: str | None
    if isinstance(request, SnapshotDiagnosticRequest):
        result = _safe_call(lambda: build_snapshot_diagnostic_projection(context.config, request, policy))
        snapshot_ids = (request.primary_snapshot_id,)
        scope_id = request.scope
    elif isinstance(request, PairTrackingRequest):
        result = _safe_call(lambda: build_pair_tracking_projection(context.config, request, policy))
        snapshot_ids = (request.base_snapshot_id, request.target_snapshot_id)
        scope_id = request.scope
    else:
        raise AgentToolError("INVALID_ARGUMENT", "Projection request mode is unsupported")
    return _envelope(context, tool_name="steward_project_snapshot", source_kind=SourceKind.DERIVED_PROJECTION,
        snapshot_ids=snapshot_ids, scope_id=scope_id, result=result, result_digest=result.projection_digest,
        examined=None, returned=None, limit=None, offset=None, truncated=False, budget_truncated=False, start_ns=start)


def steward_resolve_entry_reference(
    context: ToolExecutionContext, snapshot_id: str, scope_id: str, relative_path: str
) -> AgentToolEnvelope:
    _validate_scope(context, snapshot_id, scope_id)
    _validate_relative_path(relative_path, field="relative_path")
    context.budget.effective_limit(1)
    start = _start(context)
    verification = _verification(context, snapshot_id)
    page = _safe_call(lambda: list_snapshot_entries(
        context.config, snapshot_id, scope_id=scope_id, path_prefix=relative_path, limit=2, offset=0
    ))
    matches = tuple(item for item in page.entries if item.relative_path == relative_path)
    if len(matches) != 1:
        raise AgentToolError("ENTRY_REFERENCE_NOT_FOUND", "the historical Entry reference does not resolve")
    result = {
        "verification": verification,
        "reference": {"snapshot_id": snapshot_id, "scope_id": scope_id, "relative_path": relative_path},
        "entry": matches[0],
        "current_fact_requires_recheck": True,
        "recommended_realtime_query": "filesystem_metadata_or_search",
    }
    return _envelope(context, tool_name="steward_resolve_entry_reference", source_kind=SourceKind.HISTORICAL_SNAPSHOT,
        snapshot_ids=(snapshot_id,), scope_id=scope_id, result=result, result_digest=None, examined=1,
        returned=1, limit=None, offset=None, truncated=False, budget_truncated=False, start_ns=start)
