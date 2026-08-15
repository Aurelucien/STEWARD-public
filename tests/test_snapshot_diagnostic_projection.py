"""Isolated repository-backed acceptance for Snapshot Diagnostic Projection."""

from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.database import database_path
from local_steward.payload_hashing import PayloadLocality, default_payload_hash_policy
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot
from local_steward.observation_projection import (
    BudgetValue,
    ProjectionBudget,
    ProjectionPolicy,
    SnapshotDiagnosticRequest,
    SourcePlanState,
    build_snapshot_diagnostic_projection,
)
from local_steward.observation_projection.errors import ObservationProjectionRequestError

from .test_protocol_completion import prepared_config


def _policy(*, duplicate: bool = False, relation: bool = False, explicit: int = 8) -> ProjectionPolicy:
    return ProjectionPolicy(
        0,
        "raw-path",
        ProjectionBudget(explicit, 16, 0, 4, 4, 2, 1, (("DIAGNOSTIC_BOUNDARY", explicit),), 100_000),
        duplicate,
        relation,
    )


def _config_with_files(tmp_path: Path):
    config = prepared_config(tmp_path)
    observed = tmp_path / "observed"
    observed.mkdir()
    (observed / "a.txt").write_text("same", encoding="utf-8")
    (observed / "b.txt").write_text("same", encoding="utf-8")
    nested = observed / "nested"
    nested.mkdir()
    (nested / "c.txt").write_text("different", encoding="utf-8")
    return replace(config, scopes=(replace(config.scopes[0], normalized_path=observed),))


def test_snapshot_diagnostic_service_builds_deterministic_v1_projection_without_writes(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    snapshot = create_snapshot(config, (), make_budget())
    database_before = database_path(config).read_bytes()
    evidence_before = {path.relative_to(config.paths.evidence_dir): path.read_bytes() for path in config.paths.evidence_dir.rglob("*.json")}
    request = SnapshotDiagnosticRequest(snapshot.snapshot_id)
    first = build_snapshot_diagnostic_projection(config, request, _policy())
    second = build_snapshot_diagnostic_projection(config, request, _policy())
    assert first.projection_digest == second.projection_digest
    assert first.facts.snapshot_diagnostic is not None
    assert first.facts.pair_tracking is None
    assert first.facts.source_identity.primary_snapshot is not None
    assert first.facts.accounting[0].source_count == first.facts.accounting[0].explicit_count + first.facts.accounting[0].aggregate_accounted_count
    assert database_path(config).read_bytes() == database_before
    assert {path.relative_to(config.paths.evidence_dir): path.read_bytes() for path in config.paths.evidence_dir.rglob("*.json")} == evidence_before


def test_snapshot_diagnostic_service_uses_requested_duplicate_overlay_and_bounded_anchors(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    snapshot = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    projection = build_snapshot_diagnostic_projection(
        config,
        SnapshotDiagnosticRequest(snapshot.snapshot_id, duplicate_overlay=SourcePlanState.REQUESTED_AND_EMPTY),
        _policy(duplicate=True, explicit=1),
    )
    body = projection.facts.snapshot_diagnostic
    assert body is not None
    assert len(body.explicit_entry_anchors) == 1
    assert body.duplicate_overlays
    assert any(item.state == SourcePlanState.REQUESTED_AND_PRESENT for item in projection.facts.source_plan if item.result_kind.value == "DUPLICATE")


def test_scope_path_errors_and_unresolved_budget_use_typed_failures(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    snapshot = create_snapshot(config, (), make_budget())
    with pytest.raises(ObservationProjectionRequestError) as error:
        build_snapshot_diagnostic_projection(config, SnapshotDiagnosticRequest(snapshot.snapshot_id, scope="missing"), _policy())
    assert error.value.code == "SCOPE_UNKNOWN"
    with pytest.raises(ObservationProjectionRequestError) as error:
        build_snapshot_diagnostic_projection(config, SnapshotDiagnosticRequest(snapshot.snapshot_id), ProjectionPolicy(0, "raw-path", ProjectionBudget(BudgetValue.REQUIRES_CALIBRATION, 1, 1, 1, 1, 1, 0, (), 1)))
    assert error.value.code == "BUDGET_INVALID"


def test_relation_context_is_explicit_and_does_not_create_pair_tracking(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    base = create_snapshot(config, (), make_budget())
    (config.scopes[0].normalized_path / "a.txt").write_text("changed", encoding="utf-8")
    target = create_snapshot(config, (), make_budget())
    projection = build_snapshot_diagnostic_projection(
        config,
        SnapshotDiagnosticRequest(base.snapshot_id, relation_context_pair=(base.snapshot_id, target.snapshot_id)),
        _policy(relation=True),
    )
    assert projection.facts.pair_tracking is None
    assert projection.facts.snapshot_diagnostic is not None
    assert any(item.result_kind.value == "RELATION" for item in projection.facts.source_plan)
