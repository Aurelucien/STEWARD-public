"""Isolated acceptance for the frozen local read-only MCP STDIO adapter."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from local_steward.agent_context import (
    AgentContextPackRequest,
    agent_context_pack_machine_object,
    prepare_agent_context,
)
from local_steward.config import load_config
from local_steward.file_agent import (
    SharedToolBudget,
    ToolBudgetLimits,
    ToolExecutionContext,
    serialize_envelope,
    steward_list_snapshots,
    steward_resolve_entry_reference,
)
from local_steward.llm_context import UserIntentContext
from local_steward.mcp_server import McpDispatcher, SERVER_INSTRUCTIONS, TOOL_NAMES
from local_steward.mcp_server.adapter import MAX_SAFE_JSON_INTEGER, model_safe_json
from local_steward.mcp_server.profile import (
    BALANCED_V1_CONTEXT_BUDGET,
    BALANCED_V1_PROJECTION_POLICY,
)
from local_steward.mcp_server.protocol import (
    EXACT_INTEGER_ENCODING_SCHEME,
    LIST_TOOL,
    MAX_STRUCTURED_RESULT_BYTES,
    PREPARE_TOOL,
    PROFILE_NAME,
    RESOLVE_TOOL,
    tool_descriptors,
)
from local_steward.mcp_server.server import create_server, governed_config_path
from local_steward.observation_projection import PairTrackingRequest, SnapshotDiagnosticRequest
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot
from local_steward.storage import initialize_storage

from .conftest import write_config


def _fixture(tmp_path: Path):  # type: ignore[no-untyped-def]
    observed = tmp_path / "observed"
    observed.mkdir()
    (observed / "stable.txt").write_text("stable", encoding="utf-8")
    nested = observed / "nested"
    nested.mkdir()
    (nested / "change.txt").write_text("before", encoding="utf-8")
    source = write_config(tmp_path)
    for name in ("data/cache", "data/evidence", "data/quarantine"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    config = load_config(source, project_root=tmp_path)
    config = replace(
        config,
        scopes=(replace(config.scopes[0], normalized_path=observed),),
    )
    initialize_storage(config)
    base = create_snapshot(config, (), make_budget())
    (nested / "change.txt").write_text("after", encoding="utf-8")
    target = create_snapshot(config, (), make_budget())
    return config, base, target


def _arguments(snapshot_id: str) -> dict[str, Any]:
    return {
        "profile": PROFILE_NAME,
        "source": {"kind": "SNAPSHOT_DIAGNOSTIC", "snapshot_id": snapshot_id},
        "user_intent": {"question": "Explain the explicit historical Snapshot."},
    }


def _call(dispatcher: McpDispatcher, name: str, arguments: object):  # type: ignore[no-untyped-def]
    async def run():  # type: ignore[no-untyped-def]
        return await dispatcher.dispatch(name, arguments)

    return anyio.run(run)


def _structured(result) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    assert isinstance(result.structuredContent, dict)
    return result.structuredContent


def _direct_context(config, *, items: int) -> ToolExecutionContext:  # type: ignore[no-untyped-def]
    return ToolExecutionContext(
        config,
        SharedToolBudget(
            ToolBudgetLimits(
                max_steward_calls_per_turn=1,
                max_items_per_call=items,
                max_items_per_turn=items,
                max_serialized_bytes_per_call=MAX_STRUCTURED_RESULT_BYTES,
                max_serialized_bytes_per_turn=MAX_STRUCTURED_RESULT_BYTES,
                max_elapsed_ms_per_call=30_000,
                max_elapsed_ms_per_turn=30_000,
            )
        ),
    )


def _machine(value: object) -> dict[str, Any]:
    decoded = json.loads(serialize_envelope(value))
    assert isinstance(decoded, dict)
    return decoded


def _without_runtime_timing(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_runtime_timing(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _without_runtime_timing(item)
            for key, item in value.items()
            if key not in {"elapsed_ms", "remaining_elapsed_ms"}
        }
    return value


def _data_state(config) -> dict[str, bytes]:  # type: ignore[no-untyped-def]
    return {
        str(path.relative_to(config.paths.data_dir)): path.read_bytes()
        for path in sorted(config.paths.data_dir.rglob("*"))
        if path.is_file()
    }


def test_model_safe_integer_encoding_is_lossless_bounded_and_pointer_safe() -> None:
    source = {
        "safe": MAX_SAFE_JSON_INTEGER,
        "large": MAX_SAFE_JSON_INTEGER + 1,
        "negative": -(MAX_SAFE_JSON_INTEGER + 2),
        "a/b~c": [MAX_SAFE_JSON_INTEGER + 3],
        "boolean": True,
    }

    encoded, paths = model_safe_json(source)

    assert encoded == {
        "safe": MAX_SAFE_JSON_INTEGER,
        "large": str(MAX_SAFE_JSON_INTEGER + 1),
        "negative": str(-(MAX_SAFE_JSON_INTEGER + 2)),
        "a/b~c": [str(MAX_SAFE_JSON_INTEGER + 3)],
        "boolean": True,
    }
    assert paths == (
        "/result/a~1b~0c/0",
        "/result/large",
        "/result/negative",
    )


def test_exact_discovery_and_empty_non_tool_inventories(tmp_path: Path) -> None:
    config, _base, _target = _fixture(tmp_path)
    server = create_server(McpDispatcher(config.source_path))
    tools = {item.name: item for item in tool_descriptors()}

    assert tuple(tools) == TOOL_NAMES
    assert server.name == "local-steward-context"
    assert server.instructions == SERVER_INSTRUCTIONS
    assert "exact decimal strings" in SERVER_INSTRUCTIONS
    assert "packet-local reference tokens as Evidence IDs" in SERVER_INSTRUCTIONS
    for tool in tools.values():
        assert tool.description
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.outputSchema is not None
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is False


def test_snapshot_and_pair_pack_match_direct_core_exactly(tmp_path: Path) -> None:
    config, base, target = _fixture(tmp_path)
    dispatcher = McpDispatcher(config.source_path)
    before = _data_state(config)
    cases = (
        (
            _arguments(target.snapshot_id),
            SnapshotDiagnosticRequest(target.snapshot_id),
        ),
        (
            {
                "profile": PROFILE_NAME,
                "source": {
                    "kind": "PAIR_TRACKING",
                    "base_snapshot_id": base.snapshot_id,
                    "target_snapshot_id": target.snapshot_id,
                    "scope_id": "managed",
                },
                "user_intent": {"question": "Explain the explicit historical pair."},
            },
            PairTrackingRequest(base.snapshot_id, target.snapshot_id, scope="managed"),
        ),
    )
    for arguments, source in cases:
        result = _call(dispatcher, PREPARE_TOOL, arguments)
        direct = prepare_agent_context(
            config,
            AgentContextPackRequest(
                source,
                BALANCED_V1_PROJECTION_POLICY,
                UserIntentContext(arguments["user_intent"]["question"]),
                BALANCED_V1_CONTEXT_BUDGET,
            ),
        )
        structured = _structured(result)
        assert result.isError is False
        expected, paths = model_safe_json({
            "profile_name": PROFILE_NAME,
            "pack": agent_context_pack_machine_object(direct),
        })
        assert structured["result"] == expected
        assert structured["exact_integer_encoding"] == {
            "scheme": EXACT_INTEGER_ENCODING_SCHEME,
            "decimal_string_paths": list(paths),
        }
    assert _data_state(config) == before
    assert not list(config.paths.data_dir.glob("state.db-*"))


def test_inventory_and_entry_resolution_match_direct_facades(tmp_path: Path) -> None:
    config, _base, target = _fixture(tmp_path)
    dispatcher = McpDispatcher(config.source_path)

    listed = _call(dispatcher, LIST_TOOL, {"limit": 10, "offset": 0})
    direct_list = steward_list_snapshots(_direct_context(config, items=20), limit=10, offset=0)
    assert _without_runtime_timing(
        _structured(listed)["result"]["inventory"]["result"]
    ) == _without_runtime_timing(_machine(direct_list)["result"])
    snapshots = _structured(listed)["result"]["inventory"]["result"]["snapshots"]
    assert {item["verification"]["status"] for item in snapshots} == {"VALID"}

    resolved = _call(
        dispatcher,
        RESOLVE_TOOL,
        {
            "snapshot_id": target.snapshot_id,
            "scope_id": "managed",
            "relative_path": "nested/change.txt",
        },
    )
    direct_resolution = steward_resolve_entry_reference(
        _direct_context(config, items=1),
        target.snapshot_id,
        "managed",
        "nested/change.txt",
    )
    expected, paths = model_safe_json({"resolution": _machine(direct_resolution)})
    structured = _structured(resolved)
    assert _without_runtime_timing(structured["result"]) == _without_runtime_timing(
        expected
    )
    assert structured["exact_integer_encoding"] == {
        "scheme": EXACT_INTEGER_ENCODING_SCHEME,
        "decimal_string_paths": list(paths),
    }
    assert _structured(resolved)["result"]["resolution"]["result"][
        "current_fact_requires_recheck"
    ] is True
    entry = structured["result"]["resolution"]["result"]["entry"]
    direct_entry = _machine(direct_resolution)["result"]["entry"]
    assert entry["mtime_ns"] == str(direct_entry["mtime_ns"])
    assert entry["ctime_ns"] == str(direct_entry["ctime_ns"])
    assert "/result/resolution/result/entry/mtime_ns" in paths
    assert "/result/resolution/result/entry/ctime_ns" in paths


@pytest.mark.parametrize(
    "arguments",
    [
        {"profile": PROFILE_NAME, "source": "{}", "user_intent": {"question": "q"}},
        {
            "profile": PROFILE_NAME,
            "source": {"kind": "SNAPSHOT_DIAGNOSTIC", "snapshot_id": "not-a-uuid"},
            "user_intent": {"question": "q"},
        },
        {
            "profile": PROFILE_NAME,
            "source": {
                "kind": "PAIR_TRACKING",
                "base_snapshot_id": "00000000-0000-4000-8000-000000000001",
                "target_snapshot_id": "00000000-0000-4000-8000-000000000001",
            },
            "user_intent": {"question": "q"},
        },
        {
            "profile": PROFILE_NAME,
            "source": {
                "kind": "SNAPSHOT_DIAGNOSTIC",
                "snapshot_id": "00000000-0000-4000-8000-000000000001",
                "path_prefix": "../escape",
            },
            "user_intent": {"question": "q"},
        },
        {
            "profile": PROFILE_NAME,
            "source": {"kind": "SNAPSHOT_DIAGNOSTIC", "snapshot_id": "00000000-0000-4000-8000-000000000001"},
            "user_intent": {"question": "q"},
            "extra": True,
        },
        {
            "profile": PROFILE_NAME,
            "source": {"kind": "SNAPSHOT_DIAGNOSTIC", "snapshot_id": "00000000-0000-4000-8000-000000000001"},
            "user_intent": {"question": "\ud800"},
        },
    ],
)
def test_strict_arguments_fail_before_product_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]
) -> None:
    config, _base, _target = _fixture(tmp_path)
    invoked = False

    def forbidden(*_args: object, **_kwargs: object) -> None:
        nonlocal invoked
        invoked = True

    monkeypatch.setattr("local_steward.mcp_server.adapter.prepare_agent_context", forbidden)
    result = _call(McpDispatcher(config.source_path), PREPARE_TOOL, arguments)
    assert result.isError is True
    assert _structured(result)["error"]["code"] == "STEWARD_MCP_ARGUMENT_INVALID"
    assert invoked is False


def test_safe_unknown_source_scope_and_resource_failures(tmp_path: Path) -> None:
    config, _base, target = _fixture(tmp_path)
    dispatcher = McpDispatcher(config.source_path)

    unknown_tool = _call(dispatcher, str(tmp_path / "not-a-tool"), {})
    assert _structured(unknown_tool)["error"]["code"] == "STEWARD_MCP_TOOL_NOT_FOUND"
    assert _structured(unknown_tool)["tool_name"] == "unknown"
    unknown_snapshot = _call(
        dispatcher,
        RESOLVE_TOOL,
        {
            "snapshot_id": "00000000-0000-4000-8000-000000000001",
            "scope_id": "managed",
            "relative_path": "stable.txt",
        },
    )
    assert _structured(unknown_snapshot)["error"]["code"] == "SNAPSHOT_NOT_FOUND"
    unknown_scope = _call(
        dispatcher,
        RESOLVE_TOOL,
        {
            "snapshot_id": target.snapshot_id,
            "scope_id": "unknown",
            "relative_path": "stable.txt",
        },
    )
    assert _structured(unknown_scope)["error"]["code"] == "INVALID_ARGUMENT"
    oversized = _call(
        McpDispatcher(config.source_path, max_result_bytes=100),
        LIST_TOOL,
        {},
    )
    assert _structured(oversized)["error"]["code"] == "STEWARD_MCP_RESOURCE_LIMIT"
    for result in (unknown_tool, unknown_snapshot, unknown_scope, oversized):
        assert _structured(result)["exact_integer_encoding"] == {
            "scheme": EXACT_INTEGER_ENCODING_SCHEME,
            "decimal_string_paths": [],
        }
        text = result.content[0].text
        assert str(tmp_path) not in text
        assert str(tmp_path) not in json.dumps(result.structuredContent)


def test_timeout_concurrency_and_cancellation_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _base, _target = _fixture(tmp_path)
    release = threading.Event()
    active = 0
    lock = threading.Lock()

    def blocked() -> dict[str, Any]:
        nonlocal active
        with lock:
            active += 1
        release.wait(timeout=2)
        return {"inventory": {}}

    monkeypatch.setattr(McpDispatcher, "_operation", lambda *_args: blocked)

    async def exercise() -> None:
        dispatcher = McpDispatcher(config.source_path, timeout_seconds=1)
        tasks = [asyncio.create_task(dispatcher.dispatch(LIST_TOOL, {})) for _ in range(4)]
        for _ in range(100):
            with lock:
                if active == 4:
                    break
            await asyncio.sleep(0.005)
        busy = await dispatcher.dispatch(LIST_TOOL, {})
        assert _structured(busy)["error"]["code"] == "STEWARD_MCP_BUSY"
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        release.set()
        completed = await asyncio.gather(*tasks[1:])
        assert all(not item.isError for item in completed)

        timeout_release = threading.Event()
        monkeypatch.setattr(
            McpDispatcher,
            "_operation",
            lambda *_args: lambda: (
                timeout_release.wait(timeout=1), {"inventory": {}}
            )[1],
        )
        timed = await McpDispatcher(
            config.source_path, timeout_seconds=0.01
        ).dispatch(LIST_TOOL, {})
        timeout_release.set()
        assert _structured(timed)["error"]["code"] == "STEWARD_MCP_TIMEOUT"

    asyncio.run(exercise())


def test_configuration_authority_has_no_default_or_path_disclosure(tmp_path: Path) -> None:
    config, _base, _target = _fixture(tmp_path)
    assert governed_config_path({"LOCAL_STEWARD_MCP_CONFIG": str(config.source_path)}) == config.source_path
    for environment in (
        {},
        {"LOCAL_STEWARD_MCP_CONFIG": ""},
        {"LOCAL_STEWARD_MCP_CONFIG": "config/steward.toml"},
        {"LOCAL_STEWARD_MCP_CONFIG": str(tmp_path / "missing.toml")},
    ):
        with pytest.raises(ValueError):
            governed_config_path(environment)

    environment = dict(os.environ)
    environment.pop("LOCAL_STEWARD_MCP_CONFIG", None)
    process = subprocess.run(
        [sys.executable, "-m", "local_steward.mcp_server"],
        cwd=Path(__file__).parent.parent,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 2
    assert process.stdout == b""
    assert process.stderr == b"STEWARD MCP configuration is unavailable.\n"


def test_real_official_client_stdio_initialize_discover_call_and_shutdown(tmp_path: Path) -> None:
    config, _base, target = _fixture(tmp_path)

    async def exercise() -> None:
        environment = dict(os.environ)
        environment["LOCAL_STEWARD_MCP_CONFIG"] = str(config.source_path)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "local_steward.mcp_server"],
            cwd=str(Path(__file__).parent.parent),
            env=environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "local-steward-context"
                assert initialized.serverInfo.version == "2"
                assert initialized.instructions == SERVER_INSTRUCTIONS
                assert tuple(item.name for item in (await session.list_tools()).tools) == TOOL_NAMES
                assert not (await session.list_resources()).resources
                assert not (await session.list_resource_templates()).resourceTemplates
                assert not (await session.list_prompts()).prompts
                listed = await session.call_tool(
                    LIST_TOOL, {"limit": 10, "offset": 0}, read_timeout_seconds=timedelta(seconds=10)
                )
                prepared = await session.call_tool(
                    PREPARE_TOOL,
                    _arguments(target.snapshot_id),
                    read_timeout_seconds=timedelta(seconds=10),
                )
                resolved = await session.call_tool(
                    RESOLVE_TOOL,
                    {
                        "snapshot_id": target.snapshot_id,
                        "scope_id": "managed",
                        "relative_path": "nested/change.txt",
                    },
                    read_timeout_seconds=timedelta(seconds=10),
                )
                assert not listed.isError and not prepared.isError and not resolved.isError

    anyio.run(exercise)
