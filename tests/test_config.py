from pathlib import Path

import pytest

from local_steward.config import load_config
from local_steward.errors import ConfigurationSchemaError

from .conftest import write_config


def test_valid_config_loads_and_internal_paths_use_root(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), project_root=tmp_path)
    assert config.paths.cache_dir == tmp_path / "data/cache"
    assert str(config.scopes[0].normalized_path).endswith("managed-test")


@pytest.mark.parametrize("replacement", ["schema_version = 2", 'schema_version = "1"'])
def test_invalid_schema_version_fails(tmp_path: Path, replacement: str) -> None:
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace("schema_version = 1", replacement), encoding="utf-8")
    with pytest.raises(ConfigurationSchemaError):
        load_config(path, project_root=tmp_path)


def test_unknown_field_fails(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text(path.read_text() + "\nunknown = 1\n", encoding="utf-8")
    with pytest.raises(ConfigurationSchemaError):
        load_config(path, project_root=tmp_path)
