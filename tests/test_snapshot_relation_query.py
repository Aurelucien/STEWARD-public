"""Read-only query and CLI coverage for frozen relation results."""

from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.errors import RelationError
from local_steward.models import (
    RelationAmbiguityGroup,
    RelationCertainty,
    RelationItem,
    RelationKind,
    RelationSet,
    SnapshotEntryReference,
)
from local_steward.snapshot_relation_query import query_verified_snapshot_relations
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot

from .conftest import write_config
from .test_protocol_completion import prepared_config


BASE = "00000000-0000-4000-8000-000000000001"
TARGET = "00000000-0000-4000-8000-000000000002"


def _reference(snapshot_id: str, path: str) -> SnapshotEntryReference:
    return SnapshotEntryReference(snapshot_id, "managed", path)


def _item(
    relation_id: str,
    kind: RelationKind,
    certainty: RelationCertainty,
    *,
    group_id: str | None = None,
) -> RelationItem:
    return RelationItem(
        relation_id,
        kind,
        certainty,
        (kind.value,),
        group_id,
        (_reference(BASE, f"base-{relation_id}"),),
        (_reference(TARGET, f"target-{relation_id}"),),
    )


def _relation_set() -> RelationSet:
    group = RelationAmbiguityGroup(
        "group-1",
        (_reference(BASE, "old-a"), _reference(BASE, "old-b")),
        (_reference(TARGET, "new"),),
    )
    return RelationSet(
        1,
        "cross_snapshot_relation",
        1,
        BASE,
        TARGET,
        (
            _item("1", RelationKind.RENAME_CANDIDATE, RelationCertainty.CANDIDATE),
            _item(
                "2",
                RelationKind.AMBIGUOUS_LOCATION_TRANSITION,
                RelationCertainty.AMBIGUOUS,
                group_id="group-1",
            ),
            _item("3", RelationKind.SAME_LOCATION_CONTENT_UNKNOWN, RelationCertainty.UNKNOWN),
        ),
        (group,),
        "d" * 64,
    )


def _config_path(tmp_path: Path) -> Path:
    return write_config(tmp_path)


def test_query_applies_filter_and_offset_after_one_complete_relation_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []

    def compute(config, base_id, target_id):
        calls.append((config, base_id, target_id))
        return _relation_set()

    monkeypatch.setattr(
        "local_steward.snapshot_relation_query.compute_verified_snapshot_relations", compute
    )
    from local_steward.config import load_config

    config = load_config(_config_path(tmp_path), project_root=tmp_path)
    first = query_verified_snapshot_relations(config, BASE, TARGET, limit=1, offset=0)
    second = query_verified_snapshot_relations(config, BASE, TARGET, limit=1, offset=1)
    filtered = query_verified_snapshot_relations(
        config, BASE, TARGET, kind=RelationKind.AMBIGUOUS_LOCATION_TRANSITION, limit=1
    )
    assert [item.relation_id for item in first.relation_items] == ["1"]
    assert first.relation_item_count == 3 and first.next_offset == 1
    assert [item.relation_id for item in second.relation_items] == ["2"]
    assert second.ambiguity_groups == _relation_set().ambiguity_groups
    assert second.next_offset == 2
    assert [item.relation_id for item in filtered.relation_items] == ["2"]
    assert filtered.relation_set_digest == "d" * 64
    last = query_verified_snapshot_relations(config, BASE, TARGET, limit=1, offset=2)
    assert last.has_more is False and last.next_offset is None
    assert len(calls) == 4


@pytest.mark.parametrize("limit,offset", [(0, 0), (-1, 0), (1001, 0), (True, 0), (1, -1), (1, True)])
def test_query_rejects_invalid_pagination_before_computation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: object, offset: object
) -> None:
    monkeypatch.setattr(
        "local_steward.snapshot_relation_query.compute_verified_snapshot_relations",
        lambda *_args: pytest.fail("core must not run for invalid pagination"),
    )
    from local_steward.config import load_config

    config = load_config(_config_path(tmp_path), project_root=tmp_path)
    with pytest.raises(RelationError, match="RELATION_INVALID"):
        query_verified_snapshot_relations(config, BASE, TARGET, limit=limit, offset=offset)  # type: ignore[arg-type]


