"""Isolated end-to-end and recovery non-regression acceptance for relations."""

import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.database import database_path
from local_steward.models import (
    FilesystemSnapshotV2,
    PayloadObservationProvenance,
    RelationKind,
    SnapshotBackupStatus,
    SnapshotReplacementStatus,
    SnapshotRollbackStatus,
    SnapshotReplayStatus,
)
from local_steward.payload_hashing import PayloadLocality, default_payload_hash_policy
from local_steward.snapshot_backup import create_snapshot_index_backup
from local_steward.snapshot_relation_query import query_verified_snapshot_relations
from local_steward.snapshot_relations import canonical_relation_set, compute_verified_snapshot_relations
from local_steward.snapshot_replacement import replace_snapshot_index
from local_steward.snapshot_replay import replay_snapshot_index
from local_steward.snapshot_rollback import restore_snapshot_index_from_backup
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot, verify_snapshot
from local_steward.storage import rebuild_index, storage_status

from .test_protocol_completion import prepared_config


def _local(_: Path) -> PayloadLocality:
    return PayloadLocality.LOCAL


def _file_tree(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }


def _fixture(tmp_path: Path):
    config = prepared_config(tmp_path)
    root = tmp_path / "observed"
    root.mkdir()
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    (root / "change.txt").write_text("before", encoding="utf-8")
    (root / "old.txt").write_text("rename", encoding="utf-8")
    (root / "empty.txt").write_bytes(b"")
    (root / "unicodé.txt").write_text("unicode", encoding="utf-8")
    (root / "hard-a.txt").write_text("hard-link", encoding="utf-8")
    (root / "hard-b.txt").hardlink_to(root / "hard-a.txt")
    (root / "link").symlink_to("old.txt")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=root),))

    v1_first = create_snapshot(config, (), make_budget())
    os.chmod(root / "keep.txt", 0o600)
    v1_second = create_snapshot(config, (), make_budget())
    direct_policy = default_payload_hash_policy()
    v2_base = create_snapshot(
        config,
        (),
        make_budget(),
        direct_policy,
        locality_provider=_local,
    )
    assert isinstance(v2_base, FilesystemSnapshotV2)

    (root / "old.txt").rename(root / "new.txt")
    (root / "hard-new.txt").hardlink_to(root / "hard-a.txt")
    (root / "hard-a.txt").unlink()
    (root / "hard-b.txt").unlink()
    (root / "change.txt").write_text("after", encoding="utf-8")
    (root / "link").unlink()
    (root / "link").symlink_to("new.txt")
    v2_target = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(allow_verified_reuse=True),
        locality_provider=_local,
    )
    assert isinstance(v2_target, FilesystemSnapshotV2)
    assert any(
        entry.payload_observation.provenance
        == PayloadObservationProvenance.REUSED_FROM_VERIFIED_SNAPSHOT
        for entry in v2_target.entries
    )
    return config, v1_first, v1_second, v2_base, v2_target


