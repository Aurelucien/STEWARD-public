import json
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.database import database_path
from local_steward.snapshots import create_snapshot, get_snapshot

from .test_snapshot_queries import snapshot_fixture


def _review_fixture(tmp_path: Path):
    config, left = snapshot_fixture(tmp_path)
    root = config.scopes[0].normalized_path
    (root / "a.txt").unlink()
    (root / "ab.txt").write_text("changed", encoding="utf-8")
    (root / "created.txt").write_text("created", encoding="utf-8")
    right = create_snapshot(config, (), left.budget)
    return config, get_snapshot(config, left.snapshot_id), get_snapshot(config, right.snapshot_id)


def _command(config, left_id: str, right_id: str, *, encoded: bool = False) -> list[str]:
    command = ["--config", str(config.source_path)]
    if encoded:
        command.extend(("--format", "json"))
    return [*command, "snapshots", "diff", left_id, right_id]


def test_snapshot_diff_command_is_registered() -> None:
    result = CliRunner().invoke(app, ["snapshots", "diff", "--help"])
    assert result.exit_code == 0
    assert "Review deterministic changes" in result.stdout


def test_change_review_renders_mixed_changes_and_json(tmp_path: Path) -> None:
    config, left, right = _review_fixture(tmp_path)
    runner = CliRunner()

    human = runner.invoke(app, _command(config, left.snapshot_id, right.snapshot_id))
    encoded = runner.invoke(app, _command(config, left.snapshot_id, right.snapshot_id, encoded=True))

    assert human.exit_code == encoded.exit_code == 0
    assert "Left Snapshot ID: " + left.snapshot_id in human.stdout
    assert "Right Snapshot ID: " + right.snapshot_id in human.stdout
    assert "Added: 1" in human.stdout
    assert "Removed: 1" in human.stdout
    assert "Modified: 2" in human.stdout
    assert "FILE_CREATED scope=managed path=created.txt" in human.stdout
    assert "FILE_DELETED scope=managed path=a.txt" in human.stdout
    assert "FILE_MODIFIED scope=managed path=ab.txt" in human.stdout
    assert "hash_changed=null" in human.stdout
    assert "UNCHANGED" not in human.stdout
    assert encoded.stderr == "" and encoded.stdout.count("\n") == 1
    payload = json.loads(encoded.stdout)
    result = payload["result"]
    assert payload["command"] == "snapshots.diff" and payload["status"] == "OK"
    assert result["left_snapshot_id"] == left.snapshot_id
    assert result["right_snapshot_id"] == right.snapshot_id
    assert result["snapshot_diff"]["summary"] == {
        "added_count": 1,
        "removed_count": 1,
        "modified_count": 2,
        "unchanged_count": 2,
        "item_count": 6,
    }
    assert {event["event_type"] for event in result["change_events"]} == {
        "FILE_CREATED",
        "FILE_DELETED",
        "FILE_MODIFIED",
    }
    assert all(event["hash_changed"] is None for event in result["change_events"])
    assert {event["scope_id"] for event in result["change_events"]} == {"managed"}
    assert result["change_event_summary"] == {
        "created_count": 1,
        "deleted_count": 1,
        "modified_count": 2,
        "event_count": 4,
    }


def test_change_review_same_snapshot_is_allowed_and_reports_no_changes(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    runner = CliRunner()

    human = runner.invoke(app, _command(config, snapshot.snapshot_id, snapshot.snapshot_id))
    encoded = runner.invoke(
        app, _command(config, snapshot.snapshot_id, snapshot.snapshot_id, encoded=True)
    )

    assert human.exit_code == encoded.exit_code == 0
    assert "No filesystem changes." in human.stdout
    payload = json.loads(encoded.stdout)
    assert payload["result"]["change_events"] == []
    assert payload["result"]["snapshot_diff"]["summary"]["unchanged_count"] == snapshot.entry_count


@pytest.mark.parametrize("position", ("left", "right"))
def test_change_review_rejects_missing_snapshot(tmp_path: Path, position: str) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    missing = str(uuid4())
    left_id, right_id = (missing, snapshot.snapshot_id) if position == "left" else (snapshot.snapshot_id, missing)

    result = CliRunner().invoke(app, _command(config, left_id, right_id, encoded=True))

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "SNAPSHOT_NOT_FOUND"


def test_change_review_rejects_invalid_snapshot_id_format(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)

    result = CliRunner().invoke(app, _command(config, "not-a-uuid", snapshot.snapshot_id, encoded=True))

    assert result.exit_code == 2
    assert json.loads(result.stdout)["errors"][0]["code"] == "SNAPSHOT_NOT_FOUND"


def test_change_review_handles_repository_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, left, right = _review_fixture(tmp_path)

    def fail_read(*_args, **_kwargs):
        raise OSError("injected repository failure")

    monkeypatch.setattr("local_steward.snapshot_diff.get_snapshot", fail_read)

    result = CliRunner().invoke(app, _command(config, left.snapshot_id, right.snapshot_id, encoded=True))

    assert result.exit_code == 8
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "INTERNAL_ERROR"
    assert "Traceback" not in result.stdout and "injected repository failure" not in result.stdout


@pytest.mark.parametrize("position", ("left", "right"))
def test_change_review_rejects_invalid_snapshot(tmp_path: Path, position: str) -> None:
    config, left, right = _review_fixture(tmp_path)
    invalid = left if position == "left" else right
    evidence = config.paths.evidence_dir / str(invalid.evidence_relative_path)
    evidence.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(app, _command(config, left.snapshot_id, right.snapshot_id, encoded=True))

    assert result.exit_code == 2
    assert json.loads(result.stdout)["errors"][0]["code"] == "DIFF_INVALID"


def test_change_review_events_and_json_result_are_deterministic(tmp_path: Path) -> None:
    config, left, right = _review_fixture(tmp_path)
    runner = CliRunner()

    first = json.loads(runner.invoke(app, _command(config, left.snapshot_id, right.snapshot_id, encoded=True)).stdout)
    second = json.loads(runner.invoke(app, _command(config, left.snapshot_id, right.snapshot_id, encoded=True)).stdout)

    assert first["result"] == second["result"]
    assert [event["relative_path"] for event in first["result"]["change_events"]] == sorted(
        event["relative_path"] for event in first["result"]["change_events"]
    )


def test_change_review_does_not_scan_or_create_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, left, right = _review_fixture(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("snapshot scan or creation is forbidden")

    monkeypatch.setattr("local_steward.snapshots.scan", forbidden)
    monkeypatch.setattr("local_steward.cli.create_snapshot", forbidden)

    result = CliRunner().invoke(app, _command(config, left.snapshot_id, right.snapshot_id))

    assert result.exit_code == 0


def test_change_review_does_not_mutate_persistent_state(tmp_path: Path) -> None:
    config, left, right = _review_fixture(tmp_path)
    database_before = database_path(config).read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }

    result = CliRunner().invoke(app, _command(config, left.snapshot_id, right.snapshot_id, encoded=True))

    assert result.exit_code == 0
    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before
