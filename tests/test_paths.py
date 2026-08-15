from pathlib import Path

import pytest

from local_steward.errors import ConfigurationSchemaError
from local_steward.paths import normalize_path, overlaps


def test_relative_path_uses_base(tmp_path: Path) -> None:
    assert normalize_path("data/cache", base_dir=tmp_path) == tmp_path / "data/cache"


def test_environment_variable_is_rejected() -> None:
    with pytest.raises(ConfigurationSchemaError):
        normalize_path("$HOME/file")


def test_overlap_detects_containment(tmp_path: Path) -> None:
    assert overlaps(tmp_path / "one", tmp_path / "one/two")
