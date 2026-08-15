"""Isolated end-to-end and recovery non-regression acceptance for duplicates."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.database import database_path
from local_steward.duplicate_analysis import (
    canonical_duplicate_analysis,
    compute_verified_snapshot_duplicate_analysis,
)
from local_steward.errors import DuplicateAnalysisError
from local_steward.models import (
    FilesystemSnapshotV2,
    PayloadObservationProvenance,
    SnapshotBackupStatus,
    SnapshotReplacementStatus,
    SnapshotRollbackStatus,
    SnapshotReplayStatus,
)
from local_steward.payload_hashing import PayloadLocality, default_payload_hash_policy
from local_steward.scan_budget import make_budget
from local_steward.snapshot_backup import create_snapshot_index_backup
from local_steward.snapshot_duplicate_query import query_verified_snapshot_duplicates
from local_steward.snapshot_replacement import replace_snapshot_index
from local_steward.snapshot_replay import replay_snapshot_index
from local_steward.snapshot_rollback import restore_snapshot_index_from_backup
from local_steward.snapshots import create_snapshot, get_snapshot, verify_snapshot
from local_steward.storage import rebuild_index, storage_status

from .test_protocol_completion import prepared_config


def _local(_: Path) -> PayloadLocality:
    return PayloadLocality.LOCAL


def _tree(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }


def _fixture(tmp_path: Path):
    config = prepared_config(tmp_path)
    root = tmp_path / "observed"
    root.mkdir()
    (root / "exact-a.txt").write_bytes(b"same")
    (root / "exact-b.txt").write_bytes(b"same")
    (root / "reused.txt").write_bytes(b"same")
    (root / "alias-a.txt").write_bytes(b"link")
    (root / "alias-b.txt").hardlink_to(root / "alias-a.txt")
    (root / "empty-a.txt").write_bytes(b"")
    (root / "empty-b.txt").write_bytes(b"")
    (root / "too-large.txt").write_bytes(b"large")
    (root / "directory").mkdir()
    (root / "link").symlink_to("exact-a.txt")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=root),))
    v1 = create_snapshot(config, (), make_budget())
    policy = default_payload_hash_policy(
        max_hash_file_bytes=4,
        max_total_hash_bytes=100,
        max_hash_duration_seconds=30,
    )
    source = create_snapshot(config, (), make_budget(), policy, locality_provider=_local)
    assert isinstance(source, FilesystemSnapshotV2)
    (root / "direct-peer.txt").write_bytes(b"same")
    target = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(
            max_hash_file_bytes=4,
            max_total_hash_bytes=100,
            max_hash_duration_seconds=30,
            allow_verified_reuse=True,
        ),
        locality_provider=_local,
    )
    assert isinstance(target, FilesystemSnapshotV2)
    assert any(
        entry.payload_observation.provenance
        == PayloadObservationProvenance.REUSED_FROM_VERIFIED_SNAPSHOT
        for entry in target.entries
    )
    assert any(
        entry.relative_path == "direct-peer.txt"
        and entry.payload_observation.provenance == PayloadObservationProvenance.DIRECT_READ
        for entry in target.entries
    )
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    empty_config = replace(config, scopes=(replace(config.scopes[0], normalized_path=empty_root),))
    empty = create_snapshot(empty_config, (), make_budget(), policy, locality_provider=_local)
    assert isinstance(empty, FilesystemSnapshotV2)
    return config, empty_config, v1, source, target, empty


def _query_identity(config, snapshot_id: str) -> tuple[bytes, str, tuple[str, ...], tuple[str, ...]]:
    analysis = compute_verified_snapshot_duplicate_analysis(config, snapshot_id)
    return (
        canonical_duplicate_analysis(analysis),
        analysis.analysis_digest,
        tuple(group.payload_group_id for group in analysis.payload_equality_groups),
        tuple(alias.alias_set_id for alias in analysis.hard_link_alias_sets),
    )


def test_isolated_duplicate_cli_pagination_and_frozen_boundaries(tmp_path: Path) -> None:
    config, empty_config, v1, source, target, empty = _fixture(tmp_path)
    assert all(
        verify_snapshot(config, snapshot.snapshot_id).status == "VALID"
        for snapshot in (v1, source, target)
    )
    assert verify_snapshot(empty_config, empty.snapshot_id).status == "VALID"
    target_query = query_verified_snapshot_duplicates(config, target.snapshot_id, limit=1_000)
    assert target_query.payload_equality_group_count == 3
    assert target_query.filtered_payload_equality_group_count == 3
    assert len(target_query.hard_link_alias_sets) == 1
    assert target_query.coverage.payload_unknown_regular_entry_count == 1
    assert target_query.coverage.payload_unknown_reason_counts[0].code == "FILE_TOO_LARGE"
    assert target_query.physical_storage.allocation_status.value == "UNKNOWN"
    assert target_query.physical_storage.physical_block_sharing_status.value == "UNKNOWN"
    assert target_query.physical_storage.reclaimable_bytes is None
    assert target_query.physical_storage.reclaimable_status.value == "UNKNOWN"
    alias_group = next(
        item
        for item in target_query.payload_equality_groups
        if item.alias_set_ids
    )
    assert not alias_group.is_exact_duplicate and alias_group.known_storage_unit_count == 1
    direct_reused = next(
        item
        for item in target_query.payload_equality_groups
        if any(reference.relative_path == "direct-peer.txt" for reference in item.member_entries)
    )
    assert direct_reused.is_exact_duplicate and direct_reused.known_storage_unit_count >= 2

    runner = CliRunner()
    command = ["--config", str(config.source_path), "snapshots", "duplicates", target.snapshot_id]
    human = runner.invoke(app, command)
    encoded = runner.invoke(app, ["--format", "json", *command])
    assert human.exit_code == encoded.exit_code == 0
    assert "not multiple storage copies" in human.stdout
    assert "Reclaimable Space: UNKNOWN" in human.stdout
    assert "safe to delete" not in human.stdout.lower()
    complete = json.loads(encoded.stdout)["result"]["duplicate_query"]
    assert complete["analysis_digest"] == target_query.analysis_digest
    assert complete["physical_storage"]["reclaimable_bytes"] is None
    assert complete["physical_storage"]["reclaimable_status"] == "UNKNOWN"

    group_ids: list[str] = []
    for offset in range(target_query.payload_equality_group_count):
        page = runner.invoke(app, ["--format", "json", *command, "--limit", "1", "--offset", str(offset)])
        assert page.exit_code == 0
        result = json.loads(page.stdout)["result"]["duplicate_query"]
        assert result["analysis_digest"] == target_query.analysis_digest
        assert result["coverage"] == complete["coverage"]
        assert result["hard_link_alias_sets"] == complete["hard_link_alias_sets"]
        assert result["integrity_conflicts"] == complete["integrity_conflicts"]
        group_ids.extend(item["payload_group_id"] for item in result["payload_equality_groups"])
    assert group_ids == [item.payload_group_id for item in target_query.payload_equality_groups]
    empty_page = runner.invoke(
        app,
        ["--format", "json", *command, "--limit", "1", "--offset", str(len(group_ids))],
    )
    assert empty_page.exit_code == 0
    assert json.loads(empty_page.stdout)["result"]["duplicate_query"]["payload_equality_groups"] == []

    exact = runner.invoke(app, ["--format", "json", *command, "--only-exact"])
    exact_result = json.loads(exact.stdout)["result"]["duplicate_query"]
    assert exact.exit_code == 0
    assert exact_result["analysis_digest"] == target_query.analysis_digest
    assert exact_result["coverage"] == complete["coverage"]
    assert all(item["is_exact_duplicate"] for item in exact_result["payload_equality_groups"])
    assert len(exact_result["payload_equality_groups"]) == 2

    assert query_verified_snapshot_duplicates(config, v1.snapshot_id).payload_equality_groups == ()
    assert query_verified_snapshot_duplicates(empty_config, empty.snapshot_id).payload_equality_groups == ()


def test_duplicate_analysis_recovery_chain_is_non_persistent_and_stable(tmp_path: Path) -> None:
    config, _empty_config, _v1, _source, target, _empty = _fixture(tmp_path)
    baseline = _query_identity(config, target.snapshot_id)
    evidence_before = _tree(config.paths.evidence_dir)
    official = database_path(config)

    rebuild_index(config)
    assert _query_identity(config, target.snapshot_id) == baseline
    backup = create_snapshot_index_backup(official, config.paths.cache_dir / "duplicate-acceptance.sqlite3")
    assert backup.status == SnapshotBackupStatus.READY and backup.manifest is not None
    manifest = Path(f"{backup.backup_database}.manifest.json").read_text(encoding="utf-8")
    assert "duplicate" not in manifest
    assert _query_identity(config, target.snapshot_id) == baseline

    candidate = config.paths.cache_dir / "duplicate-candidate.sqlite3"
    replay = replay_snapshot_index(config, candidate)
    assert replay.status == SnapshotReplayStatus.READY and replay.replacement_ready
    replacement = replace_snapshot_index(config, replay)
    assert replacement.status == SnapshotReplacementStatus.REPLACED
    assert _query_identity(config, target.snapshot_id) == baseline

    rollback = restore_snapshot_index_from_backup(config, backup, official)
    assert rollback.status == SnapshotRollbackStatus.RESTORED
    assert _query_identity(config, target.snapshot_id) == baseline
    assert storage_status(config).storage_status == "HEALTHY"
    assert _tree(config.paths.evidence_dir) == evidence_before
    connection = sqlite3.connect(official)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()
    assert not {name for name in tables if "duplicate" in name}


def test_source_invalid_reused_snapshot_is_rejected_without_payload_fallback(tmp_path: Path) -> None:
    config, _empty_config, _v1, source, target, _empty = _fixture(tmp_path)
    assert query_verified_snapshot_duplicates(config, target.snapshot_id).payload_equality_groups
    stored_source = get_snapshot(config, source.snapshot_id)
    stored_target = get_snapshot(config, target.snapshot_id)
    source_evidence = config.paths.evidence_dir / str(stored_source.evidence_relative_path)
    target_evidence = config.paths.evidence_dir / str(stored_target.evidence_relative_path)
    target_before = target_evidence.read_bytes()
    source_evidence.unlink()
    assert verify_snapshot(config, target.snapshot_id).status == "INVALID"
    with pytest.raises(DuplicateAnalysisError, match="DUPLICATE_INVALID"):
        query_verified_snapshot_duplicates(config, target.snapshot_id)
    assert target_evidence.read_bytes() == target_before
