"""Production-path checks for bounded direct Snapshot payload observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.errors import SnapshotBudgetError
from local_steward.models import FilesystemSnapshot, FilesystemSnapshotV2, PayloadObservationStatus
from local_steward.payload_hashing import (
    PayloadLocality,
    default_payload_hash_policy,
    observe_direct_payloads,
)
from local_steward.scan_budget import make_budget
from local_steward.snapshots import (
    create_snapshot,
    get_snapshot,
    list_snapshot_entries,
    verify_snapshot,
)
from local_steward.storage import rebuild_index, storage_status, verify_evidence

from .test_protocol_completion import prepared_config


def _configured_scope(tmp_path: Path):
    config = prepared_config(tmp_path)
    root = tmp_path / "observed"
    root.mkdir()
    (root / "alpha.txt").write_bytes(b"alpha\n")
    (root / "empty.txt").write_bytes(b"")
    (root / "large.txt").write_bytes(b"0123456789")
    (root / "directory").mkdir()
    (root / "link.txt").symlink_to(root / "alpha.txt")
    return replace(config, scopes=(replace(config.scopes[0], normalized_path=root),)), root


def _local(_: Path) -> PayloadLocality:
    return PayloadLocality.LOCAL


def test_default_policy_and_invalid_values_are_stable() -> None:
    policy = default_payload_hash_policy()
    assert policy.max_hash_file_bytes == 1_073_741_824
    assert policy.max_total_hash_bytes == 8_589_934_592
    assert policy.max_hash_duration_seconds == 300.0 and policy.hash_chunk_size == 1_048_576
    for kwargs in (
        {"max_hash_file_bytes": True},
        {"max_hash_file_bytes": 0},
        {"max_total_hash_bytes": -1},
        {"max_hash_duration_seconds": float("inf")},
        {"hash_chunk_size": 65_537},
    ):
        with pytest.raises(SnapshotBudgetError, match="PAYLOAD_HASH_POLICY_INVALID"):
            default_payload_hash_policy(**kwargs)  # type: ignore[arg-type]


def test_direct_reader_hashes_regular_files_without_following_symlinks(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    base = create_snapshot(config, (), make_budget())
    observed = observe_direct_payloads(
        base.entries, config.scopes, default_payload_hash_policy(), locality_provider=_local
    )
    by_path = {entry.relative_path: entry.payload_observation for entry in observed}
    assert by_path["alpha.txt"].digest == hashlib.sha256(b"alpha\n").hexdigest()
    assert by_path["alpha.txt"].bytes_hashed == 6
    assert by_path["empty.txt"].status == PayloadObservationStatus.EMPTY_FILE_HASHED
    assert by_path["empty.txt"].digest == hashlib.sha256(b"").hexdigest()
    assert by_path["link.txt"].status == PayloadObservationStatus.NOT_REGULAR_FILE
    assert by_path["directory"].status == PayloadObservationStatus.NOT_REGULAR_FILE


def test_v2_creation_persists_verifies_and_queries_payload_facts(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    snapshot = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(max_hash_file_bytes=100, max_total_hash_bytes=100),
        locality_provider=_local,
    )
    assert isinstance(snapshot, FilesystemSnapshotV2)
    assert verify_snapshot(config, snapshot.snapshot_id).status == "VALID"
    assert all(item.status == "VALID" for item in verify_evidence(config))
    loaded = get_snapshot(config, snapshot.snapshot_id)
    assert isinstance(loaded, FilesystemSnapshotV2)
    assert loaded.hash_policy.max_hash_file_bytes == 100
    page = list_snapshot_entries(config, snapshot.snapshot_id, limit=100)
    alpha = next(item for item in page.entries if item.relative_path == "alpha.txt")
    assert getattr(alpha, "payload_observation").digest == hashlib.sha256(b"alpha\n").hexdigest()
    rebuild_index(config)
    assert isinstance(get_snapshot(config, snapshot.snapshot_id), FilesystemSnapshotV2)
    assert storage_status(config).storage_status == "HEALTHY"


def test_default_create_stays_v1_and_unknown_locality_never_opens_payload(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    default = create_snapshot(config, (), make_budget())
    unknown = create_snapshot(config, (), make_budget(), default_payload_hash_policy())
    assert isinstance(default, FilesystemSnapshot)
    assert not isinstance(default, FilesystemSnapshotV2)
    assert isinstance(unknown, FilesystemSnapshotV2)
    statuses = {entry.payload_observation.status for entry in unknown.entries}
    assert PayloadObservationStatus.UNSUPPORTED in statuses
    assert all(
        entry.payload_observation.digest is None
        for entry in unknown.entries
        if entry.relative_path == "alpha.txt"
    )


def test_budget_skips_large_then_hashes_later_small_file(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    snapshot = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(max_hash_file_bytes=8, max_total_hash_bytes=8),
        locality_provider=_local,
    )
    assert isinstance(snapshot, FilesystemSnapshotV2)
    by_path = {entry.relative_path: entry.payload_observation.status for entry in snapshot.entries}
    assert by_path["large.txt"] == PayloadObservationStatus.FILE_TOO_LARGE
    assert by_path["alpha.txt"] == PayloadObservationStatus.HASHED


def test_verified_reuse_uses_evidence_source_without_payload_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _root = _configured_scope(tmp_path)
    source = create_snapshot(
        config, (), make_budget(), default_payload_hash_policy(), locality_provider=_local
    )
    assert isinstance(source, FilesystemSnapshotV2)

    def forbidden_read(*_args: object) -> bytes:
        raise AssertionError("verified reuse must not read current payload bytes")

    monkeypatch.setattr("local_steward.payload_hashing.os.read", forbidden_read)
    reused = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(allow_verified_reuse=True),
        locality_provider=_local,
    )
    assert isinstance(reused, FilesystemSnapshotV2)
    alpha = next(item for item in reused.entries if item.relative_path == "alpha.txt")
    assert alpha.payload_observation.provenance is not None
    assert alpha.payload_observation.provenance.value == "REUSED_FROM_VERIFIED_SNAPSHOT"
    assert alpha.payload_observation.reused_from_snapshot_id == source.snapshot_id
    assert alpha.payload_observation.digest == hashlib.sha256(b"alpha\n").hexdigest()
    assert verify_snapshot(config, reused.snapshot_id).status == "VALID"
    assert all(item.status == "VALID" for item in verify_evidence(config))
    rebuild_index(config)
    assert verify_snapshot(config, reused.snapshot_id).status == "VALID"


def test_reuse_does_not_form_a_chain(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    direct = create_snapshot(
        config, (), make_budget(), default_payload_hash_policy(), locality_provider=_local
    )
    first_reuse = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(allow_verified_reuse=True),
        locality_provider=_local,
    )
    second_reuse = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(allow_verified_reuse=True),
        locality_provider=_local,
    )
    assert isinstance(direct, FilesystemSnapshotV2)
    assert isinstance(first_reuse, FilesystemSnapshotV2)
    assert isinstance(second_reuse, FilesystemSnapshotV2)
    alpha = next(item for item in second_reuse.entries if item.relative_path == "alpha.txt")
    assert alpha.payload_observation.reused_from_snapshot_id == direct.snapshot_id


def test_reused_snapshot_becomes_invalid_if_source_evidence_disappears(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    source = create_snapshot(
        config, (), make_budget(), default_payload_hash_policy(), locality_provider=_local
    )
    reused = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(allow_verified_reuse=True),
        locality_provider=_local,
    )
    assert isinstance(source, FilesystemSnapshotV2)
    assert isinstance(reused, FilesystemSnapshotV2)
    source_path = config.paths.evidence_dir / str(
        get_snapshot(config, source.snapshot_id).evidence_relative_path
    )
    source_path.unlink()
    result = verify_snapshot(config, reused.snapshot_id)
    assert result.status == "INVALID"
    assert {issue["code"] for issue in result.errors} >= {"PAYLOAD_REUSE_SOURCE_MISSING"}


def test_reuse_falls_back_to_direct_read_when_metadata_changes(tmp_path: Path) -> None:
    config, root = _configured_scope(tmp_path)
    create_snapshot(config, (), make_budget(), default_payload_hash_policy(), locality_provider=_local)
    (root / "alpha.txt").write_bytes(b"changed\n")
    current = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(allow_verified_reuse=True),
        locality_provider=_local,
    )
    assert isinstance(current, FilesystemSnapshotV2)
    alpha = next(item for item in current.entries if item.relative_path == "alpha.txt")
    assert alpha.payload_observation.provenance is not None
    assert alpha.payload_observation.provenance.value == "DIRECT_READ"
    assert alpha.payload_observation.digest == hashlib.sha256(b"changed\n").hexdigest()


def test_cli_hash_options_require_explicit_enablement_without_a_run(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    before = sorted(config.paths.evidence_dir.rglob("*.json"))
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config.source_path),
            "--format",
            "json",
            "snapshots",
            "create",
            "--max-hash-file-bytes",
            "100",
        ],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "SNAPSHOT_BUDGET_INVALID"
    assert sorted(config.paths.evidence_dir.rglob("*.json")) == before


def test_cli_reuse_requires_payload_hash_without_a_run(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    before = sorted(config.paths.evidence_dir.rglob("*.json"))
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config.source_path),
            "--format",
            "json",
            "snapshots",
            "create",
            "--reuse-verified-payloads",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["errors"][0]["code"] == "SNAPSHOT_BUDGET_INVALID"
    assert sorted(config.paths.evidence_dir.rglob("*.json")) == before


def test_programmatic_invalid_policy_is_rejected_before_run_creation(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    before = sorted(config.paths.evidence_dir.rglob("*.json"))
    invalid = replace(default_payload_hash_policy(), max_total_hash_bytes=0)
    with pytest.raises(SnapshotBudgetError, match="PAYLOAD_HASH_POLICY_INVALID"):
        create_snapshot(config, (), make_budget(), invalid)
    assert sorted(config.paths.evidence_dir.rglob("*.json")) == before


def test_non_local_and_unknown_never_open_a_descriptor(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    base = create_snapshot(config, (), make_budget())

    def forbidden_open(_path: Path, _flags: int) -> int:
        raise AssertionError("payload descriptor must not be opened")

    for locality, expected in (
        (PayloadLocality.NON_LOCAL, PayloadObservationStatus.NOT_LOCAL),
        (PayloadLocality.UNKNOWN, PayloadObservationStatus.UNSUPPORTED),
    ):
        observations = observe_direct_payloads(
            base.entries,
            config.scopes,
            default_payload_hash_policy(),
            locality_provider=lambda _path, value=locality: value,
            opener=forbidden_open,
        )
        alpha = next(item for item in observations if item.relative_path == "alpha.txt")
        assert alpha.payload_observation.status == expected


def test_cli_payload_hash_selects_v2(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    result = CliRunner().invoke(
        app,
        ["--config", str(config.source_path), "--format", "json", "snapshots", "create", "--payload-hash"],
    )
    assert result.exit_code == 4, result.stdout + result.stderr
    snapshot_id = json.loads(result.stdout)["result"]["snapshot"]["snapshot_id"]
    assert isinstance(get_snapshot(config, snapshot_id), FilesystemSnapshotV2)