def test_relation_cli_renders_human_and_json_without_a_repository_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "local_steward.snapshot_relation_query.compute_verified_snapshot_relations",
        lambda *_args, **_kwargs: _relation_set(),
    )
    config_path = _config_path(tmp_path)
    command = ["--config", str(config_path), "snapshots", "relate", BASE, TARGET, "--limit", "1"]
    runner = CliRunner()
    human = runner.invoke(app, command)
    encoded = runner.invoke(app, ["--format", "json", *command])
    assert human.exit_code == encoded.exit_code == 0
    assert "CANDIDATE (not a confirmed move or rename)" in human.stdout
    assert "Relation Set Digest: " + "d" * 64 in human.stdout
    import json

    payload = json.loads(encoded.stdout)
    result = payload["result"]["relation_query"]
    assert payload["command"] == "snapshots.relate" and payload["status"] == "OK"
    assert result["relation_set_digest"] == "d" * 64
    assert result["returned_relation_item_count"] == 1
    assert result["relation_items"][0]["source_entries"][0] == {
        "snapshot_id": BASE,
        "scope_id": "managed",
        "relative_path": "base-1",
    }


def test_relation_cli_keeps_ambiguity_members_and_unknown_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "local_steward.snapshot_relation_query.compute_verified_snapshot_relations",
        lambda *_args, **_kwargs: _relation_set(),
    )
    runner = CliRunner()
    config_path = _config_path(tmp_path)
    ambiguity = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "snapshots",
            "relate",
            BASE,
            TARGET,
            "--offset",
            "1",
            "--limit",
            "1",
        ],
    )
    unknown = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "snapshots",
            "relate",
            BASE,
            TARGET,
            "--offset",
            "2",
            "--limit",
            "1",
        ],
    )
    assert ambiguity.exit_code == unknown.exit_code == 0
    assert "AMBIGUOUS (no one-to-one assignment)" in ambiguity.stdout
    assert "UNKNOWN (evidence insufficient)" in unknown.stdout


def test_relation_cli_json_includes_the_complete_page_ambiguity_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "local_steward.snapshot_relation_query.compute_verified_snapshot_relations",
        lambda *_args, **_kwargs: _relation_set(),
    )
    import json

    result = CliRunner().invoke(
        app,
        [
            "--format",
            "json",
            "--config",
            str(_config_path(tmp_path)),
            "snapshots",
            "relate",
            BASE,
            TARGET,
            "--offset",
            "1",
            "--limit",
            "1",
        ],
    )
    payload = json.loads(result.stdout)["result"]["relation_query"]
    assert result.exit_code == 0
    assert payload["ambiguity_groups"][0]["ambiguity_group_id"] == "group-1"
    assert [item["relative_path"] for item in payload["ambiguity_groups"][0]["source_entries"]] == [
        "old-a",
        "old-b",
    ]


def test_relation_cli_rejects_bad_ids_and_kind_filter(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_path(tmp_path)
    malformed = runner.invoke(
        app,
        ["--config", str(config_path), "snapshots", "relate", "not-a-uuid", TARGET],
    )
    invalid_kind = runner.invoke(
        app,
        ["--config", str(config_path), "snapshots", "relate", BASE, TARGET, "--kind", "MOVE"],
    )
    assert malformed.exit_code == invalid_kind.exit_code == 2
    assert "RELATION_INVALID" in malformed.stderr
    assert "RELATION_INVALID" in invalid_kind.stderr


def test_verified_query_reads_existing_facts_without_mutating_database_or_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = prepared_config(tmp_path)
    root = tmp_path / "observed"
    root.mkdir()
    (root / "alpha.txt").write_text("one", encoding="utf-8")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=root),))
    base = create_snapshot(config, (), make_budget())
    (root / "alpha.txt").write_text("two", encoding="utf-8")
    target = create_snapshot(config, (), make_budget())
    database = config.paths.data_dir / "state.db"
    database_before = database.read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the relation query must not scan a live scope")

    monkeypatch.setattr(Path, "iterdir", forbidden)
    result = query_verified_snapshot_relations(config, base.snapshot_id, target.snapshot_id)
    monkeypatch.undo()

    assert result.base_snapshot_id == base.snapshot_id
    assert result.target_snapshot_id == target.snapshot_id
    assert result.relation_item_count == len(result.relation_items)
    assert database.read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before
