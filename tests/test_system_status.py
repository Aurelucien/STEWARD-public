import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import local_steward.system_status as status_module
from local_steward.change_semantics import ChangeEventType
from local_steward.cli import app
from local_steward.database import database_path
from local_steward.evidence import canonical_json, digest
from local_steward.errors import DiffError, ResourceCollectionError
from local_steward.resources import observe_resources
from local_steward.snapshots import create_snapshot, get_snapshot
from local_steward.system_status import build_system_status_review

from .test_resource_observation import FakeProvider, _raw_sample
from .test_snapshot_queries import snapshot_fixture


def _observation(*, warning: bool = False):
    return observe_resources(
        provider=FakeProvider(_raw_sample(swap_unavailable=warning))
    )


def _review_fixture(tmp_path: Path):
    config, first = snapshot_fixture(tmp_path)
    root = config.scopes[0].normalized_path
    (root / "a.txt").write_text("changed", encoding="utf-8")
    (root / "created.txt").write_text("created", encoding="utf-8")
    second = create_snapshot(config, (), first.budget)
    return config, get_snapshot(config, first.snapshot_id), get_snapshot(config, second.snapshot_id)


def _command(config, *, encoded: bool = False) -> list[str]:
    result = ["--config", str(config.source_path)]
    if encoded:
        result.extend(("--format", "json"))
    return [*result, "status"]


