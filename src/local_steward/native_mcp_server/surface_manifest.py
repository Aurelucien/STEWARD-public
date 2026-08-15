"""Machine-readable routing map for the Codex-facing STEWARD surfaces."""

from __future__ import annotations

from copy import deepcopy
import json
from collections.abc import Sequence
from typing import Any

from ..codex_identity import MCP_SERVER_NAME, NATIVE_SURFACE_IDENTITY


SURFACE_MANIFEST_SCHEMA_NAME = "local_steward.surface_manifest"
SURFACE_MANIFEST_SCHEMA_VERSION = 1
NATIVE_SURFACE_ID = "native_mcp_server"
PUBLIC_PRODUCT_SURFACE_ID = "public_cli_api"
COMPATIBILITY_SURFACE_ID = "mcp_server"
HISTORICAL_SURFACE_ID = "agent_mcp_server"
HISTORICAL_CANDIDATE_ID = "r4d_r3c_plugin_candidate"


def build_surface_manifest(active_tool_names: Sequence[str]) -> dict[str, Any]:
    """Build the canonical surface map without host paths or mutable runtime state."""

    tools = tuple(active_tool_names)
    if not tools or any(not isinstance(name, str) or not name for name in tools):
        raise ValueError("active native tools must be non-empty names")
    if len(set(tools)) != len(tools):
        raise ValueError("active native tools must be unique")
    return {
        "schema_name": SURFACE_MANIFEST_SCHEMA_NAME,
        "schema_version": SURFACE_MANIFEST_SCHEMA_VERSION,
        "routing": {
            "codex_primary_surface": NATIVE_SURFACE_ID,
            "selection_rule": (
                "Use native_mcp_server for the personal Codex integration. The product CLI/API "
                "remains the supported product surface; compatibility and historical adapters "
                "are not additional Codex routes."
            ),
            "non_routes": [
                COMPATIBILITY_SURFACE_ID,
                HISTORICAL_SURFACE_ID,
                HISTORICAL_CANDIDATE_ID,
            ],
        },
        "surfaces": [
            {
                "id": NATIVE_SURFACE_ID,
                "status": "ACTIVE",
                "audience": "codex",
                "role": "primary_personal_integration",
                "entrypoint": "local_steward.native_mcp_server",
                "server_name": MCP_SERVER_NAME,
                "identity": NATIVE_SURFACE_IDENTITY,
                "tools": list(tools),
            },
            {
                "id": PUBLIC_PRODUCT_SURFACE_ID,
                "status": "ACTIVE",
                "audience": "product",
                "role": "supported_cli_and_python_api",
                "entrypoint": "local_steward.cli / local_steward.file_agent",
                "tools": [],
            },
            {
                "id": COMPATIBILITY_SURFACE_ID,
                "status": "COMPATIBILITY",
                "audience": "repository",
                "role": "legacy_product_adapter",
                "entrypoint": "local_steward.mcp_server",
                "tools": [
                    "steward_list_snapshots",
                    "steward_prepare_context",
                    "steward_resolve_entry",
                ],
                "route": "not_a_codex_plugin_route",
            },
            {
                "id": HISTORICAL_SURFACE_ID,
                "status": "HISTORICAL",
                "audience": "repository",
                "role": "superseded_agent_adapter",
                "entrypoint": "local_steward.agent_mcp_server",
                "tools": ["steward_agent_context"],
                "route": "not_a_codex_plugin_route",
            },
            {
                "id": HISTORICAL_CANDIDATE_ID,
                "status": "HISTORICAL",
                "audience": "repository_experiment",
                "role": "superseded_plugin_candidate",
                "entrypoint": (
                    "experiments/steward_exoskeleton/archive/r4d_r3c_plugin_candidate.py"
                ),
                "route": "archived_not_imported",
            },
        ],
    }


def surface_manifest_json(active_tool_names: Sequence[str]) -> str:
    """Return stable compact JSON suitable for MCP initialization instructions."""

    return json.dumps(
        build_surface_manifest(active_tool_names),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def copy_surface_manifest(active_tool_names: Sequence[str]) -> dict[str, Any]:
    """Return a defensive copy for callers that need to inspect the manifest."""

    return deepcopy(build_surface_manifest(active_tool_names))
