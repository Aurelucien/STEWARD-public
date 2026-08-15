"""Isolated end-to-end and recovery checks for storage structure and growth."""

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.database import database_path
from local_steward.errors import GrowthError, StructureError
from local_steward.models import (
    DuplicateStorageKnowledgeStatus,
    GrowthRank,
    SnapshotBackupStatus,
    SnapshotReplacementStatus,
    SnapshotReplayStatus,
    SnapshotRollbackStatus,
    StructureRank,
)
from local_steward.snapshot_backup import create_snapshot_index_backup
from local_steward.snapshot_replacement import replace_snapshot_index
from local_steward.snapshot_replay import replay_snapshot_index
from local_steward.snapshot_rollback import restore_snapshot_index_from_backup
from local_steward.snapshots import get_snapshot, verify_snapshot
from local_steward.storage import rebuild_index, storage_status
from local_steward.storage_growth import canonical_storage_growth, compute_verified_snapshot_growth
from local_steward.storage_query import (
    query_verified_snapshot_growth,
    query_verified_snapshot_structure,
)
from local_steward.storage_structure import canonical_storage_structure, compute_verified_snapshot_structure

from .test_snapshot_duplicate_acceptance import _fixture, _tree


def _structure_identity(config, snapshot_id: str) -> tuple[bytes, str, tuple[str, ...], tuple[str, ...]]:
    result = compute_verified_snapshot_structure(config, snapshot_id)
    query = query_verified_snapshot_structure(config, snapshot_id, limit=1_000)
    return (
        canonical_storage_structure(result),
        result.structure_digest,
        tuple(node.path_node_id for node in result.path_nodes),
        tuple(node.path_node_id for node in query.path_nodes),
    )


def _growth_identity(config, base_snapshot_id: str, target_snapshot_id: str) -> tuple[bytes, str, tuple[str, ...], tuple[str, ...]]:
    result = compute_verified_snapshot_growth(config, base_snapshot_id, target_snapshot_id)
    query = query_verified_snapshot_growth(config, base_snapshot_id, target_snapshot_id, limit=1_000)
    return (
        canonical_storage_growth(result),
        result.growth_digest,
        tuple(node.growth_node_id for node in result.path_nodes),
        tuple(node.growth_node_id for node in query.path_nodes),
    )


def test_isolated_structure_growth_views_cli_and_no_side_effects(tmp_path: Path) -> None:
    config, empty_config, v1, source, target, empty = _fixture(tmp_path)
    assert all(
        verify_snapshot(config, snapshot.snapshot_id).status == "VALID" for snapshot in (v1, source, target)
    )
    assert verify_snapshot(empty_config, empty.snapshot_id).status == "VALID"
    database_before = database_path(config).read_bytes()
    evidence_before = _tree(config.paths.evidence_dir)

    structure = query_verified_snapshot_structure(config, target.snapshot_id, limit=1_000)
    growth = query_verified_snapshot_growth(config, source.snapshot_id, target.snapshot_id, limit=1_000)
    ranked_structure = query_verified_snapshot_structure(
        config,
        target.snapshot_id,
        rank=StructureRank.RECURSIVE_LOGICAL_BYTES,
        min_bytes=0,
        limit=1,
    )
    ranked_growth = query_verified_snapshot_growth(
        config,
        source.snapshot_id,
        target.snapshot_id,
        rank=GrowthRank.ADDED,
        min_bytes=0,
        limit=1,
    )
    assert structure.structure_digest == compute_verified_snapshot_structure(config, target.snapshot_id).structure_digest
    assert growth.growth_digest == compute_verified_snapshot_growth(config, source.snapshot_id, target.snapshot_id).growth_digest
    assert structure.coverage == query_verified_snapshot_structure(config, target.snapshot_id, depth=0).coverage
    assert growth.coverage == query_verified_snapshot_growth(config, source.snapshot_id, target.snapshot_id, depth=0).coverage
    assert ranked_structure.physical_boundary.allocation_status == DuplicateStorageKnowledgeStatus.UNKNOWN
    assert ranked_growth.physical_boundary.physical_block_sharing_status == DuplicateStorageKnowledgeStatus.UNKNOWN
    assert ranked_growth.physical_boundary.reclaimable_bytes is None
    assert query_verified_snapshot_structure(empty_config, empty.snapshot_id).path_nodes

    runner = CliRunner()
    structure_command = ["--config", str(config.source_path), "snapshots", "structure", target.snapshot_id]
    growth_command = [
        "--config",
        str(config.source_path),
        "snapshots",
        "growth",
        source.snapshot_id,
        target.snapshot_id,
    ]
    structure_human = runner.invoke(app, structure_command)
    structure_json = runner.invoke(app, ["--format", "json", *structure_command])
    growth_human = runner.invoke(app, growth_command)
    growth_json = runner.invoke(app, ["--format", "json", *growth_command])
    assert all(item.exit_code == 0 for item in (structure_human, structure_json, growth_human, growth_json))
    assert "Object-Aware Capacity: UNKNOWN" in structure_human.stdout
    assert "Physical Disk Growth: UNKNOWN" in growth_human.stdout
    structure_payload = json.loads(structure_json.stdout)["result"]["structure_query"]
    growth_payload = json.loads(growth_json.stdout)["result"]["growth_query"]
    assert structure_payload["structure_digest"] == structure.structure_digest
    assert growth_payload["growth_digest"] == growth.growth_digest
    assert growth_payload["coverage"] == json.loads(
        runner.invoke(app, ["--format", "json", *growth_command, "--depth", "0"]).stdout
    )["result"]["growth_query"]["coverage"]

    with pytest.raises(StructureError, match="STRUCTURE_INVALID"):
        query_verified_snapshot_structure(config, target.snapshot_id, scope="unknown")
    with pytest.raises(GrowthError, match="GROWTH_INVALID"):
        query_verified_snapshot_growth(config, source.snapshot_id, target.snapshot_id, path_prefix="missing")
    legal_empty = query_verified_snapshot_structure(
        config,
        target.snapshot_id,
        rank=StructureRank.RECURSIVE_LOGICAL_BYTES,
        min_bytes=10**12,
    )
    assert legal_empty.path_nodes == () and legal_empty.structure_digest == structure.structure_digest
    assert database_path(config).read_bytes() == database_before
    assert _tree(config.paths.evidence_dir) == evidence_before


