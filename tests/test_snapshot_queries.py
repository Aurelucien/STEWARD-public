from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.errors import SnapshotBudgetError, SnapshotNotFoundError
from local_steward.models import FilesystemObjectType
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot, get_snapshot, list_snapshot_entries

from .test_protocol_completion import prepared_config


def snapshot_fixture(tmp_path: Path):
    config = prepared_config(tmp_path)
    root = tmp_path / "scope"
    root.mkdir()
    (root / "a.txt").write_text("a")
    (root / "ab.txt").write_text("b")
    directory = root / "dir"
    directory.mkdir()
    (directory / "child.txt").write_text("c")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=root),))
    return config, create_snapshot(config, (), make_budget())


def test_get_snapshot_and_entries_filter_page(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    loaded = get_snapshot(config, snapshot.snapshot_id)
    assert loaded.snapshot_id == snapshot.snapshot_id and loaded.evidence_id
    page = list_snapshot_entries(config, snapshot.snapshot_id, path_prefix="dir", limit=1)
    assert [entry.relative_path for entry in page.entries] == ["dir"] and page.has_more
    regular = list_snapshot_entries(
        config, snapshot.snapshot_id, object_type=FilesystemObjectType.REGULAR_FILE
    )
    assert all(entry.object_type == FilesystemObjectType.REGULAR_FILE for entry in regular.entries)


def test_query_errors_are_stable(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    with pytest.raises(SnapshotNotFoundError):
        get_snapshot(config, "00000000-0000-4000-8000-000000000000")
    with pytest.raises(SnapshotBudgetError):
        list_snapshot_entries(config, snapshot.snapshot_id, limit=0)
    with pytest.raises(SnapshotBudgetError):
        list_snapshot_entries(config, snapshot.snapshot_id, path_prefix="../escape")


def test_show_and_entries_json(tmp_path: Path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    runner = CliRunner()
    shown = runner.invoke(
        app,
        [
            "--config",
            str(config.source_path),
            "--format",
            "json",
            "snapshots",
            "show",
            snapshot.snapshot_id,
        ],
    )
    entries = runner.invoke(
        app,
        [
            "--config",
            str(config.source_path),
            "--format",
            "json",
            "snapshots",
            "entries",
            snapshot.snapshot_id,
            "--limit",
            "1",
        ],
    )
    assert shown.exit_code == entries.exit_code == 0
    assert snapshot.run_id in shown.stdout and '"page"' in entries.stdout
