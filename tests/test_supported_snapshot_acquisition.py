"""NEXT-001B isolated supported Snapshot acquisition and recovery acceptance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.errors import (
    SnapshotAcquisitionConfirmationError,
    SnapshotAcquisitionCancelledError,
    SnapshotAcquisitionIntegrityError,
    SnapshotAcquisitionNotGovernedError,
    SnapshotAcquisitionRecoveryRequiredError,
    SnapshotBudgetError,
    SnapshotScopeError,
    StorageBusyError,
)
from local_steward.faults import FaultInjectionError
from local_steward.models import RunStatus, ScanBudget
from local_steward.output import to_jsonable
from local_steward.runs import create_run, get_run, transition_run
from local_steward.snapshot_acquisition import (
    SnapshotAcquisitionRequest,
    _AcquisitionLease,
    _acquire_snapshot,
    _recover_snapshot_acquisition,
    _writer_handoff,
    acquire_snapshot,
    recover_snapshot_acquisition,
    snapshot_acquisition_status,
)
from local_steward.snapshot_replay import (
    replay_classified_operational_index,
    replay_snapshot_index,
)
from local_steward.snapshots import (
    _persist,
    _snapshot,
    create_snapshot,
    list_snapshots,
    verify_snapshot,
)
from local_steward.storage import (
    classify_operational_evidence,
    storage_status,
    verify_evidence_report,
)

from .test_protocol_completion import prepared_config


class _FailAt:
    def __init__(self, operation: str, stage: str) -> None:
        self.operation = operation
        self.stage = stage
        self.calls: list[tuple[str, str]] = []

    def inject(self, operation: str, stage: str) -> None:
        self.calls.append((operation, stage))
        if (operation, stage) == (self.operation, self.stage):
            raise FaultInjectionError(f"{operation}:{stage}")


class _CancelAt:
    def __init__(self, operation: str, stage: str) -> None:
        self.operation = operation
        self.stage = stage

    def inject(self, operation: str, stage: str) -> None:
        if (operation, stage) == (self.operation, self.stage):
            raise KeyboardInterrupt


def _configured(tmp_path: Path):
    config = prepared_config(tmp_path)
    root = tmp_path / "observed"
    root.mkdir()
    (root / "alpha.txt").write_text("alpha", encoding="utf-8")
    child = root / "child"
    child.mkdir()
    (child / "beta.txt").write_text("beta", encoding="utf-8")
    return replace(config, scopes=(replace(config.scopes[0], normalized_path=root),)), root


def _source_manifest(root: Path) -> tuple[tuple[str, str, int, int], ...]:
    facts: list[tuple[str, str, int, int]] = []
    for path in sorted((root, *root.rglob("*")), key=lambda item: str(item)):
        info = path.lstat()
        kind = "directory" if path.is_dir() else "file"
        content = path.read_bytes().hex() if path.is_file() else ""
        facts.append((str(path.relative_to(root)), f"{kind}:{content}", info.st_mtime_ns, info.st_mode))
    return tuple(facts)


def _only_run_id(config) -> str:
    directories = sorted(
        path.name for path in (config.paths.evidence_dir / "runs").iterdir() if path.is_dir()
    )
    assert len(directories) == 1
    return directories[0]


def test_complete_acquisition_is_terminal_verified_and_path_free(tmp_path: Path) -> None:
    config, root = _configured(tmp_path)
    before = _source_manifest(root)

    report = acquire_snapshot(
        config, SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True)
    )

    assert report.disposition == "COMPLETE"
    assert report.run_status == "verified" and report.run_terminal
    assert report.snapshot_status == "complete" and report.entry_count == 4
    assert report.verification is not None and report.verification.status == "VALID"
    assert str(root) not in json.dumps(to_jsonable(report))
    assert _source_manifest(root) == before
    run = get_run(config, report.run_id)
    assert run.status == RunStatus.VERIFIED and run.terminal
    assert list_snapshots(config)[0].snapshot_id == report.snapshot_id
    assert verify_snapshot(config, report.snapshot_id or "").status == "VALID"
    assert storage_status(config).storage_status == "HEALTHY"
    evidence = verify_evidence_report(config, report.run_id)
    assert evidence.verifications[0].status == "VALID"
    assert evidence.snapshot_evidence.items[0].valid
    created = next(
        config.paths.evidence_dir.glob(f"runs/{report.run_id}/00000001_run.created.json")
    ).read_text(encoding="utf-8")
    assert str(root) not in created


def test_budget_exhaustion_is_truthful_terminal_partial(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)

    report = acquire_snapshot(
        config, SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=1), True)
    )

    assert report.disposition == "PARTIAL"
    assert report.snapshot_status == "partial" and report.run_status == "verified"
    assert report.verification is not None and report.verification.status == "VALID"


def test_acquisition_status_never_reopens_the_current_scope(tmp_path: Path) -> None:
    config, root = _configured(tmp_path)
    completed = acquire_snapshot(
        config, SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True)
    )
    os.replace(root, root.parent / "scope-removed-after-acquisition")

    status = snapshot_acquisition_status(config, completed.run_id)

    assert status.disposition == "COMPLETE"
    assert status.verification is not None and status.verification.status == "VALID"


@pytest.mark.parametrize(
    "budget",
    (
        ScanBudget(max_entries=True),
        ScanBudget(max_entries=1_000_001),
        ScanBudget(max_duration_seconds=float("nan")),
        ScanBudget(max_duration_seconds=601),
        ScanBudget(max_total_stat_bytes=True),
        ScanBudget(max_depth=True),
    ),
)
def test_admission_rejects_invalid_budgets_without_evidence(
    tmp_path: Path, budget: ScanBudget
) -> None:
    config, _root = _configured(tmp_path)
    with pytest.raises(SnapshotBudgetError):
        acquire_snapshot(config, SnapshotAcquisitionRequest("managed", budget, True))
    assert not (config.paths.evidence_dir / "runs").exists()


def test_confirmation_and_scope_policy_fail_before_run(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)
    with pytest.raises(SnapshotAcquisitionConfirmationError):
        acquire_snapshot(config, SnapshotAcquisitionRequest("managed"))
    with pytest.raises(SnapshotScopeError):
        acquire_snapshot(config, SnapshotAcquisitionRequest("unknown", confirmed=True))
    cross_mount = replace(
        config, scopes=(replace(config.scopes[0], allow_cross_mount=True),)
    )
    with pytest.raises(SnapshotScopeError):
        acquire_snapshot(cross_mount, SnapshotAcquisitionRequest("managed", confirmed=True))
    assert not (config.paths.evidence_dir / "runs").exists()


def test_disabled_missing_and_symlink_roots_fail_before_run(tmp_path: Path) -> None:
    config, root = _configured(tmp_path)
    disabled = replace(config, scopes=(replace(config.scopes[0], enabled=False),))
    missing = replace(
        config,
        scopes=(replace(config.scopes[0], normalized_path=tmp_path / "missing"),),
    )
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(root, target_is_directory=True)
    symlinked = replace(
        config,
        scopes=(replace(config.scopes[0], normalized_path=linked_root),),
    )

    for rejected in (disabled, missing, symlinked):
        with pytest.raises(SnapshotScopeError):
            acquire_snapshot(
                rejected, SnapshotAcquisitionRequest("managed", confirmed=True)
            )
    assert not (config.paths.evidence_dir / "runs").exists()


def test_nonwritable_source_is_observed_without_mutation(tmp_path: Path) -> None:
    config, root = _configured(tmp_path)
    before = _source_manifest(root)
    original_mode = root.stat().st_mode
    root.chmod(0o555)
    try:
        protected_before = _source_manifest(root)
        report = acquire_snapshot(config, SnapshotAcquisitionRequest("managed", confirmed=True))
        assert report.disposition == "COMPLETE"
        assert _source_manifest(root) == protected_before
    finally:
        root.chmod(original_mode)
    assert _source_manifest(root) == before


@pytest.mark.parametrize(
    ("operation", "stage", "expected"),
    (
        ("run.create", "after_evidence_publish", "FAILED"),
        ("run.create", "before_index_commit", "FAILED"),
        ("run.create", "after_index_commit", "FAILED"),
        ("run.transition.scanning", "before_evidence_publish", "FAILED"),
        ("run.transition.scanning", "after_evidence_publish", "FAILED"),
        ("run.transition.scanning", "before_index_commit", "FAILED"),
        ("run.transition.scanning", "after_index_commit", "FAILED"),
        ("snapshot.persist", "before_evidence_publish", "FAILED"),
        ("snapshot.persist", "after_evidence_publish", "COMPLETE"),
        ("snapshot.persist", "before_index_commit", "COMPLETE"),
        ("snapshot.persist", "after_index_commit", "COMPLETE"),
        ("run.transition.scanned", "before_evidence_publish", "COMPLETE"),
        ("run.transition.scanned", "after_evidence_publish", "COMPLETE"),
        ("run.transition.scanned", "before_index_commit", "COMPLETE"),
        ("run.transition.scanned", "after_index_commit", "COMPLETE"),
        ("run.transition.verifying", "before_evidence_publish", "COMPLETE"),
        ("run.transition.verifying", "after_evidence_publish", "COMPLETE"),
        ("run.transition.verifying", "before_index_commit", "COMPLETE"),
        ("run.transition.verifying", "after_index_commit", "COMPLETE"),
        ("run.transition.verified", "before_evidence_publish", "COMPLETE"),
        ("run.transition.verified", "after_evidence_publish", "COMPLETE"),
        ("run.transition.verified", "before_index_commit", "COMPLETE"),
        ("run.transition.verified", "after_index_commit", "COMPLETE"),
        ("acquisition", "before_scope_observation", "FAILED"),
        ("acquisition", "after_scope_observation", "FAILED"),
        ("acquisition", "before_authoritative_verify", "COMPLETE"),
        ("acquisition", "after_authoritative_verify", "COMPLETE"),
        ("acquisition", "before_result_publication", "COMPLETE"),
    ),
)
def test_every_durable_prefix_has_explicit_idempotent_recovery(
    tmp_path: Path, operation: str, stage: str, expected: str
) -> None:
    config, _root = _configured(tmp_path)
    injector = _FailAt(operation, stage)
    with pytest.raises(SnapshotAcquisitionRecoveryRequiredError):
        _acquire_snapshot(
            config,
            SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True),
            fault_injector=injector,
        )
    run_id = _only_run_id(config)
    first = recover_snapshot_acquisition(config, run_id, confirmed=True)
    second = recover_snapshot_acquisition(config, run_id, confirmed=True)
    assert first.disposition == second.disposition == expected
    assert snapshot_acquisition_status(config, run_id).disposition == expected
    assert not list(config.paths.data_dir.glob("state.db-*"))
    assert not list(config.paths.cache_dir.glob(".snapshot-acquisition-replay-*.db"))


def test_fault_before_first_evidence_is_not_misrepresented_as_a_run(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)
    with pytest.raises(SnapshotAcquisitionRecoveryRequiredError) as captured:
        _acquire_snapshot(
            config,
            SnapshotAcquisitionRequest("managed", confirmed=True),
            fault_injector=_FailAt("run.create", "before_evidence_publish"),
        )
    assert "before durable Run publication" in str(captured.value)
    assert not (config.paths.evidence_dir / "runs").exists()


def test_explicit_cancellation_before_snapshot_is_terminal_and_truthful(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)
    with pytest.raises(SnapshotAcquisitionCancelledError) as captured:
        _acquire_snapshot(
            config,
            SnapshotAcquisitionRequest("managed", confirmed=True),
            fault_injector=_CancelAt("acquisition", "before_scope_observation"),
        )
    run_id = _only_run_id(config)
    assert f"run_id={run_id}" in str(captured.value)
    status = snapshot_acquisition_status(config, run_id)
    assert status.disposition == "CANCELLED" and status.run_terminal
    assert status.snapshot_id is None


def test_partial_transition_fault_recovers_to_terminal_partial(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)
    with pytest.raises(SnapshotAcquisitionRecoveryRequiredError):
        _acquire_snapshot(
            config,
            SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=1), True),
            fault_injector=_FailAt("run.transition.partial", "after_evidence_publish"),
        )
    report = recover_snapshot_acquisition(config, _only_run_id(config), confirmed=True)
    assert report.disposition == "PARTIAL" and report.run_status == "verified"


@pytest.mark.parametrize(
    ("operation", "stage"),
    (
        ("acquisition.recovery.append.failed", "after_evidence_publish"),
        ("acquisition.recovery.rebuild", "before_candidate"),
    ),
)
def test_failed_terminal_recovery_repairs_a_second_interrupted_index_handoff(
    tmp_path: Path, operation: str, stage: str
) -> None:
    config, _root = _configured(tmp_path)
    with pytest.raises(SnapshotAcquisitionRecoveryRequiredError):
        _acquire_snapshot(
            config,
            SnapshotAcquisitionRequest("managed", confirmed=True),
            fault_injector=_FailAt(
                "run.transition.scanning", "before_evidence_publish"
            ),
        )
    run_id = _only_run_id(config)
    with pytest.raises(SnapshotAcquisitionRecoveryRequiredError):
        _recover_snapshot_acquisition(
            config,
            run_id,
            confirmed=True,
            fault_injector=_FailAt(operation, stage),
        )
    assert snapshot_acquisition_status(config, run_id).disposition == (
        "RECOVERY_REQUIRED_INDEX"
    )

    recovered = recover_snapshot_acquisition(config, run_id, confirmed=True)
    assert recovered.disposition == "FAILED"
    assert snapshot_acquisition_status(config, run_id).disposition == "FAILED"


@pytest.mark.parametrize(
    "stage",
    (
        "before_validate",
        "after_candidate_validation",
        "before_replace",
        "after_replace_before_verify",
    ),
)
def test_recovery_replacement_fault_is_retryable_without_evidence_rewrite(
    tmp_path: Path, stage: str
) -> None:
    config, _root = _configured(tmp_path)
    with pytest.raises(SnapshotAcquisitionRecoveryRequiredError):
        _acquire_snapshot(
            config,
            SnapshotAcquisitionRequest("managed", confirmed=True),
            fault_injector=_FailAt("snapshot.persist", "after_evidence_publish"),
        )
    run_id = _only_run_id(config)
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }

    with pytest.raises(SnapshotAcquisitionIntegrityError):
        _recover_snapshot_acquisition(
            config,
            run_id,
            confirmed=True,
            fault_injector=_FailAt("replacement", stage),
        )
    evidence_after_fault = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }
    assert set(evidence_before).issubset(evidence_after_fault)
    assert all(evidence_after_fault[path] == value for path, value in evidence_before.items())

    recovered = recover_snapshot_acquisition(config, run_id, confirmed=True)
    assert recovered.disposition == "COMPLETE"


def test_replaced_root_cannot_publish_success_and_recovery_does_not_rescan(
    tmp_path: Path,
) -> None:
    config, root = _configured(tmp_path)

    class _ReplaceRoot:
        def inject(self, operation: str, stage: str) -> None:
            if (operation, stage) == ("acquisition", "after_scope_observation"):
                old = root.parent / "observed-original"
                os.replace(root, old)
                root.mkdir()

    with pytest.raises(SnapshotAcquisitionRecoveryRequiredError):
        _acquire_snapshot(
            config,
            SnapshotAcquisitionRequest("managed", confirmed=True),
            fault_injector=_ReplaceRoot(),
        )
    run_id = _only_run_id(config)
    report = recover_snapshot_acquisition(config, run_id, confirmed=True)
    assert report.disposition == "FAILED" and report.snapshot_id is None


def test_recovery_closes_durable_snapshot_after_scope_disappears_without_rescan(
    tmp_path: Path,
) -> None:
    config, root = _configured(tmp_path)

    class _InterruptAfterSnapshot:
        def inject(self, operation: str, stage: str) -> None:
            if (operation, stage) == (
                "run.transition.scanned",
                "before_evidence_publish",
            ):
                os.replace(root, root.parent / "scope-no-longer-available")
                raise FaultInjectionError("durable-snapshot:scope-gone")

    with pytest.raises(SnapshotAcquisitionRecoveryRequiredError):
        _acquire_snapshot(
            config,
            SnapshotAcquisitionRequest("managed", confirmed=True),
            fault_injector=_InterruptAfterSnapshot(),
        )
    report = recover_snapshot_acquisition(config, _only_run_id(config), confirmed=True)
    assert report.disposition == "COMPLETE"
    assert report.verification is not None and report.verification.status == "VALID"


def test_unmarked_legacy_run_is_never_recovered(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)
    legacy = create_snapshot(config, ("managed",), ScanBudget(max_entries=100))
    assert snapshot_acquisition_status(config, legacy.run_id).disposition == "NOT_GOVERNED"
    with pytest.raises(SnapshotAcquisitionNotGovernedError):
        recover_snapshot_acquisition(config, legacy.run_id, confirmed=True)
    assert get_run(config, legacy.run_id).status == RunStatus.SCANNED
    assert not (config.paths.cache_dir / ".snapshot-acquisition.lock").exists()


def test_unmarked_scanning_plus_partial_snapshot_pattern_is_not_reconciled(
    tmp_path: Path,
) -> None:
    config, _root = _configured(tmp_path)
    run = create_run(config, "filesystem.snapshot")
    transition_run(config, run.run_id, RunStatus.SCANNING, "synthetic historical attempt")
    current = get_run(config, run.run_id)
    snapshot = _snapshot(config, current, config.scopes, ScanBudget(max_entries=1))
    assert snapshot.status.value == "partial"
    _persist(config, snapshot)
    _writer_handoff(config)

    assert snapshot_acquisition_status(config, run.run_id).disposition == "NOT_GOVERNED"
    with pytest.raises(SnapshotAcquisitionNotGovernedError):
        recover_snapshot_acquisition(config, run.run_id, confirmed=True)
    assert get_run(config, run.run_id).status == RunStatus.SCANNING


def test_terminal_verified_chain_is_shared_by_strict_and_classified_replay(
    tmp_path: Path,
) -> None:
    config, _root = _configured(tmp_path)
    completed = acquire_snapshot(config, SnapshotAcquisitionRequest("managed", confirmed=True))

    plan = classify_operational_evidence(config)
    assert plan.classification_complete and plan.eligible_snapshot_count == 1
    assert plan.snapshots[0].snapshot_id == completed.snapshot_id
    strict = replay_snapshot_index(config, config.paths.cache_dir / "strict-replay.db")
    classified = replay_classified_operational_index(
        config, config.paths.cache_dir / "classified-replay.db"
    )
    assert strict.replacement_ready and strict.replayed_snapshot_count == 1
    assert classified.candidate_ready and classified.actual_snapshot_count == 1


def test_lease_contention_fails_before_run_creation(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)
    with _AcquisitionLease(config):
        with pytest.raises(StorageBusyError):
            acquire_snapshot(config, SnapshotAcquisitionRequest("managed", confirmed=True))
    assert not (config.paths.evidence_dir / "runs").exists()


def test_lease_is_released_when_the_owning_process_exits(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)
    lock = config.paths.cache_dir / ".snapshot-acquisition.lock"
    script = (
        "import fcntl, os, sys; "
        "fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR, 0o600); "
        "fcntl.flock(fd, fcntl.LOCK_EX); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", script, str(lock)], check=True)

    report = acquire_snapshot(config, SnapshotAcquisitionRequest("managed", confirmed=True))
    assert report.disposition == "COMPLETE"


def test_cli_and_public_python_surface_preserve_the_same_safe_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, root = _configured(tmp_path)
    monkeypatch.setattr("local_steward.cli.load_config", lambda _path=None: config)
    runner = CliRunner()

    encoded = runner.invoke(
        app,
        [
            "--format",
            "json",
            "snapshots",
            "acquire",
            "--scope",
            "managed",
            "--max-entries",
            "100",
            "--yes",
        ],
    )

    assert encoded.exit_code == 0, encoded.output
    payload = json.loads(encoded.stdout)
    assert payload["schema_version"] == 1 and payload["command"] == "snapshots.acquire"
    assert payload["result"]["disposition"] == "COMPLETE"
    assert payload["result"]["run_status"] == "verified"
    assert payload["result"]["verification"]["status"] == "VALID"
    assert str(root) not in encoded.stdout
    run_id = payload["result"]["run_id"]

    human = runner.invoke(app, ["snapshots", "acquisition-status", run_id])
    assert human.exit_code == 0
    assert "Acquisition Disposition: COMPLETE" in human.stdout
    assert "Verification Status: VALID" in human.stdout


def test_cli_partial_and_pre_run_failures_have_frozen_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, _root = _configured(tmp_path)
    monkeypatch.setattr("local_steward.cli.load_config", lambda _path=None: config)
    runner = CliRunner()

    confirmation = runner.invoke(
        app,
        ["--format", "json", "snapshots", "acquire", "--scope", "managed"],
    )
    unknown = runner.invoke(
        app,
        [
            "--format",
            "json",
            "snapshots",
            "acquire",
            "--scope",
            "unknown",
            "--yes",
        ],
    )
    partial = runner.invoke(
        app,
        [
            "--format",
            "json",
            "snapshots",
            "acquire",
            "--scope",
            "managed",
            "--max-entries",
            "1",
            "--yes",
        ],
    )

    assert confirmation.exit_code == 2
    assert json.loads(confirmation.stdout)["errors"][0]["code"] == (
        "SNAPSHOT_ACQUISITION_CONFIRMATION_REQUIRED"
    )
    assert unknown.exit_code == 2
    assert json.loads(unknown.stdout)["errors"][0]["code"] == "SNAPSHOT_SCOPE_INVALID"
    assert partial.exit_code == 4
    assert json.loads(partial.stdout)["result"]["disposition"] == "PARTIAL"
