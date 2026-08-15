"""Pure required-source planning for Pair Tracking Projection."""

from dataclasses import dataclass

from .errors import ObservationProjectionRequestError
from .models import PairTrackingRequest, ProjectionPolicy, SourcePlanState


@dataclass(frozen=True, slots=True)
class PairTrackingSourcePlan:
    diff_requested: bool
    growth_requested: bool
    relation_requested: bool
    duplicate_requested: bool = False


def plan_pair_tracking_sources(
    request: PairTrackingRequest, policy: ProjectionPolicy
) -> PairTrackingSourcePlan:
    """Plan only explicit Pair Tracking sources; this function performs no I/O."""
    if request.diff == SourcePlanState.NOT_REQUESTED:
        raise ObservationProjectionRequestError("REQUEST_POLICY_INCOMPATIBLE")
    relation_requested = request.relation != SourcePlanState.NOT_REQUESTED
    if relation_requested and not policy.relation_overlay:
        raise ObservationProjectionRequestError("REQUEST_POLICY_INCOMPATIBLE")
    return PairTrackingSourcePlan(
        True,
        request.growth != SourcePlanState.NOT_REQUESTED,
        relation_requested,
    )
