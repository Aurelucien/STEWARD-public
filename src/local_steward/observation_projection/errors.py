"""Stable typed failures for the pure Observation Projection foundation."""

from ..errors import StewardError


class ObservationProjectionError(StewardError):
    code = "OBSERVATION_PROJECTION_INVALID"
    exit_code = 2

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class ObservationProjectionRequestError(ObservationProjectionError):
    code = "PROJECTION_REQUEST_INVALID"


class ObservationProjectionInvariantError(ObservationProjectionError):
    code = "PROJECTION_INVARIANT_VIOLATION"


class ObservationProjectionCanonicalError(ObservationProjectionError):
    code = "PROJECTION_CANONICAL_INVARIANT_VIOLATION"
