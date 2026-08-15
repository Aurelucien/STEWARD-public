"""Stable failures for the unified Agent-facing STEWARD session."""

from ..errors import StewardError


class StewardSessionError(StewardError):
    code = "STEWARD_SESSION_UNAVAILABLE"
    exit_code = 8


class StewardSessionConfigurationError(StewardSessionError):
    code = "STEWARD_SESSION_CONFIGURATION_INVALID"
    exit_code = 2


class StewardAuthorityDomainError(StewardSessionError):
    code = "STEWARD_AUTHORITY_DOMAIN_MISMATCH"
    exit_code = 2


class StewardSelectionError(StewardSessionError):
    code = "STEWARD_SELECTION_INVALID"
    exit_code = 2


class StewardSelectionNotFoundError(StewardSelectionError):
    code = "STEWARD_SELECTION_NOT_FOUND"


class StewardSelectionAmbiguousError(StewardSelectionError):
    code = "STEWARD_SELECTION_AMBIGUOUS"


class StewardSelectionResourceError(StewardSelectionError):
    code = "STEWARD_SELECTION_RESOURCE_LIMIT"
    exit_code = 8


class StewardTaskReferenceError(StewardSelectionError):
    code = "STEWARD_TASK_REFERENCE_INVALID"


class StewardScopeResolutionError(StewardSelectionError):
    code = "STEWARD_SCOPE_RESOLUTION_INVALID"


class StewardPathResolutionError(StewardSelectionError):
    code = "STEWARD_PATH_RESOLUTION_INVALID"
