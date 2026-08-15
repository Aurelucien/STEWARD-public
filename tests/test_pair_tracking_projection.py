"""Isolated Pair Tracking Projection service tests."""

from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.database import database_path
from local_steward.models import (
    SnapshotBackupStatus,
    SnapshotReplacementStatus,
    SnapshotReplayStatus,
    SnapshotRollbackStatus,
)
from local_steward.observation_projection import (
    ObservationProjectionRequestError,
    PairTrackingRequest,
    ProjectionBudget,
    ProjectionPolicy,
    ResultKind,
    SourcePlanState,
    build_pair_tracking_projection,
)
from local_steward.payload_hashing import PayloadLocality, default_payload_hash_policy
from local_steward.scan_budget import make_budget
from local_steward.snapshot_backup import create_snapshot_index_backup
from local_steward.snapshot_replacement import replace_snapshot_index
from local_steward.snapshot_replay import replay_snapshot_index
from local_steward.snapshot_rollback import restore_snapshot_index_from_backup
from local_steward.snapshots import create_snapshot
from local_steward.storage import rebuild_index

from .test_protocol_completion import prepared_config


def _policy(*, tracking: int = 8, hierarchy: int = 8, relation: bool = False) -> ProjectionPolicy:
    return ProjectionPolicy(
        0,
        "raw-path",
        ProjectionBudget(8, hierarchy, tracking, 4, 0, 2, 1, (("TRACKING_FACT", tracking),), 100_000),
        relation_overlay=relation,
    )


def _config_with_files(tmp_path: Path):
    config = prepared_config(tmp_path)
    observed = tmp_path / "observed"
    observed.mkdir()
    (observed / "keep.txt").write_text("keep", encoding="utf-8")
    (observed / "change.txt").write_text("before", encoding="utf-8")
    return replace(config, scopes=(replace(config.scopes[0], normalized_path=observed),))


def _pair(config):
    base = create_snapshot(config, (), make_budget())
    root = config.scopes[0].normalized_path
    (root / "change.txt").write_text("after", encoding="utf-8")
    (root / "added.txt").write_text("added", encoding="utf-8")
    target = create_snapshot(config, (), make_budget())
    return base, target


def test_pair_tracking_builds_deterministic_diff_universe_without_writes(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    base, target = _pair(config)
    database_before = database_path(config).read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }
    request = PairTrackingRequest(base.snapshot_id, target.snapshot_id)
    first = build_pair_tracking_projection(config, request, _policy())
    second = build_pair_tracking_projection(config, request, _policy())
    body = first.facts.pair_tracking
    assert body is not None
    assert first.projection_digest == second.projection_digest
    assert first.facts.snapshot_diagnostic is None
    assert {(item.scope_id, item.relative_path) for item in body.tracking_items} == {
        ("managed", "."),
        ("managed", "added.txt"),
        ("managed", "change.txt"),
        ("managed", "keep.txt"),
    }
    assert {item.change_kind.value for item in body.tracking_items} >= {"ADDED", "MODIFIED", "UNCHANGED"}
    assert all(item.content_state.value == "UNKNOWN" for item in body.tracking_items)
    assert any(item.result_kind == ResultKind.GROWTH for item in first.facts.source_plan)
    assert body.growth_hierarchy.state == SourcePlanState.REQUESTED_AND_PRESENT
    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before


