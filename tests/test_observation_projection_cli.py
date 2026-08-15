"""Read-only JSON CLI integration for Observation Projection."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.database import database_path
from local_steward.observation_projection import (
    PairTrackingRequest,
    ProjectionBudget,
    ProjectionPolicy,
    SnapshotDiagnosticRequest,
    build_pair_tracking_projection,
    build_snapshot_diagnostic_projection,
    machine_object,
)
from local_steward.payload_hashing import PayloadLocality, default_payload_hash_policy
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot

from .test_protocol_completion import prepared_config


def _policy(*, relation: bool = False) -> ProjectionPolicy:
    return ProjectionPolicy(
        0,
        "raw-path",
        ProjectionBudget(8, 8, 8, 4, 0, 2, 1, (("TRACKING_FACT", 8),), 100_000),
        relation_overlay=relation,
    )


def _policy_json(*, relation: bool = False) -> dict[str, object]:
    return {
        "policy_schema_version": 0,
        "ordering_reference": "raw-path",
        "budget": {
            "explicit_entry_total": 8,
            "hierarchy_node_total": 8,
            "tracking_item_total": 8,
            "relation_component_total": 4,
            "duplicate_alias_component_total": 0,
            "members_per_component": 2,
            "scope_minimum_guarantee": 1,
            "priority_quotas": [["TRACKING_FACT", 8]],
            "serialized_bytes_soft": 100_000,
        },
        "duplicate_overlay": False,
        "relation_overlay": relation,
    }


def _config_with_files(tmp_path: Path):
    config = prepared_config(tmp_path)
    observed = tmp_path / "observed"
    observed.mkdir()
    (observed / "stable.txt").write_text("stable", encoding="utf-8")
    (observed / "changed.txt").write_text("before", encoding="utf-8")
    return replace(config, scopes=(replace(config.scopes[0], normalized_path=observed),))


def _write_json(tmp_path: Path, name: str, value: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _diagnose_request(snapshot_id: str) -> dict[str, object]:
    return {
        "mode": "SNAPSHOT_DIAGNOSTIC",
        "primary_snapshot_id": snapshot_id,
        "scope": None,
        "path_prefix": None,
        "hierarchy_requested": True,
        "depth": None,
        "rank": None,
        "min_bytes": None,
        "duplicate_overlay": "NOT_REQUESTED",
        "relation_context_pair": None,
    }


def _track_request(
    base_id: str,
    target_id: str,
    *,
    growth: str = "REQUESTED_AND_PRESENT",
    relation: str = "NOT_REQUESTED",
) -> dict[str, object]:
    return {
        "mode": "PAIR_TRACKING",
        "base_snapshot_id": base_id,
        "target_snapshot_id": target_id,
        "scope": None,
        "path_prefix": None,
        "growth": growth,
        "diff": "REQUESTED_AND_PRESENT",
        "relation": relation,
    }


def _command(config, command: str, request: Path, policy: Path, *, pretty: bool = False) -> list[str]:
    values = [
        "--config", str(config.source_path), "projection", command,
        "--request-json", str(request), "--policy-json", str(policy),
    ]
    if pretty:
        values.append("--pretty")
    return values


def _projection_machine(projection) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {**machine_object(projection.facts), "projection_digest": projection.projection_digest}


def test_projection_commands_are_discoverable_and_describe_read_only_contract() -> None:
    runner = CliRunner()
    root = runner.invoke(app, ["--help"])
    diagnose = runner.invoke(app, ["projection", "diagnose", "--help"])
    track = runner.invoke(app, ["projection", "track", "--help"])
    assert root.exit_code == diagnose.exit_code == track.exit_code == 0
    assert "projection" in root.stdout
    assert "read-only" in diagnose.stdout.lower() and "live scan" in diagnose.stdout.lower()
    assert "read-only" in track.stdout.lower() and "file action" in track.stdout.lower()


def test_diagnose_cli_outputs_direct_service_machine_facts_and_pretty_is_display_only(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    snapshot = create_snapshot(config, (), make_budget())
    request_path = _write_json(tmp_path, "diagnose-request.json", _diagnose_request(snapshot.snapshot_id))
    policy_path = _write_json(tmp_path, "policy.json", _policy_json())
    database_before = database_path(config).read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }
    runner = CliRunner()
    compact = runner.invoke(app, _command(config, "diagnose", request_path, policy_path))
    pretty = runner.invoke(app, _command(config, "diagnose", request_path, policy_path, pretty=True))
    direct = build_snapshot_diagnostic_projection(
        config, SnapshotDiagnosticRequest(snapshot.snapshot_id), _policy()
    )
    assert compact.exit_code == pretty.exit_code == 0
    assert compact.stderr == pretty.stderr == ""
    assert compact.stdout.count("\n") == 1 and compact.stdout.startswith("{")
    assert json.loads(compact.stdout) == json.loads(pretty.stdout) == _projection_machine(direct)
    assert json.loads(compact.stdout)["projection_digest"] == direct.projection_digest
    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before


def test_track_cli_outputs_pair_tracking_service_facts_and_optional_growth_state(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    base = create_snapshot(config, (), make_budget())
    (config.scopes[0].normalized_path / "changed.txt").write_text("after", encoding="utf-8")
    target = create_snapshot(config, (), make_budget())
    policy_path = _write_json(tmp_path, "policy.json", _policy_json())
    request_path = _write_json(tmp_path, "track-request.json", _track_request(base.snapshot_id, target.snapshot_id))
    runner = CliRunner()
    result = runner.invoke(app, _command(config, "track", request_path, policy_path))
    direct = build_pair_tracking_projection(
        config, PairTrackingRequest(base.snapshot_id, target.snapshot_id), _policy()
    )
    assert result.exit_code == 0 and result.stderr == ""
    assert json.loads(result.stdout) == _projection_machine(direct)
    no_growth_path = _write_json(
        tmp_path, "track-no-growth.json",
        _track_request(base.snapshot_id, target.snapshot_id, growth="NOT_REQUESTED"),
    )
    no_growth = runner.invoke(app, _command(config, "track", no_growth_path, policy_path))
    assert no_growth.exit_code == 0
    assert json.loads(no_growth.stdout)["pair_tracking"]["growth_hierarchy"]["state"] == "NOT_REQUESTED"


def test_track_cli_supports_v1_v2_and_v2_v2_pairs(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    root = config.scopes[0].normalized_path
    v1 = create_snapshot(config, (), make_budget())
    (root / "changed.txt").write_text("v1-to-v2", encoding="utf-8")
    v2 = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    (root / "changed.txt").write_text("v2-to-v2", encoding="utf-8")
    v2_target = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    policy_path = _write_json(tmp_path, "policy.json", _policy_json())
    runner = CliRunner()
    for name, base, target in (("v1-v2", v1, v2), ("v2-v2", v2, v2_target)):
        request_path = _write_json(tmp_path, f"{name}.json", _track_request(base.snapshot_id, target.snapshot_id))
        result = runner.invoke(app, _command(config, "track", request_path, policy_path))
        assert result.exit_code == 0 and result.stderr == ""
        machine = json.loads(result.stdout)
        base_version = getattr(base, "snapshot_schema_version", 1)
        target_version = getattr(target, "snapshot_schema_version", 1)
        assert machine["source_identity"]["snapshot_pair"]["base"]["schema_version"] == base_version
        assert machine["source_identity"]["snapshot_pair"]["target"]["schema_version"] == target_version


def test_projection_cli_rejects_strict_json_and_reports_only_structured_stderr(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    snapshot = create_snapshot(config, (), make_budget())
    policy = _policy_json()
    request = _diagnose_request(snapshot.snapshot_id)
    request["unknown"] = True
    request_path = _write_json(tmp_path, "invalid-request.json", request)
    policy_path = _write_json(tmp_path, "policy.json", policy)
    result = CliRunner().invoke(app, _command(config, "diagnose", request_path, policy_path))
    assert result.exit_code == 2 and result.stdout == ""
    assert json.loads(result.stderr) == {
        "category": "CLI_REQUEST_VALIDATION",
        "code": "PROJECTION_JSON_INVALID",
        "context": {},
        "status": "error",
    }
    invalid_policy = _policy_json()
    invalid_policy["budget"] = {**invalid_policy["budget"], "tracking_item_total": 1.5}
    invalid_policy_path = _write_json(tmp_path, "invalid-policy.json", invalid_policy)
    float_result = CliRunner().invoke(app, _command(config, "diagnose", _write_json(tmp_path, "request.json", _diagnose_request(snapshot.snapshot_id)), invalid_policy_path))
    assert float_result.exit_code == 2 and float_result.stdout == ""
    assert json.loads(float_result.stderr)["code"] == "PROJECTION_JSON_INVALID"
    mismatched = _diagnose_request(snapshot.snapshot_id)
    mismatched["mode"] = "PAIR_TRACKING"
    mismatch = CliRunner().invoke(
        app,
        _command(
            config,
            "diagnose",
            _write_json(tmp_path, "mismatched.json", mismatched),
            policy_path,
        ),
    )
    assert mismatch.exit_code == 2 and mismatch.stdout == ""
    assert json.loads(mismatch.stderr)["code"] == "MODE_UNSUPPORTED"


def test_projection_cli_maps_source_validation_and_missing_input_to_stable_errors(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    policy_path = _write_json(tmp_path, "policy.json", _policy_json())
    missing_source = _write_json(tmp_path, "missing-source.json", _diagnose_request(str(uuid4())))
    runner = CliRunner()
    source = runner.invoke(app, _command(config, "diagnose", missing_source, policy_path))
    assert source.exit_code == 3 and source.stdout == ""
    source_error = json.loads(source.stderr)
    assert source_error["category"] == "SOURCE_VALIDATION"
    assert source_error["code"] == "SNAPSHOT_MISSING"
    missing_input = runner.invoke(
        app,
        _command(config, "diagnose", tmp_path / "does-not-exist.json", policy_path),
    )
    assert missing_input.exit_code == 2 and missing_input.stdout == ""
    assert json.loads(missing_input.stderr)["code"] == "PROJECTION_JSON_INVALID"


def test_projection_cli_does_not_scan_or_create_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_files(tmp_path)
    snapshot = create_snapshot(config, (), make_budget())
    request_path = _write_json(tmp_path, "request.json", _diagnose_request(snapshot.snapshot_id))
    policy_path = _write_json(tmp_path, "policy.json", _policy_json())

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("live scan or Snapshot creation is forbidden")

    monkeypatch.setattr("local_steward.snapshots.scan", forbidden)
    monkeypatch.setattr("local_steward.cli.create_snapshot", forbidden)
    result = CliRunner().invoke(app, _command(config, "diagnose", request_path, policy_path))
    assert result.exit_code == 0


def test_projection_cli_subprocess_entry_point_emits_one_json_document(tmp_path: Path) -> None:
    config = _config_with_files(tmp_path)
    snapshot = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    request_path = _write_json(tmp_path, "request.json", _diagnose_request(snapshot.snapshot_id))
    policy_path = _write_json(tmp_path, "policy.json", _policy_json())
    result = subprocess.run(
        [
            sys.executable, "-m", "local_steward.cli", "--config", str(config.source_path),
            "projection", "diagnose", "--request-json", str(request_path),
            "--policy-json", str(policy_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout)["mode"] == "SNAPSHOT_DIAGNOSTIC"
