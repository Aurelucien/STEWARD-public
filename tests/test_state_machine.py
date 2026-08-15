import pytest

from local_steward.errors import InvalidRunTransitionError
from local_steward.models import RunStatus
from local_steward.state_machine import allowed_targets, is_terminal, validate_transition


def test_allowed_transition_and_terminal_status() -> None:
    validate_transition(RunStatus.CREATED, RunStatus.CANCELLED)
    assert RunStatus.CANCELLED in allowed_targets(RunStatus.CREATED)
    assert is_terminal(RunStatus.CANCELLED)


def test_invalid_transition_is_rejected() -> None:
    with pytest.raises(InvalidRunTransitionError):
        validate_transition(RunStatus.CANCELLED, RunStatus.CREATED)
