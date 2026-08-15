"""Read-only integration coverage for the framework-neutral file-agent facade."""

from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.database import database_path
from local_steward.file_agent import (
    AgentToolError,
    SharedToolBudget,
    ToolBudgetLimits,
    ToolExecutionContext,
    ToolResultStatus,
    serialize_envelope,
    steward_compare_snapshots,
    steward_inspect_duplicates,
    steward_inspect_growth,
    steward_inspect_relations,
    steward_inspect_snapshot,
    steward_inspect_structure,
    steward_list_snapshots,
    steward_project_snapshot,
    steward_resolve_entry_reference,
)
import local_steward.file_agent as file_agent
from local_steward.models import FilesystemObjectType
from local_steward.observation_projection import (
    ProjectionBudget,
    ProjectionPolicy,
    SnapshotDiagnosticRequest,
)
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot, list_snapshot_entries

from .test_protocol_completion import prepared_config


def _policy() -> ProjectionPolicy:
    return ProjectionPolicy(
        0,
        "raw-path",
        ProjectionBudget(8, 8, 8, 4, 0, 2, 1, (("TRACKING_FACT", 8),), 100_000),
    )


def _fixture(tmp_path: Path):
    config = prepared_config(tmp_path)
    observed = tmp_path / "observed"
    observed.mkdir()
    (observed / "a.txt").write_text("before", encoding="utf-8")
    (observed / "dir").mkdir()
    (observed / "dir" / "child.txt").write_text("child", encoding="utf-8")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=observed),))
    base = create_snapshot(config, (), make_budget())
    (observed / "a.txt").write_text("after", encoding="utf-8")
    target = create_snapshot(config, (), make_budget())
    before_database = database_path(config).read_bytes()
    before_evidence = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }
    return config, base, target, before_database, before_evidence


def _context(config, limits: ToolBudgetLimits | None = None) -> ToolExecutionContext:  # type: ignore[no-untyped-def]
    return ToolExecutionContext(config, SharedToolBudget(limits))


def _assert_read_only(config, database_before: bytes, evidence_before: dict[Path, bytes]) -> None:  # type: ignore[no-untyped-def]
    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before


def test_all_nine_tools_wrap_read_only_stable_queries(tmp_path: Path) -> None:
    config, base, target, database_before, evidence_before = _fixture(tmp_path)
    context = _context(config)

    listed = steward_list_snapshots(context)
    inspected = steward_inspect_snapshot(
        context, target.snapshot_id, object_type=FilesystemObjectType.REGULAR_FILE, limit=1
    )
    structure = steward_inspect_structure(context, target.snapshot_id, depth=1, limit=10)
    compared = steward_compare_snapshots(context, base.snapshot_id, target.snapshot_id)
    growth = steward_inspect_growth(context, base.snapshot_id, target.snapshot_id, depth=1, limit=10)
    duplicates = steward_inspect_duplicates(context, target.snapshot_id, limit=10)
    relations = steward_inspect_relations(context, base.snapshot_id, target.snapshot_id, limit=10)
    projection = steward_project_snapshot(context, SnapshotDiagnosticRequest(target.snapshot_id), _policy())
    resolved = steward_resolve_entry_reference(context, target.snapshot_id, "managed", "a.txt")

    assert [item.tool_name for item in (
        listed, inspected, structure, compared, growth, duplicates, relations, projection, resolved
    )] == [
        "steward_list_snapshots", "steward_inspect_snapshot", "steward_inspect_structure",
        "steward_compare_snapshots", "steward_inspect_growth", "steward_inspect_duplicates",
        "steward_inspect_relations", "steward_project_snapshot", "steward_resolve_entry_reference",
    ]
    assert structure.result_digest and growth.result_digest and duplicates.result_digest and relations.result_digest
    assert projection.result_digest
    assert resolved.result["current_fact_requires_recheck"] is True
    assert resolved.result["recommended_realtime_query"] == "filesystem_metadata_or_search"
    assert "evidence_relative_path" not in serialize_envelope(inspected).decode("utf-8")
    _assert_read_only(config, database_before, evidence_before)


