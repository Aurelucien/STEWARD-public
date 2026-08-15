"""Pure validation and normalization for the Observation Projection contract."""

from uuid import UUID
from typing import NoReturn

from .errors import ObservationProjectionInvariantError, ObservationProjectionRequestError
from .models import (
    Accounting,
    AccountingDomain,
    BudgetValue,
    DiagnosticState,
    ExpansionDescriptor,
    PairTrackingRequest,
    PairTrackingGrowthHierarchyContext,
    PairTrackingGrowthHierarchyItem,
    PathGrowthMetrics,
    ProjectionBudget,
    ProjectionMode,
    ProjectionPreDigest,
    ProjectionSourceIdentity,
    ResultKind,
    ResultLocalReference,
    SelectionReason,
    SnapshotDiagnosticRequest,
    SnapshotPairSourceIdentity,
    SourcePlanItem,
    SourcePlanState,
    SourceResultIdentity,
)


def _uuid(value: str) -> None:
    try:
        UUID(value)
    except ValueError as error:
        raise ObservationProjectionRequestError("SNAPSHOT_ID_MALFORMED") from error


def _path(path: str | None) -> None:
    if path is None:
        return
    if path == ".":
        return
    if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ObservationProjectionRequestError("PATH_PREFIX_INVALID")


def validate_budget(budget: ProjectionBudget, *, allow_calibration: bool = False) -> None:
    values = (
        budget.explicit_entry_total,
        budget.hierarchy_node_total,
        budget.tracking_item_total,
        budget.relation_component_total,
        budget.duplicate_alias_component_total,
        budget.members_per_component,
        budget.scope_minimum_guarantee,
        budget.serialized_bytes_soft,
    ) + tuple(value for _, value in budget.priority_quotas)
    for value in values:
        if value == BudgetValue.REQUIRES_CALIBRATION and allow_calibration:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ObservationProjectionRequestError("BUDGET_INVALID")


def normalize_request(value: SnapshotDiagnosticRequest | PairTrackingRequest) -> SnapshotDiagnosticRequest | PairTrackingRequest:
    if isinstance(value, SnapshotDiagnosticRequest):
        _uuid(value.primary_snapshot_id)
        _path(value.path_prefix)
        if value.depth is not None and value.depth < 0:
            raise ObservationProjectionRequestError("PATH_PREFIX_INVALID")
        if value.min_bytes is not None and value.min_bytes < 0:
            raise ObservationProjectionRequestError("BUDGET_INVALID")
        if value.relation_context_pair is not None:
            left, right = value.relation_context_pair
            _uuid(left)
            _uuid(right)
            if left == right:
                raise ObservationProjectionRequestError("PAIR_SAME")
            if value.primary_snapshot_id not in value.relation_context_pair:
                raise ObservationProjectionRequestError("REQUEST_POLICY_INCOMPATIBLE")
        return value
    _uuid(value.base_snapshot_id)
    _uuid(value.target_snapshot_id)
    _path(value.path_prefix)
    if value.base_snapshot_id == value.target_snapshot_id:
        raise ObservationProjectionRequestError("PAIR_SAME")
    return value


def normalize_reasons(values: tuple[SelectionReason, ...]) -> tuple[SelectionReason, ...]:
    return tuple(sorted(set(values), key=lambda item: item.value))


def _validate_identity(value: ProjectionSourceIdentity, mode: ProjectionMode) -> None:
    if not isinstance(value, ProjectionSourceIdentity):
        raise ObservationProjectionInvariantError("SOURCE_RESULT_IDENTITY_MISMATCH")
    if (value.primary_snapshot is None) == (value.snapshot_pair is None):
        raise ObservationProjectionInvariantError("SOURCE_RESULT_IDENTITY_MISMATCH")
    if mode == ProjectionMode.SNAPSHOT_DIAGNOSTIC and value.primary_snapshot is None:
        raise ObservationProjectionInvariantError("SOURCE_RESULT_IDENTITY_MISMATCH")
    if mode == ProjectionMode.PAIR_TRACKING and value.snapshot_pair is None:
        raise ObservationProjectionInvariantError("SOURCE_RESULT_IDENTITY_MISMATCH")


def _validate_accounting(value: Accounting) -> None:
    if min(value.source_count, value.explicit_count, value.aggregate_accounted_count) < 0:
        raise ObservationProjectionInvariantError("ACCOUNTING_INVARIANT_VIOLATION")
    if value.source_count != value.explicit_count + value.aggregate_accounted_count:
        raise ObservationProjectionInvariantError("ACCOUNTING_INVARIANT_VIOLATION")
    byte_values = (value.known_source_bytes, value.known_explicit_bytes, value.known_aggregate_accounted_bytes)
    if any(item is not None and item < 0 for item in byte_values):
        raise ObservationProjectionInvariantError("ACCOUNTING_INVARIANT_VIOLATION")
    if value.known_source_bytes is not None:
        if value.known_explicit_bytes is None or value.known_aggregate_accounted_bytes is None:
            raise ObservationProjectionInvariantError("ACCOUNTING_INVARIANT_VIOLATION")
        if value.known_source_bytes != value.known_explicit_bytes + value.known_aggregate_accounted_bytes:
            raise ObservationProjectionInvariantError("ACCOUNTING_INVARIANT_VIOLATION")


def _invalid() -> NoReturn:
    raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT")


