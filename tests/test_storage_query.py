"""Read-only view, pagination, and CLI coverage for storage structure/growth."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.database import database_path
from local_steward.errors import StructureError
from local_steward.models import FilesystemObjectType, GrowthRank, StructureRank
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot
from local_steward.storage_growth import compute_snapshot_growth
from local_steward.storage_query import (
    query_verified_snapshot_growth,
    query_verified_snapshot_structure,
)
from local_steward.storage_structure import compute_snapshot_structure

from .conftest import write_config
from .test_duplicate_analysis import _entry, _snapshot
from .test_protocol_completion import prepared_config
from .test_storage_growth import _pair


def _structure_result():
    return compute_snapshot_structure(
        _snapshot(
            (
                _entry("snapshot", ".", object_type=FilesystemObjectType.DIRECTORY),
                _entry("snapshot", "a/file", size_bytes=3),
                _entry("snapshot", "a/b/file", size_bytes=7),
                _entry("snapshot", "abc/file", size_bytes=5),
            )
        )
    )


def _growth_result():
    base, target = _pair(
        (
            _entry("base", ".", object_type=FilesystemObjectType.DIRECTORY),
            _entry("base", "old/file", size_bytes=5),
            _entry("base", "same", size_bytes=2),
        ),
        (
            _entry("target", ".", object_type=FilesystemObjectType.DIRECTORY),
            _entry("target", "new/file", size_bytes=5),
            _entry("target", "same", size_bytes=4),
        ),
    )
    return compute_snapshot_growth(base, target)


def test_structure_view_effective_root_depth_ranking_and_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_steward.config import load_config

    result = _structure_result()
    calls = 0

    def compute(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        return result

    monkeypatch.setattr("local_steward.storage_query.compute_verified_snapshot_structure", compute)
    config = load_config(write_config(tmp_path), project_root=tmp_path)
    depth_zero = query_verified_snapshot_structure(
        config, "snapshot", path_prefix="a", depth=0, limit=10
    )
    depth_one = query_verified_snapshot_structure(
        config, "snapshot", path_prefix="a", depth=1, limit=10
    )
    ranked = query_verified_snapshot_structure(
        config,
        "snapshot",
        rank=StructureRank.RECURSIVE_LOGICAL_BYTES,
        min_bytes=5,
        limit=1,
    )
    next_page = query_verified_snapshot_structure(
        config,
        "snapshot",
        rank=StructureRank.RECURSIVE_LOGICAL_BYTES,
        min_bytes=5,
        limit=1,
        offset=1,
    )
    assert [item.relative_directory_path for item in depth_zero.path_nodes] == ["a"]
    assert [item.relative_directory_path for item in depth_one.path_nodes] == ["a", "a/b"]
    assert ranked.effective_view_roots[0].relative_directory_path == "."
    assert ranked.structure_digest == next_page.structure_digest == result.structure_digest
    assert ranked.coverage == next_page.coverage == result.coverage
    assert ranked.path_nodes[0].path_node_id != next_page.path_nodes[0].path_node_id
    assert calls == 4


def test_selection_errors_precede_prefix_resolution_and_empty_view_is_legal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_steward.config import load_config

    monkeypatch.setattr(
        "local_steward.storage_query.compute_verified_snapshot_structure", lambda *_args, **_kwargs: _structure_result()
    )
    config = load_config(write_config(tmp_path), project_root=tmp_path)
    with pytest.raises(StructureError, match="scope is not present"):
        query_verified_snapshot_structure(config, "snapshot", scope="missing", path_prefix="/bad")
    with pytest.raises(StructureError, match="path-prefix is not present"):
        query_verified_snapshot_structure(config, "snapshot", path_prefix="a/bc")
    with pytest.raises(StructureError, match="min-bytes requires a rank"):
        query_verified_snapshot_structure(config, "snapshot", min_bytes=1)
    empty = query_verified_snapshot_structure(
        config,
        "snapshot",
        rank=StructureRank.RECURSIVE_LOGICAL_BYTES,
        min_bytes=100,
    )
    assert empty.path_nodes == () and empty.selected_path_node_count == 0


def test_growth_view_ranks_after_depth_and_preserves_complete_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_steward.config import load_config

    result = _growth_result()
    monkeypatch.setattr("local_steward.storage_query.compute_verified_snapshot_growth", lambda *_args, **_kwargs: result)
    config = load_config(write_config(tmp_path), project_root=tmp_path)
    depth = query_verified_snapshot_growth(
        config, "base", "target", path_prefix="old", depth=0, rank=GrowthRank.REMOVED
    )
    added = query_verified_snapshot_growth(
        config, "base", "target", rank=GrowthRank.ADDED, min_bytes=5, limit=1
    )
    empty = query_verified_snapshot_growth(
        config, "base", "target", rank=GrowthRank.NET_SHRINK, offset=100
    )
    assert [item.relative_directory_path for item in depth.path_nodes] == ["old"]
    assert added.path_nodes[0].relative_directory_path == "new"
    assert added.growth_digest == result.growth_digest
    assert added.coverage == result.coverage and added.contributions == result.contributions
    assert empty.path_nodes == () and empty.has_more is False


def test_cli_human_json_and_verified_queries_are_read_only(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    root = tmp_path / "observed"
    root.mkdir()
    (root / "a.txt").write_text("one", encoding="utf-8")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=root),))
    base = create_snapshot(config, (), make_budget())
    (root / "a.txt").write_text("two words", encoding="utf-8")
    target = create_snapshot(config, (), make_budget())
    database_before = database_path(config).read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }
    runner = CliRunner()
    command = ["--config", str(config.source_path), "snapshots", "structure", target.snapshot_id]
    human = runner.invoke(app, command)
    encoded = runner.invoke(app, ["--format", "json", *command])
    growth = runner.invoke(
        app,
        ["--format", "json", "--config", str(config.source_path), "snapshots", "growth", base.snapshot_id, target.snapshot_id],
    )
    assert human.exit_code == encoded.exit_code == growth.exit_code == 0
    assert "Storage Structure" in human.stdout
    structure_payload = json.loads(encoded.stdout)
    growth_payload = json.loads(growth.stdout)
    assert structure_payload["command"] == "snapshots.structure"
    assert structure_payload["result"]["structure_query"]["structure_digest"]
    assert growth_payload["command"] == "snapshots.growth"
    assert growth_payload["result"]["growth_query"]["coverage"]["known_net_logical_delta"] == 6
    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before


def test_cli_selection_and_pagination_errors_are_typed(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = write_config(tmp_path)
    malformed = runner.invoke(app, ["--config", str(config_path), "snapshots", "structure", "not-a-uuid"])
    invalid_page = runner.invoke(
        app,
        ["--config", str(config_path), "snapshots", "growth", "not-a-uuid", "also-not-a-uuid", "--limit", "0"],
    )
    assert malformed.exit_code == invalid_page.exit_code == 2
    assert "STRUCTURE_INVALID" in malformed.stderr
    assert "GROWTH_INVALID" in invalid_page.stderr
