"""Stable safe failures for the provider-free Agent Context Pack product."""

from ..errors import StewardError


class AgentContextError(StewardError):
    """One safe outer-product failure with an optional stable source code."""

    code = "AGENT_CONTEXT_UNAVAILABLE"
    exit_code = 8

    def __init__(self, message: str, *, cause_code: str | None = None) -> None:
        self.cause_code = cause_code
        super().__init__(message)


class AgentContextRequestError(AgentContextError):
    code = "AGENT_CONTEXT_REQUEST_INVALID"
    exit_code = 2


class AgentContextSourceError(AgentContextError):
    code = "AGENT_CONTEXT_SOURCE_INVALID"
    exit_code = 2


class AgentContextSourceUnsupportedError(AgentContextError):
    code = "AGENT_CONTEXT_SOURCE_UNSUPPORTED"
    exit_code = 2


class AgentContextResourceError(AgentContextError):
    code = "AGENT_CONTEXT_RESOURCE_LIMIT"
    exit_code = 4


class AgentContextInvariantError(AgentContextError):
    code = "AGENT_CONTEXT_INVARIANT_VIOLATION"
    exit_code = 8


class AgentContextCanonicalError(AgentContextError):
    code = "AGENT_CONTEXT_CANONICAL_INVARIANT_VIOLATION"
    exit_code = 8


class AgentContextSourceUnavailableError(AgentContextError):
    code = "AGENT_CONTEXT_SOURCE_UNAVAILABLE"
    exit_code = 8


class AgentContextUnavailableError(AgentContextError):
    code = "AGENT_CONTEXT_UNAVAILABLE"
    exit_code = 8


class ContextProjectionError(AgentContextError):
    """Safe failures for the additive 0.6.0 Context Projection surface."""

    code = "CONTEXT_PROJECTION_INVALID"
    exit_code = 2


class ContextProjectionUnsupportedProfileError(ContextProjectionError):
    code = "UNSUPPORTED_PROFILE"


class ContextProjectionContinuationMismatchError(ContextProjectionError):
    code = "CONTINUATION_MISMATCH"


class ContextProjectionResourceError(ContextProjectionError):
    code = "CONTEXT_PROJECTION_RESOURCE_LIMIT"
    exit_code = 4
