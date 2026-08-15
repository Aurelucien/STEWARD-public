"""Local read-only MCP adapter for governed historical STEWARD context."""

from .adapter import McpDispatcher
from .protocol import (
    ADAPTER_SCHEMA_NAME,
    ADAPTER_SCHEMA_VERSION,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    TOOL_NAMES,
    tool_descriptors,
)
from .server import create_server

__all__ = [
    "ADAPTER_SCHEMA_NAME",
    "ADAPTER_SCHEMA_VERSION",
    "McpDispatcher",
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "TOOL_NAMES",
    "create_server",
    "tool_descriptors",
]
