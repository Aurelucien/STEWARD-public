"""Official low-level assembly for the single grant-gated Agent MCP tool."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import Prompt, Resource, ResourceTemplate

from local_steward.config import load_config

from .adapter import AgentContextRouteDispatcher
from .protocol import (
    CONFIG_ENVIRONMENT_VARIABLE,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    SERVER_VERSION,
    tool_descriptors,
)


def governed_config_path(environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    raw = values.get(CONFIG_ENVIRONMENT_VARIABLE)
    if raw is None or not raw or raw.strip() != raw:
        raise ValueError
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        raise ValueError
    load_config(path)
    return path


def create_server(dispatcher: AgentContextRouteDispatcher) -> Server[None]:
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.ERROR)
    server: Server[None] = Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.list_tools()  # type: ignore[no-untyped-call]
    async def list_tools():  # type: ignore[no-untyped-def]
        return tool_descriptors()

    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, object]):  # type: ignore[no-untyped-def]
        return await dispatcher.dispatch(name, arguments)

    @server.list_resources()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_resources() -> list[Resource]:
        return []

    @server.list_resource_templates()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_resource_templates() -> list[ResourceTemplate]:
        return []

    @server.list_prompts()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_prompts() -> list[Prompt]:
        return []

    return server


async def run_stdio(config_path: Path) -> None:
    server = create_server(AgentContextRouteDispatcher(config_path))
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    try:
        config_path = governed_config_path()
    except Exception:
        print("STEWARD Agent MCP configuration is unavailable.", file=sys.stderr)
        raise SystemExit(2) from None
    anyio.run(run_stdio, config_path)
