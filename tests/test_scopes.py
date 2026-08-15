from pathlib import Path

import pytest

from local_steward.config import load_config
from local_steward.errors import ScopeValidationError

from .conftest import write_config


def test_system_protected_scope_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text().replace('path = "~/managed-test"', 'path = "/System"'), encoding="utf-8"
    )
    with pytest.raises(ScopeValidationError):
        load_config(path, project_root=tmp_path)


def test_excluded_child_allowed(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text()
        + """
[[scopes]]
scope_id = "excluded"
role = "excluded_root"
path = "~/managed-test/private"
enabled = true
follow_directory_symlinks = false
allow_cross_mount = false
""",
        encoding="utf-8",
    )
    assert len(load_config(path, project_root=tmp_path).scopes) == 2


def test_overlapping_actionable_roots_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text()
        + """
[[scopes]]
scope_id = "reference"
role = "reference_root"
path = "~/managed-test/child"
enabled = true
follow_directory_symlinks = false
allow_cross_mount = false
""",
        encoding="utf-8",
    )
    with pytest.raises(ScopeValidationError):
        load_config(path, project_root=tmp_path)
