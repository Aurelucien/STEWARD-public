"""The only Run transition policy."""

from .errors import InvalidRunTransitionError
from .models import RunStatus

_ALLOWED: dict[RunStatus, tuple[RunStatus, ...]] = {
    RunStatus.CREATED: (
        RunStatus.SCANNING,
        RunStatus.PLANNING,
        RunStatus.APPLYING,
        RunStatus.VERIFYING,
        RunStatus.ROLLING_BACK,
        RunStatus.CANCELLED,
        RunStatus.FAILED,
    ),
    RunStatus.SCANNING: (
        RunStatus.SCANNED,
        RunStatus.PARTIAL,
        RunStatus.CANCELLED,
        RunStatus.FAILED,
    ),
    RunStatus.SCANNED: (RunStatus.PLANNING, RunStatus.VERIFYING, RunStatus.FAILED),
    RunStatus.PLANNING: (RunStatus.PLANNED, RunStatus.CANCELLED, RunStatus.FAILED),
    RunStatus.PLANNED: (RunStatus.APPLYING, RunStatus.CANCELLED, RunStatus.FAILED),
    RunStatus.APPLYING: (RunStatus.APPLIED, RunStatus.PARTIAL, RunStatus.FAILED),
    RunStatus.APPLIED: (RunStatus.VERIFYING, RunStatus.ROLLING_BACK, RunStatus.FAILED),
    RunStatus.VERIFYING: (RunStatus.VERIFIED, RunStatus.PARTIAL, RunStatus.FAILED),
    RunStatus.VERIFIED: (RunStatus.ROLLING_BACK,),
    RunStatus.PARTIAL: (RunStatus.VERIFYING, RunStatus.ROLLING_BACK, RunStatus.FAILED),
    RunStatus.FAILED: (RunStatus.ROLLING_BACK,),
    RunStatus.CANCELLED: (),
    RunStatus.ROLLING_BACK: (RunStatus.ROLLED_BACK, RunStatus.PARTIAL, RunStatus.FAILED),
    RunStatus.ROLLED_BACK: (),
}
TERMINAL = frozenset(
    (RunStatus.VERIFIED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.ROLLED_BACK)
)


def allowed_targets(current: RunStatus) -> tuple[RunStatus, ...]:
    """Return frozen, deterministic targets."""
    return _ALLOWED[current]


def validate_transition(current: RunStatus, target: RunStatus) -> None:
    """Reject all transitions outside the single graph."""
    if target not in allowed_targets(current):
        raise InvalidRunTransitionError(
            f"transition {current.value} -> {target.value} is not allowed"
        )


def is_terminal(status: RunStatus) -> bool:
    return status in TERMINAL
