"""Build a deterministic public STEWARD source tree from an explicit allowlist."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from collections.abc import Sequence


PUBLIC_ROOT_FILES = frozenset(
    {
        ".gitignore",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "pyproject.toml",
    }
)

PUBLIC_DOCS = frozenset(
    {
        "docs/ARCHITECTURE.md",
        "docs/CAPABILITIES.md",
        "docs/DOCUMENT-AND-MEDIA.md",
        "docs/EVIDENCE-AND-STORAGE.md",
    }
)

PUBLIC_ROOT_DIRECTORIES = frozenset(
    {".github", "config", "data", "experiments", "scripts", "src", "tests"}
)

PUBLIC_EXPERIMENT_FILES = frozenset(
    {
        "experiments/__init__.py",
        "experiments/steward_exoskeleton/__init__.py",
        "experiments/steward_exoskeleton/r4d_r3d_plugin_candidate.py",
    }
)

PUBLIC_EXPERIMENT_PREFIXES = (
    "experiments/steward_exoskeleton/r4d_r3d_plugin_source/",
)

EXCLUDED_FILES = frozenset(
    {
        "tests/test_audio_diarization.py",
        "tests/test_audio_reliability_evaluation.py",
        "tests/test_cross_session_plugin_identity.py",
        "tests/test_exoskeleton_r4_contract.py",
        "tests/test_file_agent_b1b_matrix.py",
        "tests/test_file_agent_b1c_hybrid.py",
        "tests/test_file_agent_evaluator_readjudication.py",
        "tests/test_file_agent_r2a_audit.py",
        "tests/test_file_agent_r2b_evidence_replay.py",
        "tests/test_file_agent_structured_document_acceptance_harness.py",
        "tests/test_file_agent_structured_document_acceptance_launcher.py",
        "tests/test_file_agent_structured_document_r4_readjudication.py",
        "tests/test_file_agent_temporal_evidence_acceptance_harness.py",
        "tests/test_file_agent_temporal_evidence_r2_readjudication.py",
        "tests/test_file_agent_v01_recovery_harness.py",
        "tests/test_file_agent_v1_acceptance_harness.py",
        "tests/test_llm_context_sandbox_execution_evidence.py",
        "tests/test_llm_context_sandbox_infrastructure.py",
        "tests/test_llm_context_sandbox_validation_recording.py",
        "tests/test_llm_request_contract_integration.py",
        "tests/test_next023_audio_reliability_acceptance.py",
        "tests/test_next024_video_acceptance.py",
        "tests/test_next025_video_precision_acceptance.py",
        "tests/test_next027_video_semantic_acceptance.py",
        "tests/test_r4d_r3e_acceptance.py",
        "tests/test_snapshot_acquisition_formal_harness.py",
        "tests/test_snapshot_refresh_formal_harness.py",
        "tests/test_steward_exoskeleton_plugin_candidate.py",
        "tests/test_steward_exoskeleton_r2_smoke.py",
        "tests/test_steward_exoskeleton_r3_evaluation.py",
        "tests/test_steward_exoskeleton_r4a.py",
        "tests/test_steward_exoskeleton_r4b.py",
        "tests/test_steward_exoskeleton_r4c.py",
        "tests/test_steward_r4d_plugin_candidate.py",
        "tests/test_steward_context_skill_source.py",
        "tests/test_steward_core_skill_source.py",
        "tests/test_steward_skill_source.py",
        "tests/test_verified_snapshot_acceptance_harness.py",
        "tests/test_video_understanding_evaluation.py",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_paths(repository_root: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repository_root,
        check=True,
        capture_output=True,
    )
    return tuple(
        sorted(
            item.decode("utf-8")
            for item in completed.stdout.split(b"\0")
            if item
        )
    )


def is_public_path(relative_path: str) -> bool:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    if relative_path in PUBLIC_ROOT_FILES or relative_path in PUBLIC_DOCS:
        return True
    if relative_path in EXCLUDED_FILES:
        return False
    root = path.parts[0]
    if root not in PUBLIC_ROOT_DIRECTORIES or root == "docs":
        return False
    if root == "experiments":
        return relative_path in PUBLIC_EXPERIMENT_FILES or relative_path.startswith(
            PUBLIC_EXPERIMENT_PREFIXES
        )
    if root == "data":
        return path.name == ".gitkeep"
    return "__pycache__" not in path.parts and not path.name.endswith((".pyc", ".pyo"))


def _validate_destination(repository_root: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise ValueError("public destination must not be a symlink")
    resolved_repository = repository_root.resolve(strict=True)
    resolved_destination = destination.resolve(strict=False)
    if resolved_destination == resolved_repository or resolved_repository in resolved_destination.parents:
        raise ValueError("public destination must be outside the private repository")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("public destination must be absent or an empty directory")


def export_public_release(repository_root: Path, destination: Path) -> dict[str, object]:
    repository_root = repository_root.resolve(strict=True)
    _validate_destination(repository_root, destination)
    destination.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    for relative_path in _candidate_paths(repository_root):
        if not is_public_path(relative_path):
            continue
        source = repository_root / relative_path
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"public source must be a regular file: {relative_path}")
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        records.append(
            {
                "path": relative_path,
                "bytes": target.stat().st_size,
                "sha256": _sha256(target),
            }
        )
    payload: dict[str, object] = {
        "schema_name": "steward.public_release_manifest",
        "schema_version": 1,
        "file_count": len(records),
        "files": records,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload["collection_sha256"] = hashlib.sha256(canonical).hexdigest()
    (destination / "PUBLIC_RELEASE_MANIFEST.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    payload = export_public_release(options.repository_root, options.destination)
    print(
        json.dumps(
            {
                "file_count": payload["file_count"],
                "collection_sha256": payload["collection_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