def test_structure_growth_pagination_and_recovery_chain_are_stable(tmp_path: Path) -> None:
    config, _empty_config, _v1, source, target, _empty = _fixture(tmp_path)
    structure_baseline = _structure_identity(config, target.snapshot_id)
    growth_baseline = _growth_identity(config, source.snapshot_id, target.snapshot_id)
    structure_full = query_verified_snapshot_structure(config, target.snapshot_id, limit=1_000)
    growth_full = query_verified_snapshot_growth(config, source.snapshot_id, target.snapshot_id, limit=1_000)
    structure_pages = tuple(
        item.path_nodes[0].path_node_id
        for offset in range(structure_full.selected_path_node_count)
        for item in (query_verified_snapshot_structure(config, target.snapshot_id, limit=1, offset=offset),)
        if item.path_nodes
    )
    growth_pages = tuple(
        item.path_nodes[0].growth_node_id
        for offset in range(growth_full.selected_path_node_count)
        for item in (query_verified_snapshot_growth(config, source.snapshot_id, target.snapshot_id, limit=1, offset=offset),)
        if item.path_nodes
    )
    assert structure_pages == tuple(node.path_node_id for node in structure_full.path_nodes)
    assert growth_pages == tuple(node.growth_node_id for node in growth_full.path_nodes)

    evidence_before = _tree(config.paths.evidence_dir)
    official = database_path(config)
    rebuild_index(config)
    assert _structure_identity(config, target.snapshot_id) == structure_baseline
    assert _growth_identity(config, source.snapshot_id, target.snapshot_id) == growth_baseline

    backup = create_snapshot_index_backup(official, config.paths.cache_dir / "structure-growth.sqlite3")
    assert backup.status == SnapshotBackupStatus.READY and backup.manifest is not None
    manifest = Path(f"{backup.backup_database}.manifest.json").read_text(encoding="utf-8")
    assert "storage_growth" not in manifest and "storage_structure" not in manifest
    candidate = config.paths.cache_dir / "structure-growth-candidate.sqlite3"
    replay = replay_snapshot_index(config, candidate)
    assert replay.status == SnapshotReplayStatus.READY and replay.replacement_ready
    replacement = replace_snapshot_index(config, replay)
    assert replacement.status == SnapshotReplacementStatus.REPLACED
    assert _structure_identity(config, target.snapshot_id) == structure_baseline
    assert _growth_identity(config, source.snapshot_id, target.snapshot_id) == growth_baseline

    rollback = restore_snapshot_index_from_backup(config, backup, official)
    assert rollback.status == SnapshotRollbackStatus.RESTORED
    assert _structure_identity(config, target.snapshot_id) == structure_baseline
    assert _growth_identity(config, source.snapshot_id, target.snapshot_id) == growth_baseline
    assert storage_status(config).storage_status == "HEALTHY"
    assert _tree(config.paths.evidence_dir) == evidence_before
    connection = sqlite3.connect(official)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()
    assert not {name for name in tables if "structure" in name or "growth" in name}


def test_source_invalid_reuse_rejects_structure_and_growth_without_fallback(tmp_path: Path) -> None:
    config, _empty_config, _v1, source, target, _empty = _fixture(tmp_path)
    assert query_verified_snapshot_structure(config, target.snapshot_id).structure_digest
    stored_source = get_snapshot(config, source.snapshot_id)
    stored_target = get_snapshot(config, target.snapshot_id)
    source_evidence = config.paths.evidence_dir / str(stored_source.evidence_relative_path)
    target_evidence = config.paths.evidence_dir / str(stored_target.evidence_relative_path)
    target_before = target_evidence.read_bytes()
    source_evidence.unlink()
    assert verify_snapshot(config, target.snapshot_id).status == "INVALID"
    with pytest.raises(StructureError, match="STRUCTURE_INVALID"):
        query_verified_snapshot_structure(config, target.snapshot_id)
    with pytest.raises(GrowthError, match="GROWTH_INVALID"):
        query_verified_snapshot_growth(config, source.snapshot_id, target.snapshot_id)
    assert target_evidence.read_bytes() == target_before
