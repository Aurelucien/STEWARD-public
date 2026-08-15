"""Stable host-authority failures for the native Agent surface."""

from ..errors import StewardError


class StewardAuthorityError(StewardError):
    code = "STEWARD_AUTHORITY_INVALID"
    exit_code = 2


class StewardAuthorityRequiredError(StewardAuthorityError):
    code = "STEWARD_AUTHORITY_REQUIRED"


class StewardAuthorityContextError(StewardAuthorityError):
    code = "STEWARD_AUTHORITY_CONTEXT_INVALID"
