"""Canonical identity constants for the personal STEWARD Codex integration."""

from __future__ import annotations

import hashlib
import json
from typing import Final


PLUGIN_NAME: Final = "steward-exoskeleton"
PLUGIN_BASE_VERSION: Final = "0.33.0"
SKILL_NAME: Final = "steward-codex"
MCP_SERVER_NAME: Final = "local-steward-native"
NATIVE_SURFACE_IDENTITY: Final = "STEWARD_CODEX_NATIVE_V27"
NATIVE_SERVER_VERSION: Final = "24"
HOOK_IDENTITY: Final = "STEWARD_HOST_OBSERVER_V1"
INTEGRATION_IDENTITY_SCHEMA_NAME: Final = "local_steward.codex_integration_identity"
INTEGRATION_IDENTITY_SCHEMA_VERSION: Final = 1


def integration_identity_machine_object() -> dict[str, object]:
    """Return the path-free identity tuple shared by Skill, MCP and hook surfaces."""

    return {
        "schema_name": INTEGRATION_IDENTITY_SCHEMA_NAME,
        "schema_version": INTEGRATION_IDENTITY_SCHEMA_VERSION,
        "plugin_name": PLUGIN_NAME,
        "plugin_base_version": PLUGIN_BASE_VERSION,
        "skill_name": SKILL_NAME,
        "mcp_server_name": MCP_SERVER_NAME,
        "native_surface_identity": NATIVE_SURFACE_IDENTITY,
        "native_server_version": NATIVE_SERVER_VERSION,
        "hook_identity": HOOK_IDENTITY,
    }


def integration_identity_sha256() -> str:
    """Return the stable path-free digest of the canonical integration identity."""

    payload = json.dumps(
        integration_identity_machine_object(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