def test_entry_facade_preserves_flat_underlying_page_and_has_no_depth(tmp_path: Path) -> None:
    config, _base, target, database_before, evidence_before = _fixture(tmp_path)
    expected = list_snapshot_entries(config, target.snapshot_id, path_prefix="dir", limit=1, offset=0)
    envelope = steward_inspect_snapshot(_context(config), target.snapshot_id, path_prefix="dir", limit=1)

    assert [item["relative_path"] for item in envelope.result["page"]["entries"]] == [
        item.relative_path for item in expected.entries
    ]
    assert envelope.result["page"]["has_more"] == expected.has_more
    assert "depth" not in steward_inspect_snapshot.__annotations__
    _assert_read_only(config, database_before, evidence_before)


def test_budget_and_invalid_boundaries_are_safe(tmp_path: Path) -> None:
    config, _base, target, database_before, evidence_before = _fixture(tmp_path)
    partial = steward_inspect_snapshot(
        _context(config, ToolBudgetLimits(max_items_per_call=1)), target.snapshot_id, limit=10
    )
    assert partial.status == ToolResultStatus.PARTIAL_RESULT and partial.truncated
    with pytest.raises(AgentToolError, match="BUDGET_EXHAUSTED") as call_budget:
        steward_list_snapshots(_context(config, ToolBudgetLimits(max_steward_calls_per_turn=0)))
    assert call_budget.value.code == "BUDGET_EXHAUSTED"
    with pytest.raises(AgentToolError, match="INVALID_ARGUMENT"):
        steward_inspect_snapshot(_context(config), target.snapshot_id, path_prefix="../escape")
    with pytest.raises(AgentToolError, match="QUERY_TOO_BROAD"):
        steward_inspect_structure(_context(config, ToolBudgetLimits(max_depth=0)), target.snapshot_id, depth=1)
    with pytest.raises(AgentToolError, match="ENTRY_REFERENCE_NOT_FOUND"):
        steward_resolve_entry_reference(_context(config), target.snapshot_id, "managed", "missing.txt")
    _assert_read_only(config, database_before, evidence_before)


def test_entry_reference_requires_an_exact_historical_relative_path_without_fallback(tmp_path: Path) -> None:
    config, _base, target, database_before, evidence_before = _fixture(tmp_path)
    context = _context(config)

    resolved = steward_resolve_entry_reference(context, target.snapshot_id, "managed", "dir/child.txt")
    assert resolved.result["reference"]["relative_path"] == "dir/child.txt"
    for invalid_path in ("child.txt", "dir/chlid.txt"):
        with pytest.raises(AgentToolError, match="ENTRY_REFERENCE_NOT_FOUND"):
            steward_resolve_entry_reference(context, target.snapshot_id, "managed", invalid_path)
    with pytest.raises(AgentToolError, match="INVALID_ARGUMENT"):
        steward_resolve_entry_reference(context, target.snapshot_id, "managed", str(tmp_path / "observed" / "dir" / "child.txt"))
    _assert_read_only(config, database_before, evidence_before)


def test_turn_and_serialization_budgets_and_public_boundary(tmp_path: Path) -> None:
    config, _base, target, database_before, evidence_before = _fixture(tmp_path)
    turn_context = _context(config, ToolBudgetLimits(max_items_per_turn=1))
    steward_inspect_snapshot(turn_context, target.snapshot_id, limit=1)
    with pytest.raises(AgentToolError) as turn_budget:
        steward_inspect_snapshot(turn_context, target.snapshot_id, limit=1)
    assert turn_budget.value.code == "BUDGET_EXHAUSTED"
    with pytest.raises(AgentToolError) as byte_budget:
        steward_inspect_snapshot(
            _context(config, ToolBudgetLimits(max_serialized_bytes_per_call=1)), target.snapshot_id
        )
    assert byte_budget.value.code == "BUDGET_EXHAUSTED"
    assert "create_snapshot" not in file_agent.__all__
    assert "write_evidence" not in file_agent.__all__
    assert "rebuild_index" not in file_agent.__all__
    _assert_read_only(config, database_before, evidence_before)