def _validate_growth_metrics(value: PathGrowthMetrics) -> None:
    non_negative = (
        value.base_known_logical_bytes,
        value.target_known_logical_bytes,
        value.added_logical_bytes,
        value.added_location_count,
        value.removed_logical_bytes,
        value.removed_location_count,
        value.same_location_increase_bytes,
        value.same_location_increase_count,
        value.same_location_decrease_bytes,
        value.same_location_decrease_count,
        value.same_location_unchanged_count,
        value.unknown_size_contribution_count,
    )
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in non_negative):
        _invalid()
    if isinstance(value.known_net_logical_delta, bool) or not isinstance(value.known_net_logical_delta, int):
        _invalid()
    if not isinstance(value.decomposition_complete, bool):
        _invalid()


def _validate_growth_hierarchy_item(
    value: PairTrackingGrowthHierarchyItem,
    context: PairTrackingGrowthHierarchyContext,
) -> None:
    identity = context.source_result_identity
    if (
        identity is None
        or not isinstance(identity, SourceResultIdentity)
        or not isinstance(identity.source_identity, SnapshotPairSourceIdentity)
        or not isinstance(value.node_reference, ResultLocalReference)
        or not isinstance(value.expansion_descriptor, ExpansionDescriptor)
    ):
        _invalid()
    namespace = value.node_reference.namespace
    if (
        namespace.result_kind != ResultKind.GROWTH
        or namespace.source_identity != identity.source_identity
        or namespace.source_result_digest != identity.result_digest
        or not value.node_reference.result_local_id
        or not value.scope_id
    ):
        _invalid()
    _validate_growth_metrics(value.direct_metrics)
    _validate_growth_metrics(value.recursive_metrics)
    if value.selection_reasons != normalize_reasons(value.selection_reasons):
        _invalid()
    descriptor = value.expansion_descriptor
    expected_path = value.relative_directory_path or "."
    if (
        descriptor.result_kind != ResultKind.GROWTH
        or descriptor.namespace != namespace
        or descriptor.local_id != value.node_reference.result_local_id
        or descriptor.snapshot_ids
        != (identity.source_identity.base.snapshot_id, identity.source_identity.target.snapshot_id)
        or descriptor.scope != value.scope_id
        or descriptor.path_prefix != expected_path
    ):
        _invalid()


def _validate_pair_tracking_growth_hierarchy(value: ProjectionPreDigest) -> None:
    body = value.pair_tracking
    if body is None or not isinstance(body.growth_hierarchy, PairTrackingGrowthHierarchyContext):
        _invalid()
    context = body.growth_hierarchy
    plans = tuple(item for item in value.source_plan if item.result_kind == ResultKind.GROWTH)
    accounting = tuple(
        item for item in value.accounting
        if item.domain == AccountingDomain.PAIR_TRACKING_GROWTH_HIERARCHY
    )
    if len(plans) != 1 or not isinstance(context.state, SourcePlanState) or plans[0].state != context.state:
        _invalid()
    if context.state == SourcePlanState.NOT_REQUESTED:
        if context.source_result_identity is not None or context.hierarchy_items or accounting:
            _invalid()
        return
    identity = context.source_result_identity
    if (
        identity is None
        or not isinstance(identity, SourceResultIdentity)
        or identity.result_kind != ResultKind.GROWTH
        or not isinstance(identity.source_identity, SnapshotPairSourceIdentity)
        or identity.source_identity != value.source_identity.snapshot_pair
        or identity.result_digest is None
        or plans[0].source_identity != identity
        or len(accounting) != 1
    ):
        _invalid()
    hierarchy_accounting = accounting[0]
    if context.state == SourcePlanState.REQUESTED_AND_EMPTY:
        if context.hierarchy_items or (
            hierarchy_accounting.source_count,
            hierarchy_accounting.explicit_count,
            hierarchy_accounting.aggregate_accounted_count,
        ) != (0, 0, 0):
            _invalid()
        return
    if context.state != SourcePlanState.REQUESTED_AND_PRESENT or not context.hierarchy_items:
        _invalid()
    if hierarchy_accounting.explicit_count != len(context.hierarchy_items):
        _invalid()
    seen: set[ResultLocalReference] = set()
    for item in context.hierarchy_items:
        if not isinstance(item, PairTrackingGrowthHierarchyItem):
            _invalid()
        if item.node_reference in seen:
            _invalid()
        seen.add(item.node_reference)
        _validate_growth_hierarchy_item(item, context)
        if item.expansion_descriptor not in value.expansion_descriptors:
            _invalid()


def validate_predigest(value: ProjectionPreDigest) -> None:
    if (value.snapshot_diagnostic is None) == (value.pair_tracking is None):
        raise ObservationProjectionInvariantError("IMPOSSIBLE_MODE_BODY_STATE")
    if value.mode == ProjectionMode.SNAPSHOT_DIAGNOSTIC and value.snapshot_diagnostic is None:
        raise ObservationProjectionInvariantError("IMPOSSIBLE_MODE_BODY_STATE")
    if value.mode == ProjectionMode.PAIR_TRACKING and value.pair_tracking is None:
        raise ObservationProjectionInvariantError("IMPOSSIBLE_MODE_BODY_STATE")
    _validate_identity(value.source_identity, value.mode)
    if not isinstance(value.diagnostic_state, DiagnosticState):
        raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT")
    if value.diagnostic_state.source_identity != value.source_identity:
        raise ObservationProjectionInvariantError("SOURCE_RESULT_IDENTITY_MISMATCH")
    validate_budget(value.resolved_policy.budget)
    if not all(isinstance(item, SourcePlanItem) for item in value.source_plan):
        raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT")
    for item in value.accounting:
        _validate_accounting(item)
    if value.mode == ProjectionMode.PAIR_TRACKING:
        _validate_pair_tracking_growth_hierarchy(value)
