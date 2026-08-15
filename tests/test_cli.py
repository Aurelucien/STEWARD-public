from pathlib import Path

from typer.testing import CliRunner

from local_steward.cli import app

from .conftest import write_config


def test_help_works() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0


def test_validate_json_is_one_document(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["--config", str(write_config(tmp_path)), "--format", "json", "config", "validate"]
    )
    assert result.exit_code == 0
    assert result.stdout.count("\n") == 1
