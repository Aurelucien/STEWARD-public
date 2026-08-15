"""Read-only query and CLI coverage for frozen duplicate/storage analysis."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.database import database_path
from local_steward.duplicate_analysis import compute_snapshot_duplicate_analysis
from local_steward.errors import DuplicateAnalysisError
from local_steward.models import FilesystemSnapshotV2
from local_steward.payload_hashing import PayloadLocality, default_payload_hash_policy
from local_steward.scan_budget import make_budget
from local_steward.snapshot_duplicate_query import query_verified_snapshot_duplicates
from local_steward.snapshots import create_snapshot

from .conftest import write_config
from .test_duplicate_analysis import _entry, _snapshot, _v2
from .test_protocol_completion import prepared_config


SNAPSHOT_ID = "00000000-0000-4000-8000-000000000001"


def _analysis():
    entries = (
        _v2(_entry("snapshot", "a-1", inode=10), "a" * 64),
        _v2(_entry("snapshot", "a-2", inode=11), "a" * 64),
        _v2(_entry("snapshot", "b-1", inode=20), "b" * 64),
        _v2(_entry("snapshot", "b-2", inode=21), "b" * 64),
        _v2(_entry("snapshot", "alias-1", inode=30, link_count=2), "c" * 64),
        _v2(_entry("snapshot", "alias-2", inode=30, link_count=2), "c" * 64),
    )
    return replace(compute_snapshot_duplicate_analysis(_snapshot(entries)), snapshot_id=SNAPSHOT_ID)


def _config_path(tmp_path: Path) -> Path:
    return write_config(tmp_path)


def test_query_pages_one_complete_analysis_and_preserves_complete_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def compute(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        return _analysis()

    monkeypatch.setattr(
        "local_steward.snapshot_duplicate_query.compute_verified_snapshot_duplicate_analysis", compute
    )
    from local_steward.config import load_config

    config = load_config(_config_path(tmp_path), project_root=tmp_path)
    first = query_verified_snapshot_duplicates(config, SNAPSHOT_ID, limit=1)
    middle = query_verified_snapshot_duplicates(config, SNAPSHOT_ID, limit=1, offset=1)
    last = query_verified_snapshot_duplicates(config, SNAPSHOT_ID, limit=1, offset=2)
    empty = query_verified_snapshot_duplicates(config, SNAPSHOT_ID, limit=1, offset=3)
    exact = query_verified_snapshot_duplicates(config, SNAPSHOT_ID, only_exact=True, limit=10)
    assert [item.payload_group_id for item in (*first.payload_equality_groups, *middle.payload_equality_groups, *last.payload_equality_groups)] == [
        item.payload_group_id for item in _analysis().payload_equality_groups
    ]
    assert first.analysis_digest == middle.analysis_digest == last.analysis_digest == exact.analysis_digest
    assert first.coverage == middle.coverage == exact.coverage
    assert first.hard_link_alias_sets == middle.hard_link_alias_sets
    assert first.integrity_conflicts == middle.integrity_conflicts
    assert last.has_more is False and last.next_offset is None
    assert empty.payload_equality_groups == () and empty.has_more is False
    assert len(exact.payload_equality_groups) == 2
    assert calls == 5


@pytest.mark.parametrize(
    "limit,offset,only_exact",
    [(0, 0, False), (-1, 0, False), (1001, 0, False), (True, 0, False), (1, -1, False), (1, True, False), (1, 0, "yes")],
)
def test_query_rejects_invalid_view_before_core_computation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: object,
    offset: object,
    only_exact: object,
) -> None:
    monkeypatch.setattr(
        "local_steward.snapshot_duplicate_query.compute_verified_snapshot_duplicate_analysis",
        lambda *_args, **_kwargs: pytest.fail("core must not run for invalid query input"),
    )
    from local_steward.config import load_config

    config = load_config(_config_path(tmp_path), project_root=tmp_path)
    with pytest.raises(DuplicateAnalysisError, match="DUPLICATE_INVALID"):
        query_verified_snapshot_duplicates(  # type: ignore[arg-type]
            config, SNAPSHOT_ID, limit=limit, offset=offset, only_exact=only_exact
        )


def test_duplicate_cli_renders_human_json_and_unknown_physical_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "local_steward.snapshot_duplicate_query.compute_verified_snapshot_duplicate_analysis",
        lambda *_args, **_kwargs: _analysis(),
    )
    command = ["--config", str(_config_path(tmp_path)), "snapshots", "duplicates", SNAPSHOT_ID, "--limit", "1"]
    runner = CliRunner()
    human = runner.invoke(app, command)
    encoded = runner.invoke(app, ["--format", "json", *command])
    assert human.exit_code == encoded.exit_code == 0
    assert "Complete Analysis Digest:" in human.stdout
    assert "not multiple storage copies" in human.stdout
    assert "Reclaimable Space: UNKNOWN" in human.stdout
    assert "safe to delete" not in human.stdout.lower()
    payload = json.loads(encoded.stdout)
    result = payload["result"]["duplicate_query"]
    assert payload["command"] == "snapshots.duplicates" and payload["status"] == "OK"
    assert result["analysis_digest"] == _analysis().analysis_digest
    assert result["returned_payload_equality_group_count"] == 1
    assert result["physical_storage"]["reclaimable_bytes"] is None
    assert result["physical_storage"]["reclaimable_status"] == "UNKNOWN"
    assert result["hard_link_alias_sets"][0]["member_entries"][0] == {
        "snapshot_id": "snapshot",
        "scope_id": "managed",
        "relative_path": "alias-1",
    }


def test_duplicate_cli_rejects_malformed_id_and_invalid_pagination(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_path(tmp_path)
    malformed = runner.invoke(app, ["--config", str(config_path), "snapshots", "duplicates", "not-a-uuid"])
    invalid_page = runner.invoke(
        app,
        ["--config", str(config_path), "snapshots", "duplicates", SNAPSHOT_ID, "--limit", "0"],
    )
    assert malformed.exit_code == invalid_page.exit_code == 2
    assert "DUPLICATE_INVALID" in malformed.stderr
    assert "DUPLICATE_INVALID" in invalid_page.stderr


def test_verified_query_is_read_only_and_v1_is_a_valid_empty_group_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = prepared_config(tmp_path)
    root = tmp_path / "observed"
    root.mkdir()
    (root / "a.txt").write_text("same", encoding="utf-8")
    (root / "b.txt").write_text("same", encoding="utf-8")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=root),))
    v1 = create_snapshot(config, (), make_budget())
    v2 = create_snapshot(
        config,
        (),
        make_budget(),
        default_payload_hash_policy(),
        locality_provider=lambda _path: PayloadLocality.LOCAL,
    )
    assert isinstance(v2, FilesystemSnapshotV2)
    database_before = database_path(config).read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("duplicate query must not scan a live scope")

    monkeypatch.setattr(Path, "iterdir", forbidden)
    v1_query = query_verified_snapshot_duplicates(config, v1.snapshot_id)
    v2_query = query_verified_snapshot_duplicates(config, v2.snapshot_id)
    monkeypatch.undo()
    assert v1_query.payload_equality_groups == ()
    assert v1_query.coverage.payload_unknown_regular_entry_count == 2
    assert v2_query.payload_equality_groups[0].is_exact_duplicate
    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before
