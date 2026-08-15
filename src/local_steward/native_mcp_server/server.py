"""Official STDIO assembly for the native risk-separated STEWARD service."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
import sys

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import Prompt, Resource, ResourceTemplate

from ..agent_session import load_steward_session
from .adapter import NativeStewardDispatcher
from .host_policy import load_codex_host_policy
from .protocol import (
    CONFIG_ENVIRONMENT_VARIABLE,
    HOST_POLICY_ENVIRONMENT_VARIABLE,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    SERVER_VERSION,
    tool_descriptors,
)


def _request_meta(server: Server[None]) -> dict[str, object]:
    """Extract MCP metadata from the active request context."""

    try:
        meta = server.request_context.meta
    except LookupError:
        return {}
    if meta is None:
        return {}
    try:
        value = meta.model_dump(by_alias=True, exclude_none=True)
    except (AttributeError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def governed_paths(environment: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    values = os.environ if environment is None else environment
    paths: list[Path] = []
    for name in (CONFIG_ENVIRONMENT_VARIABLE, HOST_POLICY_ENVIRONMENT_VARIABLE):
        raw = values.get(name)
        if raw is None or not raw or raw.strip() != raw:
            raise ValueError
        path = Path(raw)
        if not path.is_absolute() or not path.is_file():
            raise ValueError
        paths.append(path)
    return paths[0], paths[1]


def create_server(dispatcher: NativeStewardDispatcher) -> Server[None]:
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.ERROR)
    server: Server[None] = Server(
        SERVER_NAME, version=SERVER_VERSION, instructions=SERVER_INSTRUCTIONS
    )

    @server.list_tools()  # type: ignore[no-untyped-call]
    async def list_tools():  # type: ignore[no-untyped-def]
        return tool_descriptors()

    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, object]):  # type: ignore[no-untyped-def]
        return await dispatcher.dispatch(name, arguments, request_meta=_request_meta(server))

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


async def run_stdio(dispatcher: NativeStewardDispatcher) -> None:
    server = create_server(dispatcher)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    try:
        config_path, host_policy_path = governed_paths()
        session = load_steward_session(config_path)
        host_policy = load_codex_host_policy(host_policy_path)
        dispatcher = NativeStewardDispatcher(session, host_policy)
    except Exception:
        print("STEWARD native session or Codex host policy is unavailable.", file=sys.stderr)
        raise SystemExit(2) from None
    anyio.run(run_stdio, dispatcher)
