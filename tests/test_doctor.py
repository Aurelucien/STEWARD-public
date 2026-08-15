from pathlib import Path

from local_steward.config import load_config
from local_steward.doctor import run_doctor

from .conftest import write_config


def test_doctor_removes_sqlite_probe(tmp_path: Path) -> None:
    (tmp_path / "data/cache").mkdir(parents=True)
    (tmp_path / "data/evidence").mkdir()
    (tmp_path / "data/quarantine").mkdir()
    path = write_config(tmp_path)
    summary = run_doctor(load_config(path, project_root=tmp_path))
    assert not list((tmp_path / "data").glob(".local-steward-doctor-*"))
    assert summary.checks
