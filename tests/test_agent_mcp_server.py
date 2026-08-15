from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import anyio
import pytest

from local_steward.agent_mcp_server import (
    AgentContextRouteDispatcher,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    TOOL_NAME,
    create_server,
    tool_descriptors,
)
from local_steward.agent_mcp_server.protocol import (
    ADAPTER_SCHEMA_NAME,
    ADAPTER_SCHEMA_VERSION,
    EXACT_INTEGER_ENCODING_SCHEME,
)
from local_steward.config import load_config
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
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=observed),))
    initialize_storage(config)
    base = create_snapshot(config, (), make_budget())
    (nested / "change.txt").write_text("after", encoding="utf-8")
    target = create_snapshot(config, (), make_budget())
    return config, base, target


def _arguments(
    operation: str,
    snapshots: list[str] | None = None,
    *,
    scope: str | None = None,
    path: str | None = None,
    bounds: dict[str, int] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "operation_kind": operation,
        "question": "Explain only the exact bounded historical evidence.",
    }
    if snapshots is not None:
        value["ordered_snapshot_ids"] = snapshots
    if scope is not None:
        value["scope_id"] = scope
    if path is not None:
        value["path_or_prefix"] = path
    if bounds is not None:
        value["bounds"] = bounds
    return value


def _call(
    dispatcher: AgentContextRouteDispatcher,
    arguments: object,
    *,
    tool_name: str = TOOL_NAME,
):  # type: ignore[no-untyped-def]
    async def run():  # type: ignore[no-untyped-def]
        return await dispatcher.dispatch(tool_name, arguments)

    return anyio.run(run)


def _structured(result) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    assert isinstance(result.structuredContent, dict)
    return result.structuredContent


def test_single_tool_surface_is_closed_and_non_idempotent(tmp_path: Path) -> None:
    config, _base, _target = _fixture(tmp_path)
    server = create_server(AgentContextRouteDispatcher(config.source_path))
    descriptors = tool_descriptors()

    assert server.name == SERVER_NAME
    assert server.instructions == SERVER_INSTRUCTIONS
    assert [item.name for item in descriptors] == [TOOL_NAME]
    descriptor = descriptors[0]
    assert descriptor.annotations is not None
    assert descriptor.annotations.readOnlyHint is True
    assert descriptor.annotations.destructiveHint is False
    assert descriptor.annotations.idempotentHint is False
    assert descriptor.inputSchema["additionalProperties"] is False
    assert descriptor.outputSchema is not None


@pytest.mark.parametrize(
    ("arguments", "decision", "code"),
    [
        (
            _arguments("CONFIGURATION_OR_HEALTH"),
            "CORE",
            "STEWARD_ROUTE_CORE_REQUIRED",
        ),
        (
            _arguments("ORDERED_HISTORICAL_CHANGE_EXPLANATION"),
            "CLARIFY",
            "STEWARD_ROUTE_CLARIFICATION_REQUIRED",
        ),
        (
            _arguments("GENERIC_CONTEXT_MAGIC"),
            "UNSUPPORTED",
            "STEWARD_ROUTE_UNSUPPORTED",
        ),
    ],
)
def test_non_context_routes_publish_no_context_business_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    decision: str,
    code: str,
) -> None:
    config, _base, _target = _fixture(tmp_path)
    invoked = False

    def forbidden(*_args: object, **_kwargs: object) -> None:
        nonlocal invoked
        invoked = True

    monkeypatch.setattr(
        "local_steward.agent_mcp_server.adapter.prepare_agent_context", forbidden
    )
    result = _call(AgentContextRouteDispatcher(config.source_path), arguments)
    value = _structured(result)

    assert result.isError is True
    assert value["status"] == "ERROR"
    assert value["route"]["decision"] == decision
    assert value["error"]["code"] == code
    assert value["context_pack"] is None
    assert "Business result: `NONE`" in value["fact_block_markdown"] or (
        "NO_BUSINESS_RESULT" in value["fact_block_markdown"]
    )
    assert sha256(value["fact_block_markdown"].encode()).hexdigest() == value[
        "fact_block_sha256"
    ]
    assert invoked is False