def test_isolated_relation_cli_pagination_and_version_boundaries(tmp_path: Path) -> None:
    config, v1_first, v1_second, v2_base, v2_target = _fixture(tmp_path)
    assert all(
        verify_snapshot(config, snapshot.snapshot_id).status == "VALID"
        for snapshot in (v1_first, v1_second, v2_base, v2_target)
    )
    assert query_verified_snapshot_relations(
        config, v1_first.snapshot_id, v1_second.snapshot_id
    ).relation_schema_version == 1
    assert query_verified_snapshot_relations(
        config, v1_second.snapshot_id, v2_base.snapshot_id
    ).relation_schema_version == 1

    full = query_verified_snapshot_relations(
        config, v2_base.snapshot_id, v2_target.snapshot_id, limit=1_000
    )
    kinds = {item.kind for item in full.relation_items}
    assert RelationKind.SAME_LOCATION_CONTENT_CHANGED in kinds
    assert RelationKind.SAME_LOCATION_SYMLINK_TARGET_CHANGED in kinds

    runner = CliRunner()
    command = [
        "--config",
        str(config.source_path),
        "snapshots",
        "relate",
        v2_base.snapshot_id,
        v2_target.snapshot_id,
    ]
    human = runner.invoke(app, command)
    encoded = runner.invoke(app, ["--format", "json", *command])
    assert human.exit_code == encoded.exit_code == 0
    unknown_human = runner.invoke(
        app,
        [
            "--config",
            str(config.source_path),
            "snapshots",
            "relate",
            v1_second.snapshot_id,
            v2_base.snapshot_id,
        ],
    )
    assert unknown_human.exit_code == 0
    assert "evidence insufficient" in unknown_human.stdout
    payload = json.loads(encoded.stdout)["result"]["relation_query"]
    assert payload["relation_set_digest"] == full.relation_set_digest

    page_ids: list[str] = []
    for offset in range(full.relation_item_count):
        page = query_verified_snapshot_relations(
            config, v2_base.snapshot_id, v2_target.snapshot_id, limit=1, offset=offset
        )
        assert page.relation_set_digest == full.relation_set_digest
        page_ids.extend(item.relation_id for item in page.relation_items)
    assert page_ids == [item.relation_id for item in full.relation_items]
    assert query_verified_snapshot_relations(
        config, v2_base.snapshot_id, v2_target.snapshot_id, limit=1, offset=len(page_ids)
    ).relation_items == ()
    filtered = query_verified_snapshot_relations(
        config,
        v2_base.snapshot_id,
        v2_target.snapshot_id,
        kind=RelationKind.SAME_LOCATION_CONTENT_CHANGED,
    )
    assert filtered.relation_set_digest == full.relation_set_digest
    assert all(item.kind == RelationKind.SAME_LOCATION_CONTENT_CHANGED for item in filtered.relation_items)

    same = runner.invoke(
        app,
        [
            "--config",
            str(config.source_path),
            "snapshots",
            "relate",
            v2_base.snapshot_id,
            v2_base.snapshot_id,
        ],
    )
    reverse = runner.invoke(
        app,
        [
            "--config",
            str(config.source_path),
            "snapshots",
            "relate",
            v2_target.snapshot_id,
            v2_base.snapshot_id,
        ],
    )
    missing = runner.invoke(
        app,
        [
            "--config",
            str(config.source_path),
            "snapshots",
            "relate",
            "00000000-0000-4000-8000-000000000099",
            v2_target.snapshot_id,
        ],
    )
    assert same.exit_code == reverse.exit_code == missing.exit_code == 2
    assert all("RELATION_INVALID" in result.stderr for result in (same, reverse, missing))


def test_recovery_chain_does_not_persist_or_change_relation_facts(tmp_path: Path) -> None:
    config, _v1_first, _v1_second, v2_base, v2_target = _fixture(tmp_path)
    relation_set = compute_verified_snapshot_relations(config, v2_base.snapshot_id, v2_target.snapshot_id)
    baseline_bytes = canonical_relation_set(relation_set)
    baseline_digest = relation_set.relation_set_digest
    evidence_before = _file_tree(config.paths.evidence_dir)
    official = database_path(config)

    rebuild_index(config)
    after_rebuild = compute_verified_snapshot_relations(config, v2_base.snapshot_id, v2_target.snapshot_id)
    assert canonical_relation_set(after_rebuild) == baseline_bytes
    assert after_rebuild.relation_set_digest == baseline_digest

    backup = create_snapshot_index_backup(official, config.paths.cache_dir / "acceptance-backup.sqlite3")
    assert backup.status == SnapshotBackupStatus.READY and backup.manifest is not None
    manifest_text = Path(f"{backup.backup_database}.manifest.json").read_text(encoding="utf-8")
    assert "relation" not in manifest_text
    candidate = config.paths.cache_dir / "acceptance-candidate.sqlite3"
    replay = replay_snapshot_index(config, candidate)
    assert replay.status == SnapshotReplayStatus.READY and replay.replacement_ready
    replacement = replace_snapshot_index(config, replay)
    assert replacement.status == SnapshotReplacementStatus.REPLACED
    after_replacement = compute_verified_snapshot_relations(
        config, v2_base.snapshot_id, v2_target.snapshot_id
    )
    assert canonical_relation_set(after_replacement) == baseline_bytes
    assert after_replacement.relation_set_digest == baseline_digest

    rollback = restore_snapshot_index_from_backup(config, backup, official)
    assert rollback.status == SnapshotRollbackStatus.RESTORED
    after_rollback = compute_verified_snapshot_relations(config, v2_base.snapshot_id, v2_target.snapshot_id)
    assert canonical_relation_set(after_rollback) == baseline_bytes
    assert after_rollback.relation_set_digest == baseline_digest
    assert storage_status(config).storage_status == "HEALTHY"
    assert _file_tree(config.paths.evidence_dir) == evidence_before

    connection = sqlite3.connect(official)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()
    assert not {name for name in tables if "relation" in name}
    assert not [path for path in config.paths.data_dir.rglob("*") if "relation" in path.name]
    assert hashlib.sha256(canonical_relation_set(after_rollback)).hexdigest() == baseline_digest
