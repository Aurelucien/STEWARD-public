"""Isolated acceptance for the first supported verified Snapshot inspection path."""

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.database import database_path
from local_steward.file_agent import (
    AgentToolError,
    SharedToolBudget,
    SourceKind,
    ToolBudgetLimits,
    ToolExecutionContext,
    ToolResultStatus,
    serialize_envelope,
    steward_inspect_snapshot,
    steward_list_snapshots,
)
from local_steward.models import RunStatus
from local_steward.snapshots import get_snapshot, verify_snapshot

from .test_snapshot_queries import snapshot_fixture


_UNKNOWN_SNAPSHOT = "00000000-0000-4000-8000-000000000000"


def _arguments(config: object, *command: str, encoded: bool = False) -> list[str]:
    source_path = getattr(config, "source_path")
    arguments = ["--config", str(source_path)]
    if encoded:
        arguments.extend(("--format", "json"))
    arguments.extend(command)
    return arguments


def _invoke(config: object, *command: str, encoded: bool = False):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(app, _arguments(config, *command, encoded=encoded))


def _json(result: object) -> dict[str, object]:
    return json.loads(getattr(result, "stdout"))  # type: ignore[no-any-return]


def test_cli_verified_snapshot_golden_path_human_and_json(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    connection = sqlite3.connect(database_path(config))
    try:
        assert connection.execute("SELECT schema_version FROM schema_metadata").fetchone()[0] == 3
    finally:
        connection.close()

    status = _invoke(config, "storage", "status", encoded=True)
    inventory_human = _invoke(config, "snapshots", "list")
    inventory_json = _invoke(config, "snapshots", "list", encoded=True)
    verified = _invoke(config, "snapshots", "verify", snapshot.snapshot_id, encoded=True)
    shown_human = _invoke(config, "snapshots", "show", snapshot.snapshot_id)
    shown_json = _invoke(config, "snapshots", "show", snapshot.snapshot_id, encoded=True)
    entries_human = _invoke(
        config, "snapshots", "entries", snapshot.snapshot_id, "--scope", "managed", "--limit", "1"
    )
    entries_json = _invoke(
        config,
        "snapshots",
        "entries",
        snapshot.snapshot_id,
        "--scope",
        "managed",
        "--limit",
        "1",
        encoded=True,
    )

    assert all(
        result.exit_code == 0
        for result in (
            status,
            inventory_human,
            inventory_json,
            verified,
            shown_human,
            shown_json,
            entries_human,
            entries_json,
        )
    )
    assert _json(status)["result"]["storage_status"] == "HEALTHY"  # type: ignore[index]
    assert snapshot.snapshot_id in inventory_human.stdout
    assert _json(inventory_json)["command"] == "snapshots.list"
    assert _json(verified)["result"]["verification"]["status"] == "VALID"  # type: ignore[index]
    assert "Verification Status: VALID" in shown_human.stdout
    shown_result = _json(shown_json)["result"]
    assert shown_result["verification"]["status"] == "VALID"  # type: ignore[index]
    assert shown_result["snapshot"]["snapshot_digest"] == snapshot.snapshot_digest  # type: ignore[index]
    assert "Verification Status: VALID" in entries_human.stdout
    page = _json(entries_json)["result"]["page"]  # type: ignore[index]
    assert page["returned_count"] == 1 and page["limit"] == 1 and page["has_more"] is True  # type: ignore[index]
    assert page["entries"][0]["scope_id"] == "managed"  # type: ignore[index]


def test_cli_unknown_scope_and_snapshot_are_explicit(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)

    unknown_scope = _invoke(
        config,
        "snapshots",
        "entries",
        snapshot.snapshot_id,
        "--scope",
        "unknown",
        encoded=True,
    )
    unknown_snapshot = _invoke(
        config, "snapshots", "show", _UNKNOWN_SNAPSHOT, encoded=True
    )

    assert unknown_scope.exit_code == unknown_snapshot.exit_code == 2
    assert _json(unknown_scope)["errors"][0]["code"] == "SNAPSHOT_SCOPE_INVALID"  # type: ignore[index]
    assert _json(unknown_snapshot)["errors"][0]["code"] == "SNAPSHOT_NOT_FOUND"  # type: ignore[index]


def test_cli_verification_failure_never_publishes_snapshot_or_entries(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    connection = sqlite3.connect(database_path(config))
    try:
        connection.execute(
            "UPDATE runs SET status=? WHERE run_id=?",
            (RunStatus.PARTIAL.value, snapshot.run_id),
        )
        connection.commit()
    finally:
        connection.close()
    verification = verify_snapshot(config, snapshot.snapshot_id)
    assert verification.status == "INVALID"
    assert {item["code"] for item in verification.errors} == {"SNAPSHOT_RUN_STATUS_INVALID"}

    shown = _invoke(config, "snapshots", "show", snapshot.snapshot_id, encoded=True)
    entries = _invoke(config, "snapshots", "entries", snapshot.snapshot_id, encoded=True)

    assert shown.exit_code == entries.exit_code == 5
    for result, prohibited in ((shown, "snapshot"), (entries, "page")):
        payload = _json(result)
        assert payload["status"] == "INVALID"
        assert payload["result"]["verification"]["status"] == "INVALID"  # type: ignore[index]
        assert payload["errors"][0]["code"] == "SNAPSHOT_RUN_STATUS_INVALID"  # type: ignore[index]
        assert prohibited not in payload["result"]  # type: ignore[operator]
    with pytest.raises(AgentToolError) as api_failure:
        steward_inspect_snapshot(
            ToolExecutionContext(config, SharedToolBudget()), snapshot.snapshot_id
        )
    assert api_failure.value.code == "SNAPSHOT_NOT_VALID"


def test_cli_noncurrent_storage_uses_existing_migration_failure(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    connection = sqlite3.connect(database_path(config))
    try:
        connection.execute("UPDATE schema_metadata SET schema_version=2")
        connection.commit()
    finally:
        connection.close()

    result = _invoke(config, "snapshots", "show", snapshot.snapshot_id, encoded=True)

    assert result.exit_code == 3
    assert _json(result)["errors"][0]["code"] == "STORAGE_MIGRATION_REQUIRED"  # type: ignore[index]


def test_public_python_api_golden_path_and_typed_failures(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    stored = get_snapshot(config, snapshot.snapshot_id)
    context = ToolExecutionContext(
        config,
        SharedToolBudget(ToolBudgetLimits(max_items_per_call=1, max_items_per_turn=2)),
    )

    inventory = steward_list_snapshots(context, limit=1)
    inspection = steward_inspect_snapshot(
        context, snapshot.snapshot_id, scope_id="managed", limit=1
    )

    assert inventory.source_kind == inspection.source_kind == SourceKind.HISTORICAL_SNAPSHOT
    assert inspection.status == ToolResultStatus.PARTIAL_RESULT
    assert inspection.result["verification"]["status"] == "VALID"
    assert inspection.result["verification"]["evidence_id"] == stored.evidence_id
    assert inspection.result["page"]["returned_count"] == 1
    assert inspection.result["page"]["has_more"] is True
    assert inspection.snapshot_ids == (snapshot.snapshot_id,)
    assert inspection.scope_id == "managed"
    assert inspection.budget.consumed.calls == 2
    assert inspection.budget.consumed.items == 2
    assert "evidence_relative_path" not in serialize_envelope(inspection).decode("utf-8")

    with pytest.raises(AgentToolError) as unknown_scope:
        steward_inspect_snapshot(
            ToolExecutionContext(config, SharedToolBudget()),
            snapshot.snapshot_id,
            scope_id="unknown",
        )
    assert unknown_scope.value.code == "INVALID_ARGUMENT"
    with pytest.raises(AgentToolError) as unknown_snapshot:
        steward_inspect_snapshot(
            ToolExecutionContext(config, SharedToolBudget()), _UNKNOWN_SNAPSHOT
        )
    assert unknown_snapshot.value.code == "SNAPSHOT_NOT_FOUND"
    with pytest.raises(AgentToolError) as exhausted:
        steward_list_snapshots(
            ToolExecutionContext(
                config, SharedToolBudget(ToolBudgetLimits(max_steward_calls_per_turn=0))
            )
        )
    assert exhausted.value.code == "BUDGET_EXHAUSTED"