def test_exact_context_routes_consume_grants_and_publish_complete_envelopes(
    tmp_path: Path,
) -> None:
    config, base, target = _fixture(tmp_path)
    dispatcher = AgentContextRouteDispatcher(config.source_path)
    before = {
        path.relative_to(config.paths.data_dir).as_posix(): path.read_bytes()
        for path in config.paths.data_dir.rglob("*")
        if path.is_file()
    }
    cases = (
        _arguments(
            "BOUNDED_STRUCTURAL_DIAGNOSTIC",
            [target.snapshot_id],
            scope="managed",
            bounds={"limit": 4, "offset": 0},
        ),
        _arguments(
            "ORDERED_HISTORICAL_CHANGE_EXPLANATION",
            [base.snapshot_id, target.snapshot_id],
            scope="managed",
            path="nested",
            bounds={"limit": 4, "offset": 0},
        ),
    )
    values: list[dict[str, Any]] = []
    for arguments in cases:
        result = _call(dispatcher, arguments)
        value = _structured(result)
        values.append(value)
        assert result.isError is False
        assert value["schema_name"] == ADAPTER_SCHEMA_NAME
        assert value["schema_version"] == ADAPTER_SCHEMA_VERSION
        assert value["status"] == "OK"
        assert value["route"]["decision"] == "CONTEXT"
        assert value["route"]["grant"]["reusable"] is False
        assert value["context_pack"]["pack_digest"]
        assert value["publication"]["route_decision"] == "CONTEXT"
        assert value["publication"]["fact_block_markdown"] == value[
            "fact_block_markdown"
        ]
        assert value["publication"]["fact_block_sha256"] == value["fact_block_sha256"]
        assert value["publication"]["source_provenance"]
        assert "HISTORICAL_NOT_CURRENT" in value["publication"]["authority_boundary"]
        assert value["exact_integer_encoding"]["scheme"] == (
            EXACT_INTEGER_ENCODING_SCHEME
        )
        assert set(value["exact_integer_encoding"]["decimal_string_paths"]) == {
            item["json_pointer"]
            for item in value["publication"]["exact_integer_encoding"]
        }
        assert result.content[0].text == value["fact_block_markdown"]
    assert [item["snapshot_id"] for item in values[1]["publication"]["source_provenance"]] == [
        base.snapshot_id,
        target.snapshot_id,
    ]
    after = {
        path.relative_to(config.paths.data_dir).as_posix(): path.read_bytes()
        for path in config.paths.data_dir.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not list(config.paths.data_dir.glob("state.db-*"))


def test_identical_grant_cannot_be_reused_in_one_server_process(tmp_path: Path) -> None:
    config, _base, target = _fixture(tmp_path)
    dispatcher = AgentContextRouteDispatcher(config.source_path)
    arguments = _arguments(
        "BOUNDED_STRUCTURAL_DIAGNOSTIC",
        [target.snapshot_id],
        scope="managed",
        bounds={"limit": 4, "offset": 0},
    )

    first = _call(dispatcher, arguments)
    second = _call(dispatcher, arguments)

    assert first.isError is False
    assert second.isError is True
    assert _structured(second)["error"]["code"] == "STEWARD_ROUTE_GRANT_INVALID"
    assert _structured(second)["context_pack"] is None


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"operation_kind": "lowercase", "question": "q"},
        {
            "operation_kind": "BOUNDED_STRUCTURAL_DIAGNOSTIC",
            "question": "q",
            "ordered_snapshot_ids": ["not-a-uuid"],
        },
        {
            "operation_kind": "BOUNDED_STRUCTURAL_DIAGNOSTIC",
            "question": "q",
            "bounds": {"limit": 13, "offset": 0},
        },
    ],
)
def test_malformed_and_unknown_tool_calls_fail_before_route_access(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    config, _base, _target = _fixture(tmp_path)
    dispatcher = AgentContextRouteDispatcher(config.source_path)
    result = _call(dispatcher, arguments)
    unknown = _call(dispatcher, arguments, tool_name="not-a-tool")
    for item in (result, unknown):
        value = _structured(item)
        assert item.isError is True
        assert value["error"]["code"] == "STEWARD_AGENT_MCP_ARGUMENT_INVALID"
        assert value["context_pack"] is None
        assert str(tmp_path) not in json.dumps(value)


def test_oversized_success_becomes_typed_failure(tmp_path: Path) -> None:
    config, _base, target = _fixture(tmp_path)
    result = _call(
        AgentContextRouteDispatcher(config.source_path, max_result_bytes=100),
        _arguments(
            "BOUNDED_STRUCTURAL_DIAGNOSTIC",
            [target.snapshot_id],
            scope="managed",
            bounds={"limit": 4, "offset": 0},
        ),
    )
    value = _structured(result)
    assert result.isError is True
    assert value["error"]["code"] == "STEWARD_AGENT_MCP_RESOURCE_LIMIT"
    assert value["context_pack"] is None
