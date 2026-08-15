"""Classified operational replay keeps invalid history visible but out of business state."""

from dataclasses import replace
import json
import sqlite3
from pathlib import Path

import pytest

from local_steward.database import SCHEMA_VERSION, database_path
from local_steward.doctor import run_doctor
from local_steward.models import (
    EvidenceIntegrityStatus,
    ReplayEligibility,
    RunStatus,
    SemanticConsistencyStatus,
    SnapshotReplayStatus,
)
from local_steward.scan_budget import make_budget
from local_steward.snapshot_replay import (
    replay_classified_operational_index,
    replay_snapshot_index,
)
from local_steward.snapshots import create_snapshot
from local_steward.storage import (
    classify_operational_evidence,
    storage_status,
    validate_classified_replay_plan,
)

from .test_snapshot_queries import snapshot_fixture


def _database_counts(path: Path) -> tuple[int, int, int, int]:
    connection = sqlite3.connect(path)
    try:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("runs", "evidence_records", "snapshots", "snapshot_entries")
        )
    finally:
        connection.close()


def _preserve_scanning_partial(config, snapshot) -> None:
    """Model the governed interrupted attempt without inventing a terminal transition."""
    run_dir = config.paths.evidence_dir / "runs" / snapshot.run_id
    files = sorted(run_dir.glob("*.json"))
    final = json.loads(files[-1].read_text(encoding="utf-8"))
    assert final["evidence_type"] == "run.state_transition"
    assert final["payload"]["to_status"] == RunStatus.PARTIAL.value
    files[-1].unlink()
    prior = json.loads(files[-2].read_text(encoding="utf-8"))
    connection = sqlite3.connect(database_path(config))
    try:
        connection.execute(
            "UPDATE runs SET status=?, updated_at=?, last_sequence=?, "
            "last_evidence_digest=?, terminal=0 WHERE run_id=?",
            (
                RunStatus.SCANNING.value,
                prior["created_at"],
                prior["sequence"],
                prior["evidence_digest"],
                snapshot.run_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _mixed_corpus(tmp_path: Path):
    config, eligible = snapshot_fixture(tmp_path)
    ineligible = create_snapshot(config, (), make_budget(max_entries=1))
    _preserve_scanning_partial(config, ineligible)
    return config, eligible, ineligible


def _candidate_config(config, destination: Path):
    return replace(config, paths=replace(config.paths, data_dir=destination.parent))


def test_fully_eligible_operational_replay_has_no_diagnostics(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    destination = tmp_path / "candidate" / "state.db"
    destination.parent.mkdir()
    report = replay_classified_operational_index(config, destination)
    assert report.status == SnapshotReplayStatus.READY and report.candidate_ready
    assert report.operational_storage_status == "HEALTHY"
    assert not report.historical_diagnostics_present
    assert _database_counts(destination) == (1, 4, 1, snapshot.entry_count)


def test_mixed_corpus_replays_only_eligible_business_rows(tmp_path: Path) -> None:
    config, eligible, ineligible = _mixed_corpus(tmp_path)
    destination = tmp_path / "candidate" / "state.db"
    destination.parent.mkdir()
    report = replay_classified_operational_index(config, destination)
    assert report.status == SnapshotReplayStatus.READY and report.candidate_ready
    assert report.historical_diagnostics_present
    assert report.excluded_snapshot_count == 1
    assert report.excluded_entry_count == ineligible.entry_count
    assert _database_counts(destination) == (2, 7, 1, eligible.entry_count)
    excluded = next(
        item for item in report.plan.snapshots if item.snapshot_id == ineligible.snapshot_id
    )
    assert excluded.evidence_integrity == EvidenceIntegrityStatus.VALID
    assert excluded.semantic_consistency == SemanticConsistencyStatus.INCONSISTENT
    assert excluded.replay_eligibility == ReplayEligibility.INELIGIBLE
    assert excluded.reason_codes == ("SNAPSHOT_RUN_STATUS_INVALID",)
    assert excluded.evidence_id and excluded.evidence_digest and excluded.evidence_relative_path
    assert len(excluded.entries) == ineligible.entry_count
    assert all(
        entry.entry_id
        and entry.snapshot_id == ineligible.snapshot_id
        and entry.scope_id
        and entry.evidence_id == excluded.evidence_id
        and entry.evidence_digest == excluded.evidence_digest
        and entry.replay_eligibility == ReplayEligibility.INELIGIBLE
        and entry.reason_codes == ("SNAPSHOT_RUN_STATUS_INVALID",)
        for entry in excluded.entries
    )

    connection = sqlite3.connect(destination)
    try:
        assert connection.execute(
            "SELECT count(*) FROM snapshots WHERE snapshot_id=?", (ineligible.snapshot_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM snapshot_entries WHERE snapshot_id=?", (ineligible.snapshot_id,)
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_mixed_corpus_still_fails_strict_replay(tmp_path: Path) -> None:
    config, _eligible, _ineligible = _mixed_corpus(tmp_path)
    report = replay_snapshot_index(config, tmp_path / "strict.sqlite3")
    assert report.status == SnapshotReplayStatus.FAILED
    assert "SNAPSHOT_RUN_STATUS_INVALID" in {item["code"] for item in report.issues}


def test_corruption_hard_stops_before_candidate_creation(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    created = config.paths.evidence_dir / "runs" / snapshot.run_id / "00000001_run.created.json"
    document = json.loads(created.read_text(encoding="utf-8"))
    document["evidence_digest"] = "0" * 64
    created.write_text(json.dumps(document), encoding="utf-8")
    destination = tmp_path / "candidate.sqlite3"
    report = replay_classified_operational_index(config, destination)
    assert report.status == SnapshotReplayStatus.FAILED and not report.candidate_ready
    assert not destination.exists()
    assert any(item.evidence_integrity == EvidenceIntegrityStatus.INVALID for item in report.plan.runs)
    assert report.plan.total_snapshot_count == 1
    assert report.plan.total_snapshot_entry_count > 0


def test_unknown_evidence_version_hard_stops(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    created = config.paths.evidence_dir / "runs" / snapshot.run_id / "00000001_run.created.json"
    document = json.loads(created.read_text(encoding="utf-8"))
    document["schema_version"] = 999
    created.write_text(json.dumps(document), encoding="utf-8")
    destination = tmp_path / "candidate.sqlite3"
    report = replay_classified_operational_index(config, destination)
    assert report.status == SnapshotReplayStatus.FAILED and not destination.exists()
    assert report.plan.runs[0].evidence_integrity == EvidenceIntegrityStatus.UNKNOWN


@pytest.mark.parametrize(
    ("consistency", "eligibility"),
    [
        (SemanticConsistencyStatus.UNKNOWN, None),
        (SemanticConsistencyStatus.CONSISTENT, None),
    ],
)
def test_unknown_or_unclassified_snapshot_hard_stops(
    consistency: SemanticConsistencyStatus,
    eligibility: ReplayEligibility | None,
    tmp_path: Path,
) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    plan = classify_operational_evidence(config)
    broken = replace(
        plan,
        snapshots=(replace(plan.snapshots[0], semantic_consistency=consistency, replay_eligibility=eligibility),),
        classification_complete=False,
    )
    assert validate_classified_replay_plan(broken)


def test_missing_authoritative_run_hard_stops(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    created = config.paths.evidence_dir / "runs" / snapshot.run_id / "00000001_run.created.json"
    created.unlink()
    destination = tmp_path / "candidate.sqlite3"
    report = replay_classified_operational_index(config, destination)
    assert report.status == SnapshotReplayStatus.FAILED and not destination.exists()


def test_operational_health_is_healthy_with_visible_historical_diagnostics(
    tmp_path: Path,
) -> None:
    config, _eligible, ineligible = _mixed_corpus(tmp_path)
    destination = tmp_path / "candidate" / "state.db"
    destination.parent.mkdir()
    replay = replay_classified_operational_index(config, destination)
    assert replay.status == SnapshotReplayStatus.READY
    result = storage_status(_candidate_config(config, destination))
    assert result.storage_status == "HEALTHY"
    assert result.snapshot_integrity is not None
    assert result.snapshot_integrity.status != "HEALTHY"
    assert len(result.historical_evidence_diagnostics) == 1
    diagnostic = result.historical_evidence_diagnostics[0]
    assert diagnostic["persistent_run_id"] == ineligible.run_id
    assert diagnostic["code"] == "SNAPSHOT_RUN_STATUS_INVALID"
    doctor = run_doctor(_candidate_config(config, destination))
    storage_check = next(item for item in doctor.checks if item.check_id == "storage_index")
    assert storage_check.status.value == "AVAILABLE"
    assert storage_check.details["historical_evidence_diagnostic_count"] == 1


def test_candidate_digest_failure_rolls_back_without_changing_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    source_database = database_path(config).read_bytes()
    source_evidence = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr("local_steward.snapshot_replay._destination_digest", lambda _conn: "bad")
    destination = tmp_path / "candidate.sqlite3"
    report = replay_classified_operational_index(config, destination)
    assert report.status == SnapshotReplayStatus.FAILED and not report.candidate_ready
    assert "OPERATIONAL_REPLAY_DIGEST_MISMATCH" in {item["code"] for item in report.issues}
    assert _database_counts(destination) == (0, 0, 0, 0)
    assert database_path(config).read_bytes() == source_database
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*")
        if path.is_file()
    } == source_evidence


def test_classification_and_accounting_are_deterministic(tmp_path: Path) -> None:
    config, _eligible, _ineligible = _mixed_corpus(tmp_path)
    first = classify_operational_evidence(config)
    second = classify_operational_evidence(config)
    assert first == second
    assert first.accounting_digest == second.accounting_digest
    assert first.classification_complete and not validate_classified_replay_plan(first)


def test_live_database_target_is_never_opened_for_operational_replay(tmp_path: Path) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    before = database_path(config).read_bytes()
    report = replay_classified_operational_index(config, database_path(config))
    assert report.status == SnapshotReplayStatus.FAILED
    assert report.issues[0]["code"] == "OPERATIONAL_REPLAY_TARGET_IS_LIVE_DATABASE"
    assert database_path(config).read_bytes() == before
    assert SCHEMA_VERSION == 3
