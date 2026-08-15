"""Pure required-source planning for Snapshot Diagnostic Projection."""

from dataclasses import dataclass

from .errors import ObservationProjectionInvariantError, ObservationProjectionRequestError
from .models import ProjectionPolicy, SnapshotDiagnosticRequest, SourcePlanState


@dataclass(frozen=True, slots=True)
class SnapshotDiagnosticSourcePlan:
    structure_requested: bool
    duplicate_requested: bool
    relation_requested: bool
    relation_pair: tuple[str, str] | None


def plan_snapshot_diagnostic_sources(
    request: SnapshotDiagnosticRequest, policy: ProjectionPolicy
) -> SnapshotDiagnosticSourcePlan:
    """Derive only required V2A analyses; this function has no I/O."""
    duplicate_requested = request.duplicate_overlay != SourcePlanState.NOT_REQUESTED
    relation_requested = request.relation_context_pair is not None
    if duplicate_requested and not policy.duplicate_overlay:
        raise ObservationProjectionRequestError("REQUEST_POLICY_INCOMPATIBLE")
    if relation_requested and not policy.relation_overlay:
        raise ObservationProjectionRequestError("REQUEST_POLICY_INCOMPATIBLE")
    if request.duplicate_overlay == SourcePlanState.REQUESTED_AND_PRESENT:
        raise ObservationProjectionInvariantError("UNREACHABLE_SOURCE_PLAN_STATE")
    return SnapshotDiagnosticSourcePlan(
        request.hierarchy_requested,
        duplicate_requested,
        relation_requested,
        request.relation_context_pair,
    )