def test_pair_tracking_can_leave_growth_and_relation_not_requested(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    base, target = _pair(config)
    projection = build_pair_tracking_projection(
        config,
        PairTrackingRequest(
            base.snapshot_id,
            target.snapshot_id,
            growth=SourcePlanState.NOT_REQUESTED,
            relation=SourcePlanState.NOT_REQUESTED,
        ),
        _policy(),
    )
    body = projection.facts.pair_tracking
    assert body is not None
    assert body.growth_hierarchy.state == SourcePlanState.NOT_REQUESTED
    assert not body.growth_hierarchy.hierarchy_items
    assert all(item.result_kind != ResultKind.GROWTH or item.state == SourcePlanState.NOT_REQUESTED for item in projection.facts.source_plan)
    assert all(item.result_kind != ResultKind.RELATION or item.state == SourcePlanState.NOT_REQUESTED for item in projection.facts.source_plan)


def test_pair_tracking_budget_overflow_preserves_independent_accounting(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    base, target = _pair(config)
    projection = build_pair_tracking_projection(
        config,
        PairTrackingRequest(base.snapshot_id, target.snapshot_id),
        _policy(tracking=1, hierarchy=4),
    )
    body = projection.facts.pair_tracking
    assert body is not None
    location = next(item for item in projection.facts.accounting if item.domain.value == "PAIR_TRACKING_LOCATION")
    growth = next(item for item in projection.facts.accounting if item.domain.value == "GROWTH_REGULAR_LOCATION")
    hierarchy = next(item for item in projection.facts.accounting if item.domain.value == "PAIR_TRACKING_GROWTH_HIERARCHY")
    assert location.source_count == location.explicit_count + location.aggregate_accounted_count
    assert growth.source_count == growth.explicit_count + growth.aggregate_accounted_count
    assert hierarchy.source_count == hierarchy.explicit_count + hierarchy.aggregate_accounted_count
    assert len(body.tracking_items) == 1
    assert body.growth_hierarchy.hierarchy_items


def test_pair_tracking_uses_verified_v2_content_facts_without_rehashing(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    root = config.scopes[0].normalized_path
    base = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    (root / "change.txt").write_text("after", encoding="utf-8")
    target = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    projection = build_pair_tracking_projection(config, PairTrackingRequest(base.snapshot_id, target.snapshot_id), _policy())
    body = projection.facts.pair_tracking
    assert body is not None
    changed = next(item for item in body.tracking_items if item.relative_path == "change.txt")
    assert changed.content_state.value == "VERIFIED_CHANGED"
    assert changed.growth_contribution_reference is not None
    assert any(reason.value == "CONTENT_CHANGED" for reason in changed.selection_reasons)


def test_pair_tracking_supports_v1_to_v2_and_rejects_unknown_scope(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    root = config.scopes[0].normalized_path
    base = create_snapshot(config, (), make_budget())
    (root / "change.txt").write_text("after", encoding="utf-8")
    target = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    projection = build_pair_tracking_projection(
        config, PairTrackingRequest(base.snapshot_id, target.snapshot_id), _policy()
    )
    changed = next(
        item for item in projection.facts.pair_tracking.tracking_items
        if item.relative_path == "change.txt"
    )
    assert changed.content_state.value == "UNKNOWN"
    assert projection.facts.source_identity.snapshot_pair.base.schema_version == 1
    assert projection.facts.source_identity.snapshot_pair.target.schema_version == 2
    with pytest.raises(ObservationProjectionRequestError) as error:
        build_pair_tracking_projection(
            config,
            PairTrackingRequest(base.snapshot_id, target.snapshot_id, scope="missing"),
            _policy(),
        )
    assert error.value.code == "SCOPE_UNKNOWN"


def test_pair_tracking_relation_overlay_keeps_added_and_removed_locations_separate(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    observed = tmp_path / "observed"
    observed.mkdir()
    (observed / "old.txt").write_text("same-content", encoding="utf-8")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=observed),))
    base = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    (observed / "old.txt").unlink()
    (observed / "new.txt").write_text("same-content", encoding="utf-8")
    target = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    projection = build_pair_tracking_projection(
        config,
        PairTrackingRequest(
            base.snapshot_id,
            target.snapshot_id,
            relation=SourcePlanState.REQUESTED_AND_PRESENT,
        ),
        _policy(relation=True),
    )
    body = projection.facts.pair_tracking
    assert body is not None
    endpoints = {
        (item.relative_path, item.change_kind.value)
        for item in body.tracking_items
        if item.relative_path in {"old.txt", "new.txt"}
    }
    assert endpoints == {("old.txt", "REMOVED"), ("new.txt", "ADDED")}
    assert all(item.change_kind.value not in {"MOVED", "RENAMED"} for item in body.tracking_items)
    assert body.relation_overlays
    relation_accounting = next(
        item for item in projection.facts.accounting
        if item.domain.value == "RELATION_OVERLAY"
    )
    assert relation_accounting.source_count == (
        relation_accounting.explicit_count + relation_accounting.aggregate_accounted_count
    )


def test_pair_tracking_digest_survives_derived_index_recovery_chain(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    base, target = _pair(config)
    request = PairTrackingRequest(base.snapshot_id, target.snapshot_id)
    baseline = build_pair_tracking_projection(config, request, _policy()).projection_digest
    official = database_path(config)
    rebuild_index(config)
    assert build_pair_tracking_projection(config, request, _policy()).projection_digest == baseline

    backup = create_snapshot_index_backup(official, config.paths.cache_dir / "pair-tracking.sqlite3")
    assert backup.status == SnapshotBackupStatus.READY and backup.manifest is not None
    candidate = config.paths.cache_dir / "pair-tracking-candidate.sqlite3"
    replay = replay_snapshot_index(config, candidate)
    assert replay.status == SnapshotReplayStatus.READY and replay.replacement_ready
    replacement = replace_snapshot_index(config, replay)
    assert replacement.status == SnapshotReplacementStatus.REPLACED
    assert build_pair_tracking_projection(config, request, _policy()).projection_digest == baseline

    rollback = restore_snapshot_index_from_backup(config, backup, official)
    assert rollback.status == SnapshotRollbackStatus.RESTORED
    assert build_pair_tracking_projection(config, request, _policy()).projection_digest == baseline
