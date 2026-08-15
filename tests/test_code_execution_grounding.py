"""Synthetic acceptance for the Codex-owned code-execution grounding loop."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess

import pytest

from local_steward.code_execution import (
    CODE_EXECUTION_PACKET_SCHEMA_NAME,
    build_code_execution_packet,
)
from local_steward.errors import CodeExecutionBaselineError, CodeExecutionError
from local_steward.agent_session import create_steward_session
from local_steward.native_mcp_server import NativeStewardDispatcher, create_codex_host_policy
from local_steward.native_mcp_server.protocol import CODE_TOOL

from .test_protocol_completion import prepared_config


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def _fixture(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = prepared_config(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('before')\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.generated\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    _git(tmp_path, "config", "user.name", "Synthetic")
    _git(tmp_path, "add", ".gitignore", "config", "src", "tests")
    _git(tmp_path, "commit", "-qm", "initial")
    return config


def test_preflight_is_bounded_deterministic_and_path_safe(tmp_path: Path) -> None:
    config = _fixture(tmp_path)
    first = build_code_execution_packet(config, phase="PREFLIGHT", target_paths=["src"])
    second = build_code_execution_packet(config, phase="PREFLIGHT", target_paths=["src"])

    assert first == second
    assert first["schema_name"] == CODE_EXECUTION_PACKET_SCHEMA_NAME
    assert first["packet_status"] == "READY"
    assert first["scope"] == {
        "root_id": "PROJECT_ROOT",
        "target_paths": ["src"],
        "target_policy": "EXACT_RELATIVE_TARGETS",
    }
    assert first["workspace"]["identity"]["root_id"] == "PROJECT_ROOT"  # type: ignore[index]
    assert first["baseline"]["baseline_digest"]  # type: ignore[index]
    assert str(tmp_path) not in json.dumps(first, ensure_ascii=False)
    assert first["delivery"]["codex_remains_execution_owner"] is True  # type: ignore[index]


def test_preflight_defaults_to_project_root_and_publishes_readable_receipt(
    tmp_path: Path,
) -> None:
    config = _fixture(tmp_path)
    packet = build_code_execution_packet(
        config,
        phase="PREFLIGHT",
        validation_claims=[{"check_id": "pytest", "status": "NOT_RUN"}],
    )
    assert packet["scope"] == {
        "root_id": "PROJECT_ROOT",
        "target_paths": [],
        "target_policy": "PROJECT_ROOT",
    }
    receipt = packet["execution_receipt"]
    assert receipt["phase"] == "PREFLIGHT"
    assert receipt["packet_status"] == "READY"
    assert receipt["observed"]["target_policy"] == "PROJECT_ROOT"
    assert receipt["unverified_checks"][0]["verification"] == "NOT_VERIFIED"
    assert receipt["boundary"]["steward_executed_commands"] is False
    assert packet["change_review"]["status"] == "NOT_APPLICABLE"


def test_postflight_classifies_changes_scope_drift_and_caller_claims(tmp_path: Path) -> None:
    config = _fixture(tmp_path)
    preflight = build_code_execution_packet(config, phase="PREFLIGHT", target_paths=["src"])
    (tmp_path / "src" / "main.py").write_text("print('after')\n", encoding="utf-8")
    (tmp_path / "tests" / "test_new.py").write_text("def test_new(): pass\n", encoding="utf-8")
    postflight = build_code_execution_packet(
        config,
        phase="POSTFLIGHT",
        baseline=preflight["baseline"],
        validation_claims=[{"check_id": "pytest", "status": "PASS", "exit_code": 0}],
    )

    assert postflight["packet_status"] == "SCOPE_DRIFT"
    changes = postflight["changes"]
    assert changes["status"] == "SCOPE_DRIFT"  # type: ignore[index]
    assert any(item["path"] == "src/main.py" for item in changes["changed_paths"])  # type: ignore[index]
    assert any(item["path"] == "tests/test_new.py" for item in changes["unexpected_paths"])  # type: ignore[index]
    claim = postflight["reported_checks"][0]  # type: ignore[index]
    assert claim["evidence_class"] == "CALLER_REPORTED"
    assert claim["verification"] == "NOT_VERIFIED"
    assert postflight["delivery"]["caller_reported_is_not_verified"] is True  # type: ignore[index]
    assert postflight["unverified_checks"] == postflight["reported_checks"]
    assert postflight["execution_receipt"]["observed"]["change_status"] == "SCOPE_DRIFT"


def test_postflight_reports_risk_and_ignored_artifact_changes(tmp_path: Path) -> None:
    config = _fixture(tmp_path)
    preflight = build_code_execution_packet(config, phase="PREFLIGHT")
    (tmp_path / "pyproject.toml").write_text("[tool.synthetic]\n", encoding="utf-8")
    (tmp_path / "src" / "result.generated").write_text("generated\n", encoding="utf-8")
    postflight = build_code_execution_packet(
        config,
        phase="POSTFLIGHT",
        baseline=preflight["baseline"],
    )
    review = postflight["change_review"]
    assert review["status"] == "REVIEW_REQUIRED"
    assert review["risk_level"] == "HIGH"
    assert review["review_required"] is True
    assert "DEPENDENCY" in review["category_counts"]
    assert "CONFIGURATION" in review["category_counts"]
    assert "src/result.generated" in review["artifact_changes"]
    assert "src/result.generated" in postflight["changes"]["ignored_artifact_changes"]
    assert postflight["execution_receipt"]["observed"]["ignored_artifact_change_count"] == 1


def test_postflight_rejects_tampered_or_mismatched_baseline(tmp_path: Path) -> None:
    config = _fixture(tmp_path)
    preflight = build_code_execution_packet(config, phase="PREFLIGHT")
    baseline = copy.deepcopy(preflight["baseline"])
    baseline["state"]["identity"]["head"] = "tampered"
    with pytest.raises(CodeExecutionBaselineError):
        build_code_execution_packet(config, phase="POSTFLIGHT", baseline=baseline)
    with pytest.raises(CodeExecutionBaselineError):
        build_code_execution_packet(config, phase="POSTFLIGHT", baseline=preflight["baseline"], target_paths=["src"])
    wrong_project = copy.deepcopy(preflight["baseline"])
    wrong_project["project_name"] = "Other project"
    wrong_project["baseline_digest"] = "0" * 64
    with pytest.raises(CodeExecutionBaselineError):
        build_code_execution_packet(config, phase="POSTFLIGHT", baseline=wrong_project)
    with pytest.raises(CodeExecutionBaselineError):
        build_code_execution_packet(config, phase="PREFLIGHT", baseline=preflight["baseline"])


def test_protected_sidecar_change_fails_closed_without_reading_its_payload(tmp_path: Path) -> None:
    config = _fixture(tmp_path)
    preflight = build_code_execution_packet(config, phase="PREFLIGHT", target_paths=["src"])
    sidecar = config.paths.data_dir / "state.db-wal"
    sidecar.write_bytes(b"synthetic-sidecar")
    try:
        postflight = build_code_execution_packet(
            config, phase="POSTFLIGHT", baseline=preflight["baseline"]
        )
    finally:
        sidecar.unlink()
    assert postflight["packet_status"] == "PROTECTED_CHANGE"
    assert postflight["changes"]["sidecar_changes"] == ["state.db-wal"]  # type: ignore[index]
    assert postflight["changes"]["protected_paths"][0]["area"] == "data"  # type: ignore[index]


def test_code_targets_reject_absolute_traversal_and_protected_paths(tmp_path: Path) -> None:
    config = _fixture(tmp_path)
    for target in (str(tmp_path / "src"), "../outside", "data"):
        with pytest.raises(CodeExecutionError):
            build_code_execution_packet(config, phase="PREFLIGHT", target_paths=[target])


@pytest.mark.anyio
async def test_native_code_tool_publishes_read_only_packet_and_risk(tmp_path: Path) -> None:
    config = _fixture(tmp_path)
    dispatcher = NativeStewardDispatcher(
        create_steward_session(config), create_codex_host_policy()
    )
    result = await dispatcher.dispatch(
        CODE_TOOL,
        {"phase": "PREFLIGHT", "target_paths": ["src"]},
        request_meta={"openai/session": "codex-thread-006"},
    )
    assert result.isError is False
    assert result.structuredContent["risk_class"] == "CODE_WORKSPACE_READ"
    packet = result.structuredContent["result"]["code_execution_packet"]
    assert packet["packet_kind"] == "CODE_EXECUTION"
    assert packet["packet_status"] == "READY"
    assert packet["execution_receipt"]["receipt_kind"] == "CODE_EXECUTION"
    assert result.structuredContent["thread_attribution"]["status"] == "HOST_BOUND"
    assert (
        packet["execution_receipt"]["thread_attribution"]
        == result.structuredContent["thread_attribution"]
    )
    assert packet["change_review"]["status"] == "NOT_APPLICABLE"
    assert result.structuredContent["authority"]["tool_approval_mode"] == "approve"


@pytest.mark.anyio
async def test_native_code_tool_rejects_model_supplied_thread_identity(tmp_path: Path) -> None:
    config = _fixture(tmp_path)
    dispatcher = NativeStewardDispatcher(
        create_steward_session(config), create_codex_host_policy()
    )
    result = await dispatcher.dispatch(
        CODE_TOOL,
        {"phase": "PREFLIGHT", "thread_id": "model-invented"},
        request_meta={"openai/session": "host-thread"},
    )

    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "STEWARD_NATIVE_ARGUMENT_INVALID"
    assert result.structuredContent["thread_attribution"]["thread_reference"] == "host-thread"
