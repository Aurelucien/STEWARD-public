"""NEXT-002B isolated Snapshot refresh and bounded change-review acceptance."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.errors import (
    DiffError,
    SnapshotAcquisitionRecoveryRequiredError,
    SnapshotChangeReviewError,
    SnapshotChangeReviewResourceError,
    SnapshotRefreshBaseError,
)
from local_steward.faults import FaultInjectionError
from local_steward.models import ScanBudget, ScopeRole
from local_steward.output import to_jsonable
from local_steward.snapshot_acquisition import (
    SnapshotAcquisitionRequest,
    _acquire_snapshot,
    acquire_snapshot,
    snapshot_acquisition_status,
)
from local_steward.snapshot_refresh import (
    SnapshotChangeReviewRequest,
    SnapshotRefreshRequest,
    _refresh_snapshot,
    refresh_snapshot,
    review_snapshot_changes,
)
from local_steward.snapshots import create_snapshot, list_snapshots, verify_snapshot

from .test_protocol_completion import prepared_config


class _FailAt:
    def __init__(self, operation: str, stage: str) -> None:
        self.operation = operation
        self.stage = stage

    def inject(self, operation: str, stage: str) -> None:
        if (operation, stage) == (self.operation, self.stage):
            raise FaultInjectionError(f"{operation}:{stage}")


def _configured(tmp_path: Path):
    config = prepared_config(tmp_path)
    root = tmp_path / "observed"
    root.mkdir()
    (root / "alpha.txt").write_text("alpha", encoding="utf-8")
    child = root / "child"
    child.mkdir()
    (child / "beta.txt").write_text("beta", encoding="utf-8")
    return replace(config, scopes=(replace(config.scopes[0], normalized_path=root),)), root


def _base(config):
    report = acquire_snapshot(
        config,
        SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True),
    )
    assert report.snapshot_id is not None and report.disposition == "COMPLETE"
    return report


def _run_count(config) -> int:
    root = config.paths.evidence_dir / "runs"
    return len([path for path in root.iterdir() if path.is_dir()]) if root.exists() else 0


def _source_manifest(root: Path) -> tuple[tuple[str, bytes, int, int], ...]:
    return tuple(
        (
            path.relative_to(root).as_posix(),
            path.read_bytes() if path.is_file() else b"",
            path.lstat().st_mtime_ns,
            path.lstat().st_mode,
        )
        for path in sorted((root, *root.rglob("*")), key=lambda item: str(item))
    )


def test_complete_refresh_uses_existing_lifecycle_and_bounded_review(tmp_path: Path) -> None:
    config, root = _configured(tmp_path)
    base = _base(config)
    (root / "new.txt").write_text("new", encoding="utf-8")
    (root / "alpha.txt").write_text("alpha changed", encoding="utf-8")

    report = refresh_snapshot(
        config,
        SnapshotRefreshRequest(
            "managed",
            base.snapshot_id or "",
            ScanBudget(max_entries=100),
            True,
            change_limit=1,
        ),
    )

    assert report.disposition == "COMPLETE"
    assert report.acquisition.run_status == "verified"
    assert report.acquisition.verification is not None
    assert report.acquisition.verification.status == "VALID"
    assert report.review is not None
    assert report.review.returned_count == 1 and report.review.has_more
    assert report.review.full_event_count >= 2
    assert report.review.event_summary.created_count >= 1
    assert report.review.event_summary.modified_count >= 1
    assert all(item.hash_changed is None for item in report.review.items)
    assert verify_snapshot(config, report.acquisition.snapshot_id or "").status == "VALID"
    assert len(tuple((config.paths.evidence_dir / "runs" / report.acquisition.run_id).iterdir())) == 6
    encoded = json.dumps(to_jsonable(report), sort_keys=True)
    assert str(root) not in encoded
    assert "left_entry" not in encoded and "right_entry" not in encoded


def test_no_change_review_is_complete_with_empty_bounded_page(tmp_path: Path) -> None:
    config, root = _configured(tmp_path)
    base = _base(config)
    source_before = _source_manifest(root)

    report = refresh_snapshot(
        config,
        SnapshotRefreshRequest(
            "managed", base.snapshot_id or "", ScanBudget(max_entries=100), True
        ),
    )

    assert report.disposition == "COMPLETE" and report.review is not None
    assert report.review.event_summary.event_count == 0
    assert report.review.returned_count == 0 and not report.review.has_more
    assert report.review.diff_summary.unchanged_count == base.entry_count
    assert _source_manifest(root) == source_before


def test_change_review_pagination_is_deterministic_and_digest_is_page_invariant(
    tmp_path: Path,
) -> None:
    config, root = _configured(tmp_path)
    base = _base(config)
    (root / "one.txt").write_text("1", encoding="utf-8")
    (root / "two.txt").write_text("2", encoding="utf-8")
    target = acquire_snapshot(
        config,
        SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True),
    )
    assert target.snapshot_id is not None
    root.rename(tmp_path / "scope-unavailable-during-historical-review")

    first = review_snapshot_changes(
        config,
        SnapshotChangeReviewRequest(base.snapshot_id or "", target.snapshot_id, 1, 0),
    )
    second = review_snapshot_changes(
        config,
        SnapshotChangeReviewRequest(base.snapshot_id or "", target.snapshot_id, 1, 1),
    )

    assert first.review_digest == second.review_digest
    assert first.full_event_count == second.full_event_count >= 2
    assert first.items != second.items
    assert first.next_offset == 1
    assert [item.relative_path for item in first.items + second.items] == sorted(
        item.relative_path for item in first.items + second.items
    )


def test_partial_refresh_retains_authoritative_snapshot_without_review(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)
    base = _base(config)

    report = refresh_snapshot(
        config,
        SnapshotRefreshRequest(
            "managed", base.snapshot_id or "", ScanBudget(max_entries=1), True
        ),
    )

    assert report.disposition == "PARTIAL_NO_REVIEW"
    assert report.review is None and not report.review_errors
    assert report.acquisition.disposition == "PARTIAL"
    assert report.acquisition.run_status == "verified"
    assert snapshot_acquisition_status(config, report.acquisition.run_id).disposition == "PARTIAL"


def test_review_failure_after_acquisition_preserves_terminal_identity(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)
    base = _base(config)

    def fail_review(*_args):
        raise DiffError("injected derived review failure")

    report = _refresh_snapshot(
        config,
        SnapshotRefreshRequest(
            "managed", base.snapshot_id or "", ScanBudget(max_entries=100), True
        ),
        reviewer=fail_review,
    )

    assert report.disposition == "ACQUIRED_REVIEW_UNAVAILABLE"
    assert report.review is None
    assert report.review_errors[0]["code"] == "DIFF_INVALID"
    assert report.acquisition.snapshot_id is not None
    assert snapshot_acquisition_status(config, report.acquisition.run_id).disposition == "COMPLETE"


def test_legacy_base_and_changed_root_fail_before_new_run(tmp_path: Path) -> None:
    config, root = _configured(tmp_path)
    legacy = create_snapshot(config, ("managed",), ScanBudget(max_entries=100))
    before = _run_count(config)
    with pytest.raises(SnapshotRefreshBaseError):
        refresh_snapshot(
            config,
            SnapshotRefreshRequest(
                "managed", legacy.snapshot_id, ScanBudget(max_entries=100), True
            ),
        )
    assert _run_count(config) == before

    base = _base(config)
    before = _run_count(config)
    root.rename(tmp_path / "old-observed")
    root.mkdir()
    with pytest.raises(SnapshotRefreshBaseError):
        refresh_snapshot(
            config,
            SnapshotRefreshRequest(
                "managed", base.snapshot_id or "", ScanBudget(max_entries=100), True
            ),
        )
    assert _run_count(config) == before


def test_invalid_scope_base_page_and_resource_limits_fail_before_refresh(
    tmp_path: Path,
) -> None:
    config, _root = _configured(tmp_path)
    base = _base(config)
    before = _run_count(config)

    with pytest.raises(SnapshotRefreshBaseError):
        refresh_snapshot(
            config,
            SnapshotRefreshRequest(
                "different", base.snapshot_id or "", ScanBudget(max_entries=100), True
            ),
        )
    with pytest.raises(SnapshotChangeReviewError):
        refresh_snapshot(
            config,
            SnapshotRefreshRequest(
                "managed",
                base.snapshot_id or "",
                ScanBudget(max_entries=100),
                True,
                change_limit=0,
            ),
        )
    with pytest.raises(SnapshotChangeReviewResourceError):
        refresh_snapshot(
            config,
            SnapshotRefreshRequest(
                "managed",
                base.snapshot_id or "",
                ScanBudget(max_entries=100_001),
                True,
            ),
        )
    changed_role = replace(
        config,
        scopes=(replace(config.scopes[0], role=ScopeRole.REFERENCE_ROOT),),
    )
    with pytest.raises(SnapshotRefreshBaseError):
        refresh_snapshot(
            changed_role,
            SnapshotRefreshRequest(
                "managed", base.snapshot_id or "", ScanBudget(max_entries=100), True
            ),
        )
    assert _run_count(config) == before


def test_standalone_review_rejects_partial_and_legacy_snapshots(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)
    base = _base(config)
    partial = acquire_snapshot(
        config,
        SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=1), True),
    )
    legacy = create_snapshot(config, ("managed",), ScanBudget(max_entries=100))

    for target in (partial.snapshot_id or "", legacy.snapshot_id):
        with pytest.raises(SnapshotChangeReviewError):
            review_snapshot_changes(
                config,
                SnapshotChangeReviewRequest(base.snapshot_id or "", target),
            )


def test_nonterminal_supported_base_is_rejected_without_another_run(tmp_path: Path) -> None:
    config, _root = _configured(tmp_path)
    with pytest.raises(SnapshotAcquisitionRecoveryRequiredError):
        _acquire_snapshot(
            config,
            SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True),
            fault_injector=_FailAt("run.transition.scanned", "after_evidence_publish"),
        )
    interrupted = list_snapshots(config)[0]
    before = _run_count(config)

    with pytest.raises(SnapshotRefreshBaseError):
        refresh_snapshot(
            config,
            SnapshotRefreshRequest(
                "managed", interrupted.snapshot_id, ScanBudget(max_entries=100), True
            ),
        )

    assert _run_count(config) == before


def test_cli_and_python_refresh_surfaces_preserve_same_bounded_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, root = _configured(tmp_path)
    base = _base(config)
    (root / "new.txt").write_text("new", encoding="utf-8")
    monkeypatch.setattr("local_steward.cli.load_config", lambda _path=None: config)
    runner = CliRunner()

    encoded = runner.invoke(
        app,
        [
            "--format",
            "json",
            "snapshots",
            "refresh",
            "--scope",
            "managed",
            "--against",
            base.snapshot_id or "",
            "--max-entries",
            "100",
            "--change-limit",
            "1",
            "--yes",
        ],
    )

    assert encoded.exit_code == 0, encoded.output
    payload = json.loads(encoded.stdout)
    assert payload["command"] == "snapshots.refresh" and payload["status"] == "OK"
    assert payload["result"]["disposition"] == "COMPLETE"
    review = payload["result"]["review"]
    assert review["returned_count"] == 1 and review["full_event_count"] >= 1
    assert str(root) not in encoded.stdout

    target_id = payload["result"]["acquisition"]["snapshot_id"]
    human = runner.invoke(
        app,
        ["snapshots", "change-review", base.snapshot_id or "", target_id, "--limit", "1"],
    )
    assert human.exit_code == 0
    assert "Deleted Locations:" in human.stdout
    assert "Review Digest:" in human.stdout
