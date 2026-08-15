from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.export_public_release import export_public_release, is_public_path


ROOT = Path(__file__).resolve().parents[1]


def test_public_path_policy_excludes_private_runtime_artifacts() -> None:
    assert is_public_path("src/local_steward/cli.py")
    assert is_public_path("tests/test_storage_query.py")
    assert is_public_path("docs/ARCHITECTURE.md")
    assert is_public_path("docs/EVIDENCE-AND-STORAGE.md")
    assert is_public_path("LICENSE")
    assert not is_public_path("STATUS.md")
    assert not is_public_path("docs/STATUS-HISTORY.md")
    assert not is_public_path("docs/SNAPSHOT-EVIDENCE-V2-DESIGN.md")
    assert not is_public_path(
        "experiments/steward_exoskeleton/acceptance/run/transcript.jsonl"
    )
    assert not is_public_path(
        "experiments/steward_exoskeleton/archive/old-plugin/SKILL.md"
    )
    assert is_public_path(
        "experiments/steward_exoskeleton/r4d_r3d_plugin_source/skills/"
        "steward-codex/SKILL.md"
    )
    assert is_public_path(
        "experiments/steward_exoskeleton/r4d_r3d_plugin_candidate.py"
    )
    assert not is_public_path("experiments/file_agent_runtime/b1b_matrix.py")
    assert not is_public_path("tests/test_file_agent_b1b_matrix.py")
    assert not is_public_path("tests/test_steward_skill_source.py")
    assert is_public_path("tests/test_steward_native_agent_surface.py")
    assert not is_public_path("data/state.db")


def test_public_export_is_bounded_sanitized_and_deterministic(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = export_public_release(ROOT, first_root)
    second = export_public_release(ROOT, second_root)

    assert first == second
    assert first["file_count"] > 250
    assert first["collection_sha256"] == second["collection_sha256"]
    assert (first_root / "README.md").is_file()
    assert (first_root / "src/local_steward/cli.py").is_file()
    assert not (first_root / "STATUS.md").exists()
    assert not (first_root / "experiments/steward_exoskeleton/acceptance").exists()
    assert not (first_root / "experiments/file_agent_runtime").exists()
    assert not list(first_root.rglob("transcript.jsonl"))
    assert not list(first_root.rglob("tool-ledger.json"))

    manifest = json.loads(
        (first_root / "PUBLIC_RELEASE_MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest == first
    assert all(not Path(item["path"]).is_absolute() for item in manifest["files"])


def test_public_export_rejects_nonempty_or_nested_destination(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        export_public_release(ROOT, occupied)
    with pytest.raises(ValueError, match="outside"):
        export_public_release(ROOT, ROOT / "public-build")
