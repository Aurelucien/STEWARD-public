import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_steward.cli import app
from local_steward.database import database_path
from local_steward.errors import SnapshotScopeError
from local_steward.filesystem import select_scopes
from local_steward.models import ScopeRole
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot

from .test_protocol_completion import prepared_config


def _configured_scope(tmp_path: Path):
    config = prepared_config(tmp_path)
    root = tmp_path / "scope"
    root.mkdir()
    (root / "file.txt").write_text("snapshot", encoding="utf-8")
    return replace(config, scopes=(replace(config.scopes[0], normalized_path=root),)), root


def _persistent_state(config) -> tuple[bytes, dict[Path, bytes]]:
    database = database_path(config)
    return (
        database.read_bytes(),
        {
            path.relative_to(config.paths.evidence_dir): path.read_bytes()
            for path in config.paths.evidence_dir.rglob("*")
            if path.is_file()
        },
    )


def _create_command(config, *scope_ids: str, encoded: bool = False) -> list[str]:
    command = ["--config", str(config.source_path)]
    if encoded:
        command.extend(("--format", "json"))
    command.extend(("snapshots", "create"))
    for scope_id in scope_ids:
        command.extend(("--scope", scope_id))
    return command


def test_unknown_scope_cli_fails_without_persistent_side_effects(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    before = _persistent_state(config)
    runner = CliRunner()

    human = runner.invoke(app, _create_command(config, "unknown"))
    encoded = runner.invoke(app, _create_command(config, "unknown", encoded=True))

    assert human.exit_code == encoded.exit_code == 2
    assert "Error [SNAPSHOT_SCOPE_INVALID]: unknown scope IDs: unknown" in human.stderr
    assert "KeyError" not in human.stderr and "INTERNAL_ERROR" not in human.stderr
    payload = json.loads(encoded.stdout)
    assert payload["errors"] == [
        {"code": "SNAPSHOT_SCOPE_INVALID", "message": "unknown scope IDs: unknown"}
    ]
    assert "KeyError" not in encoded.stdout and "traceback" not in encoded.stdout.lower()
    assert _persistent_state(config) == before


def test_multiple_unknown_scopes_are_reported_in_stable_order(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    before = _persistent_state(config)

    result = CliRunner().invoke(app, _create_command(config, "zeta", "alpha", "zeta"))

    assert result.exit_code == 2
    assert result.stderr == "Error [SNAPSHOT_SCOPE_INVALID]: unknown scope IDs: alpha, zeta\n"
    assert _persistent_state(config) == before


def test_disabled_scope_is_rejected_before_run_creation(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    disabled = replace(config, scopes=(replace(config.scopes[0], enabled=False),))
    before = _persistent_state(config)

    with pytest.raises(SnapshotScopeError, match="scope unavailable: managed"):
        create_snapshot(disabled, ("managed",), make_budget())

    assert _persistent_state(config) == before


def test_unsupported_symlink_policy_is_rejected_before_run_creation(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    unsupported = replace(
        config, scopes=(replace(config.scopes[0], follow_directory_symlinks=True),)
    )
    before = _persistent_state(config)

    with pytest.raises(SnapshotScopeError, match="scope symlink policy unsupported: managed"):
        create_snapshot(unsupported, ("managed",), make_budget())

    assert _persistent_state(config) == before


def test_unsupported_scope_role_is_rejected_before_run_creation(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)
    unsupported = replace(
        config, scopes=(replace(config.scopes[0], role=ScopeRole.EXCLUDED_ROOT),)
    )
    before = _persistent_state(config)

    with pytest.raises(SnapshotScopeError, match="scope unavailable: managed"):
        create_snapshot(unsupported, ("managed",), make_budget())

    assert _persistent_state(config) == before


def test_explicit_legal_scope_preserves_snapshot_creation(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)

    snapshot = create_snapshot(config, ("managed",), make_budget())

    assert snapshot.scope_ids == ("managed",)
    assert snapshot.entry_count == 2


def test_default_enabled_managed_scope_preserves_snapshot_creation(tmp_path: Path) -> None:
    config, _root = _configured_scope(tmp_path)

    selected = select_scopes(config, ())
    snapshot = create_snapshot(config, (), make_budget())

    assert tuple(scope.scope_id for scope in selected) == ("managed",)
    assert snapshot.scope_ids == ("managed",)
