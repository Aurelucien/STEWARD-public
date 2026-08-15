"""Scoped Snapshot location identity checks."""

from dataclasses import replace
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import local_steward.system_status as status_module
from local_steward.change_semantics import change_events_from_snapshot_diff
from local_steward.cli import app
from local_steward.database import database_path
from local_steward.errors import DiffError
from local_steward.models import ChangeEventType, SnapshotDiffChangeType
from local_steward.resources import observe_resources
from local_steward.runs import get_run
from local_steward.scan_budget import make_budget
from local_steward.snapshot_diff import compute_snapshot_diff
from local_steward.snapshots import create_snapshot, get_snapshot, verify_snapshot
from local_steward.storage import storage_status, verify_evidence_report

from .test_protocol_completion import prepared_config
from .test_resource_observation import FakeProvider, _raw_sample


def _observation():
    return observe_resources(provider=FakeProvider(_raw_sample()))


def _scoped_fixture(tmp_path: Path):
    config = prepared_config(tmp_path)
    scope_a = tmp_path / "scope-a"
    scope_b = tmp_path / "scope-b"
    scope_a.mkdir()
    scope_b.mkdir()
    config = replace(
        config,
        scopes=(
            replace(config.scopes[0], scope_id="scope-a", normalized_path=scope_a),
            replace(config.scopes[0], scope_id="scope-b", normalized_path=scope_b),
        ),
    )
    for root in (scope_a, scope_b):
        (root / "same.txt").write_text("same", encoding="utf-8")
        (root / "remove.txt").write_text("remove", encoding="utf-8")
        (root / "é.txt").write_text("unicode", encoding="utf-8")
    first = create_snapshot(config, (), make_budget())
    (scope_a / "same.txt").write_text("changed only in scope a", encoding="utf-8")
    (scope_a / "added.txt").write_text("added", encoding="utf-8")
    (scope_b / "remove.txt").unlink()
    second = create_snapshot(config, (), make_budget())
    return config, get_snapshot(config, first.snapshot_id), get_snapshot(config, second.snapshot_id)


def _item(diff, scope_id: str, relative_path: str):
    return next(
        item
        for item in diff.items
        if item.scope_id == scope_id and item.relative_path == relative_path
    )


def _command(config, left_id: str, right_id: str, *, encoded: bool = False) -> list[str]:
    command = ["--config", str(config.source_path)]
    if encoded:
        command.extend(("--format", "json"))
    return [*command, "snapshots", "diff", left_id, right_id]


def test_v1_evidence_and_index_preserve_two_scoped_same_paths(tmp_path: Path) -> None:
    config, first, second = _scoped_fixture(tmp_path)

    for snapshot in (first, second):
        locations = {(entry.scope_id, entry.relative_path) for entry in snapshot.entries}
        assert ("scope-a", ".") in locations and ("scope-b", ".") in locations
        assert ("scope-a", "same.txt") in locations and ("scope-b", "same.txt") in locations
        assert verify_snapshot(config, snapshot.snapshot_id).status == "VALID"
    report = verify_evidence_report(config).snapshot_evidence
    assert report.valid_count == 2 and report.invalid_count == 0
    assert storage_status(config).storage_status == "HEALTHY"


def test_scoped_same_path_diff_and_event_identity_are_unambiguous(tmp_path: Path) -> None:
    _config, first, second = _scoped_fixture(tmp_path)

    diff = compute_snapshot_diff(first, second)
    events = change_events_from_snapshot_diff(diff)

    assert _item(diff, "scope-a", "same.txt").change_type == SnapshotDiffChangeType.MODIFIED
    assert _item(diff, "scope-b", "same.txt").change_type == SnapshotDiffChangeType.UNCHANGED
    assert _item(diff, "scope-a", "added.txt").change_type == SnapshotDiffChangeType.ADDED
    assert _item(diff, "scope-b", "remove.txt").change_type == SnapshotDiffChangeType.REMOVED
    event_locations = {(event.scope_id, event.relative_path, event.event_type) for event in events}
    assert ("scope-a", "same.txt", ChangeEventType.FILE_MODIFIED) in event_locations
    assert ("scope-a", "added.txt", ChangeEventType.FILE_CREATED) in event_locations
    assert ("scope-b", "remove.txt", ChangeEventType.FILE_DELETED) in event_locations
    assert all(event.hash_changed is None for event in events)


