"""Tests for the machine-readable native/compatibility surface map."""

from __future__ import annotations

import json

from local_steward.native_mcp_server.protocol import (
    SERVER_INSTRUCTIONS,
    SURFACE_MANIFEST,
    SURFACE_MANIFEST_JSON,
    TOOL_NAMES,
)
from local_steward.native_mcp_server.surface_manifest import (
    COMPATIBILITY_SURFACE_ID,
    HISTORICAL_CANDIDATE_ID,
    HISTORICAL_SURFACE_ID,
    NATIVE_SURFACE_ID,
    PUBLIC_PRODUCT_SURFACE_ID,
    SURFACE_MANIFEST_SCHEMA_NAME,
    SURFACE_MANIFEST_SCHEMA_VERSION,
    build_surface_manifest,
    copy_surface_manifest,
)


def test_surface_manifest_is_canonical_and_path_free() -> None:
    assert SURFACE_MANIFEST["schema_name"] == SURFACE_MANIFEST_SCHEMA_NAME
    assert SURFACE_MANIFEST["schema_version"] == SURFACE_MANIFEST_SCHEMA_VERSION
    assert json.loads(SURFACE_MANIFEST_JSON) == SURFACE_MANIFEST
    assert all(
        not isinstance(value, str) or value.startswith("/") is False
        for value in json.dumps(SURFACE_MANIFEST).split('"')
    )

    surfaces = {item["id"]: item for item in SURFACE_MANIFEST["surfaces"]}
    assert set(surfaces) == {
        NATIVE_SURFACE_ID,
        PUBLIC_PRODUCT_SURFACE_ID,
        COMPATIBILITY_SURFACE_ID,
        HISTORICAL_SURFACE_ID,
        HISTORICAL_CANDIDATE_ID,
    }
    assert surfaces[NATIVE_SURFACE_ID]["status"] == "ACTIVE"
    assert surfaces[NATIVE_SURFACE_ID]["tools"] == list(TOOL_NAMES)
    assert surfaces[PUBLIC_PRODUCT_SURFACE_ID]["status"] == "ACTIVE"
    assert surfaces[COMPATIBILITY_SURFACE_ID]["status"] == "COMPATIBILITY"
    assert surfaces[HISTORICAL_SURFACE_ID]["status"] == "HISTORICAL"
    assert surfaces[HISTORICAL_CANDIDATE_ID]["status"] == "HISTORICAL"
    assert SURFACE_MANIFEST["routing"]["codex_primary_surface"] == NATIVE_SURFACE_ID
    assert SURFACE_MANIFEST["routing"]["non_routes"] == [
        COMPATIBILITY_SURFACE_ID,
        HISTORICAL_SURFACE_ID,
        HISTORICAL_CANDIDATE_ID,
    ]
    assert "local_steward.surface_manifest" not in SERVER_INSTRUCTIONS
    assert SURFACE_MANIFEST_JSON not in SERVER_INSTRUCTIONS
    assert len(SERVER_INSTRUCTIONS.encode("utf-8")) < 1_500


def test_surface_manifest_builder_rejects_invalid_tool_lists() -> None:
    for value in ((), ("",), ("same", "same")):
        try:
            build_surface_manifest(value)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid tool list was accepted")


def test_surface_manifest_copy_is_defensive() -> None:
    copied = copy_surface_manifest(TOOL_NAMES)
    copied["surfaces"][0]["tools"].clear()
    assert SURFACE_MANIFEST["surfaces"][0]["tools"] == list(TOOL_NAMES)
