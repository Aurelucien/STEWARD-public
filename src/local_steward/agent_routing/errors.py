"""Stable safe failures for provider-free routing and publication."""

from ..errors import StewardError


class AgentRoutingError(StewardError):
    code = "AGENT_ROUTING_UNAVAILABLE"
    exit_code = 8


class AgentRoutingRequestError(AgentRoutingError):
    code = "AGENT_ROUTING_REQUEST_INVALID"
    exit_code = 2


class AgentRouteGrantError(AgentRoutingError):
    code = "AGENT_ROUTE_GRANT_INVALID"
    exit_code = 2


class AgentRouteGrantReusedError(AgentRouteGrantError):
    code = "AGENT_ROUTE_GRANT_REUSED"
    exit_code = 2


class AgentPublicationError(AgentRoutingError):
    code = "AGENT_PUBLICATION_INVALID"
    exit_code = 2


class AgentRoutingCanonicalError(AgentRoutingError):
    code = "AGENT_ROUTING_CANONICAL_INVARIANT_VIOLATION"
    exit_code = 8