def test_cross_scope_relocation_is_removed_and_added_not_modified(tmp_path: Path) -> None:
    _config, first, _second = _scoped_fixture(tmp_path)
    moved = next(entry for entry in first.entries if entry.scope_id == "scope-a" and entry.relative_path == "same.txt")
    left = replace(first, snapshot_id="left", entries=(moved,))
    right = replace(first, snapshot_id="right", entries=(replace(moved, scope_id="scope-b"),))

    diff = compute_snapshot_diff(left, right)

    assert [(item.scope_id, item.relative_path, item.change_type) for item in diff.items] == [
        ("scope-a", "same.txt", SnapshotDiffChangeType.REMOVED),
        ("scope-b", "same.txt", SnapshotDiffChangeType.ADDED),
    ]
    assert diff.summary.modified_count == 0


def test_duplicate_requires_the_same_scoped_location(tmp_path: Path) -> None:
    _config, first, _second = _scoped_fixture(tmp_path)
    entry = first.entries[0]
    duplicate = replace(first, entries=(*first.entries, replace(entry, entry_id="duplicate")))

    with pytest.raises(DiffError, match="duplicate scoped location"):
        compute_snapshot_diff(duplicate, first)


def test_scoped_sorting_unicode_and_case_paths_are_stable(tmp_path: Path) -> None:
    _config, first, _second = _scoped_fixture(tmp_path)
    source = first.entries[0]
    entries = (
        replace(source, scope_id="scope-b", relative_path="case.txt", entry_id="b-case"),
        replace(source, scope_id="scope-a", relative_path="é.txt", entry_id="a-unicode"),
        replace(source, scope_id="scope-a", relative_path="Case.txt", entry_id="a-case"),
    )
    left = replace(first, snapshot_id="left", entries=tuple(reversed(entries)))
    right = replace(first, snapshot_id="right", entries=entries)

    first_diff = compute_snapshot_diff(left, right)
    second_diff = compute_snapshot_diff(right, left)

    assert [(item.scope_id, item.relative_path) for item in first_diff.items] == [
        ("scope-a", "Case.txt"),
        ("scope-a", "é.txt"),
        ("scope-b", "case.txt"),
    ]
    assert all(item.change_type == SnapshotDiffChangeType.UNCHANGED for item in first_diff.items)
    assert [(item.scope_id, item.relative_path) for item in second_diff.items] == [
        ("scope-a", "Case.txt"),
        ("scope-a", "é.txt"),
        ("scope-b", "case.txt"),
    ]


def test_cli_json_human_and_status_review_preserve_scope_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, first, second = _scoped_fixture(tmp_path)
    runner = CliRunner()

    human = runner.invoke(app, _command(config, first.snapshot_id, second.snapshot_id))
    encoded = runner.invoke(app, _command(config, first.snapshot_id, second.snapshot_id, encoded=True))
    monkeypatch.setattr(status_module, "observe_resources", lambda *_args: _observation())
    status = runner.invoke(app, ["--config", str(config.source_path), "--format", "json", "status"])

    assert human.exit_code == encoded.exit_code == status.exit_code == 0
    assert "FILE_MODIFIED scope=scope-a path=same.txt" in human.stdout
    assert "FILE_DELETED scope=scope-b path=remove.txt" in human.stdout
    events = json.loads(encoded.stdout)["result"]["change_events"]
    assert {event["scope_id"] for event in events} == {"scope-a", "scope-b"}
    recent = json.loads(status.stdout)["result"]["review"]["recent_changes"]
    assert recent["status"] == "AVAILABLE"
    assert {event["scope_id"] for event in recent["change_events"]} == {"scope-a", "scope-b"}


def test_scoped_review_is_read_only_and_leaves_no_scanning_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, first, second = _scoped_fixture(tmp_path)
    database_before = database_path(config).read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }
    monkeypatch.setattr(status_module, "observe_resources", lambda *_args: _observation())
    runner = CliRunner()

    assert runner.invoke(app, _command(config, first.snapshot_id, second.snapshot_id, encoded=True)).exit_code == 0
    assert runner.invoke(app, ["--config", str(config.source_path), "--format", "json", "status"]).exit_code == 0
    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before
    assert get_run(config, first.run_id).status.value != "scanning"
    assert get_run(config, second.run_id).status.value != "scanning"
