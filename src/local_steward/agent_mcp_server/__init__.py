"""Single grant-gated Agent MCP boundary for STEWARD Context."""

from .adapter import AgentContextRouteDispatcher
from .protocol import SERVER_INSTRUCTIONS, SERVER_NAME, TOOL_NAME, tool_descriptors
from .server import create_server

__all__ = [
    "AgentContextRouteDispatcher",
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "TOOL_NAME",
    "create_server",
    "tool_descriptors",
]