def _invalidate_snapshot_evidence(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"]["snapshot"]["snapshot_digest"] = "0" * 64
    document["evidence_digest"] = digest(document)
    path.write_bytes(canonical_json(document))


def test_status_command_is_registered_with_resource_options() -> None:
    result = CliRunner().invoke(app, ["status", "--help"])

    assert result.exit_code == 0
    assert "--sample-seconds" in result.stdout
    assert "--top" in result.stdout
    assert "--sort" in result.stdout


def test_status_uses_resource_defaults_and_renders_all_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _first, _second = _review_fixture(tmp_path)
    calls: list[tuple[float, int, str]] = []

    def observe(sample_seconds: float, top: int, sort: str):
        calls.append((sample_seconds, top, sort))
        return _observation()

    monkeypatch.setattr(status_module, "observe_resources", observe)
    result = CliRunner().invoke(app, _command(config))

    assert result.exit_code == 0
    assert calls == [(1.0, 20, "cpu")]
    for heading in (
        "System Resources",
        "Evidence Health",
        "Storage Health",
        "Recent Filesystem Changes",
        "Known Limitations",
    ):
        assert heading in result.stdout
    assert "FILE_MODIFIED scope=managed path=a.txt" in result.stdout
    assert "FILE_CREATED scope=managed path=created.txt" in result.stdout
    assert "UNCHANGED" not in result.stdout


def test_status_passes_existing_resource_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _first, _second = _review_fixture(tmp_path)
    calls: list[tuple[float, int, str]] = []

    def observe(sample_seconds: float, top: int, sort: str):
        calls.append((sample_seconds, top, sort))
        return _observation()

    monkeypatch.setattr(status_module, "observe_resources", observe)
    result = CliRunner().invoke(
        app,
        [*_command(config), "--sample-seconds", "2.5", "--top", "3", "--sort", "memory"],
    )

    assert result.exit_code == 0
    assert calls == [(2.5, 3, "memory")]


def test_status_json_uses_envelope_and_preserves_null_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _first, _second = _review_fixture(tmp_path)
    monkeypatch.setattr(status_module, "observe_resources", lambda *_args: _observation(warning=True))

    result = CliRunner().invoke(app, _command(config, encoded=True))

    assert result.exit_code == 0 and result.stderr == "" and result.stdout.count("\n") == 1
    payload = json.loads(result.stdout)
    review = payload["result"]["review"]
    assert payload["command"] == "status" and payload["status"] == "OK"
    assert review["resources"]["memory"]["swap_total_bytes"] is None
    assert "SWAP_UNAVAILABLE" in payload["warnings"]
    assert review["recent_changes"]["snapshot_diff_summary"]["unchanged_count"] > 0
    assert {event["event_type"] for event in review["recent_changes"]["change_events"]} == {
        ChangeEventType.FILE_CREATED.value,
        ChangeEventType.FILE_MODIFIED.value,
    }
    assert all(event["hash_changed"] is None for event in review["recent_changes"]["change_events"])


def test_status_resource_collection_failure_uses_existing_error_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    monkeypatch.setattr(
        status_module,
        "observe_resources",
        lambda *_args: (_ for _ in ()).throw(ResourceCollectionError("unavailable")),
    )

    result = CliRunner().invoke(app, _command(config, encoded=True))

    assert result.exit_code == 3
    assert json.loads(result.stdout)["errors"][0]["code"] == "RESOURCE_OBSERVATION_FAILED"


def test_status_exposes_evidence_counts_and_healthy_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _first, _second = _review_fixture(tmp_path)
    monkeypatch.setattr(status_module, "observe_resources", lambda *_args: _observation())

    review = build_system_status_review(config)

    assert review.evidence_health.status == "VALID"
    assert review.evidence_health.snapshot_evidence.valid_count == 2
    assert review.evidence_health.snapshot_evidence.invalid_count == 0
    assert review.storage_health.storage_status == "HEALTHY"


def test_status_keeps_inconsistent_evidence_and_storage_as_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, first, _second = _review_fixture(tmp_path)
    _invalidate_snapshot_evidence(config.paths.evidence_dir / str(first.evidence_relative_path))
    monkeypatch.setattr(status_module, "observe_resources", lambda *_args: _observation())

    result = CliRunner().invoke(app, _command(config, encoded=True))

    assert result.exit_code == 0
    review = json.loads(result.stdout)["result"]["review"]
    assert review["evidence_health"]["snapshot_evidence"]["invalid_count"] == 1
    assert review["storage_health"]["storage_status"] == "INCONSISTENT"


def test_status_marks_recent_changes_unavailable_with_fewer_than_two_valid_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    monkeypatch.setattr(status_module, "observe_resources", lambda *_args: _observation())

    review = build_system_status_review(config)

    assert review.recent_changes.status == "UNAVAILABLE"
    assert review.recent_changes.limitation == "INSUFFICIENT_VALID_SNAPSHOTS"
    result = CliRunner().invoke(app, _command(config, encoded=True))
    assert result.exit_code == 0


def test_status_skips_invalid_snapshots_when_selecting_recent_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, first = snapshot_fixture(tmp_path)
    root = config.scopes[0].normalized_path
    (root / "invalid.txt").write_text("invalid", encoding="utf-8")
    invalid = create_snapshot(config, (), first.budget)
    (root / "valid.txt").write_text("valid", encoding="utf-8")
    newest = create_snapshot(config, (), first.budget)
    invalid = get_snapshot(config, invalid.snapshot_id)
    _invalidate_snapshot_evidence(config.paths.evidence_dir / str(invalid.evidence_relative_path))
    monkeypatch.setattr(status_module, "observe_resources", lambda *_args: _observation())

    review = build_system_status_review(config)

    assert review.recent_changes.status == "AVAILABLE"
    assert review.recent_changes.left_snapshot_id == first.snapshot_id
    assert review.recent_changes.right_snapshot_id == newest.snapshot_id


def test_status_records_diff_limitation_without_bypassing_diff_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _first, _second = _review_fixture(tmp_path)
    monkeypatch.setattr(status_module, "observe_resources", lambda *_args: _observation())
    monkeypatch.setattr(
        status_module,
        "compute_verified_snapshot_diff",
        lambda *_args: (_ for _ in ()).throw(DiffError("incompatible")),
    )

    review = build_system_status_review(config)

    assert review.recent_changes.status == "UNAVAILABLE"
    assert review.recent_changes.limitation == "RECENT_SNAPSHOT_DIFF_UNAVAILABLE"


def test_status_does_not_scan_or_mutate_persistent_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _first, _second = _review_fixture(tmp_path)
    database_before = database_path(config).read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }
    monkeypatch.setattr(status_module, "observe_resources", lambda *_args: _observation())
    monkeypatch.setattr(
        "local_steward.snapshots.scan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("scan is forbidden")),
    )

    result = CliRunner().invoke(app, _command(config, encoded=True))

    assert result.exit_code == 0
    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before


def test_status_result_is_stable_for_same_injected_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _first, _second = _review_fixture(tmp_path)
    monkeypatch.setattr(status_module, "observe_resources", lambda *_args: _observation())
    runner = CliRunner()

    first = json.loads(runner.invoke(app, _command(config, encoded=True)).stdout)
    second = json.loads(runner.invoke(app, _command(config, encoded=True)).stdout)

    assert first["result"] == second["result"]
    assert [event["relative_path"] for event in first["result"]["review"]["recent_changes"]["change_events"]] == sorted(
        event["relative_path"]
        for event in first["result"]["review"]["recent_changes"]["change_events"]
    )
