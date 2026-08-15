"""Focused acceptance for the plugin-bundled Codex workspace observer."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from local_steward.codex_hooks import (
    HANDOFF_SCHEMA_NAME,
    HANDOFF_SCHEMA_VERSION,
    HOOK_IDENTITY,
    RECEIPT_SCHEMA_NAME,
    RECEIPT_SCHEMA_VERSION,
    handle_hook,
    hook_runtime_identity,
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def _repository(root: Path, *, name: str = "workspace") -> Path:
    repository = root / name
    repository.mkdir(parents=True)
    (repository / "main.py").write_text("print('before')\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".cache/\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "observer@example.invalid")
    _git(repository, "config", "user.name", "Observer")
    _git(repository, "add", ".gitignore", "main.py")
    _git(repository, "commit", "-qm", "initial")
    return repository


def _payload(
    repository: Path,
    event: str,
    *,
    session_id: str = "session-private-value",
    turn_id: str = "turn-private-value",
    **values: object,
) -> dict[str, object]:
    result: dict[str, object] = {
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": str(repository),
        "hook_event_name": event,
        "permission_mode": "default",
    }
    result.update(values)
    return result


def _receipt_files(state_root: Path) -> list[Path]:
    return sorted((state_root / "receipts").glob("*.json"))


def test_observer_binds_current_workspace_and_delivers_one_sanitized_receipt(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    state_root = tmp_path / "plugin-data"

    started = handle_hook(
        _payload(repository, "SessionStart", source="startup"),
        state_root,
    )
    assert started is not None
    context = started["hookSpecificOutput"]["additionalContext"]
    assert f"{HOOK_IDENTITY}_ACTIVE" in context
    assert "plugin steward-exoskeleton 0.33.0" in context
    assert "Skill steward-codex" in context
    assert "native STEWARD_CODEX_NATIVE_V27" in context
    assert "server 24" in context
    assert "hook generation" in context
    assert "do not call steward_code_execution" in context
    assert str(repository) not in context

    preflight = _payload(
        repository,
        "PreToolUse",
        tool_name="apply_patch",
        tool_use_id="tool-private-value",
        tool_input={"command": "SECRET_PATCH_BODY"},
    )
    assert handle_hook(preflight, state_root) is None
    (repository / "main.py").write_text("print('after')\n", encoding="utf-8")
    postflight = _payload(
        repository,
        "PostToolUse",
        tool_name="apply_patch",
        tool_use_id="tool-private-value",
        tool_input={"command": "SECRET_PATCH_BODY"},
        tool_response={"output": "SECRET_TOOL_OUTPUT"},
    )
    assert handle_hook(postflight, state_root) is None

    delivery = handle_hook(
        _payload(
            repository,
            "Stop",
            stop_hook_active=False,
            last_assistant_message="finished",
        ),
        state_root,
    )
    assert delivery is not None
    assert delivery["decision"] == "block"
    assert HOOK_IDENTITY in delivery["reason"]
    assert "main.py" in delivery["reason"]
    assert "Do not run additional tools" in delivery["reason"]

    receipts = _receipt_files(state_root)
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert receipt["schema_name"] == RECEIPT_SCHEMA_NAME
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert receipt["hook_runtime_identity"] == hook_runtime_identity()
    assert receipt["status"] == "CHANGED"
    assert receipt["workspaces"][0]["changed_paths"] == ["main.py"]
    assert receipt["boundary"]["command_text_persisted"] is False
    assert receipt["boundary"]["tool_output_persisted"] is False
    for private_value in (
        str(repository),
        "session-private-value",
        "turn-private-value",
        "tool-private-value",
        "SECRET_PATCH_BODY",
        "SECRET_TOOL_OUTPUT",
    ):
        assert private_value not in encoded

    handoffs = sorted((state_root / "handoffs").glob("*.json"))
    assert len(handoffs) == 1
    handoff = json.loads(handoffs[0].read_text(encoding="utf-8"))
    assert handoff["schema_name"] == HANDOFF_SCHEMA_NAME
    assert handoff["schema_version"] == HANDOFF_SCHEMA_VERSION
    assert handoff["hook_runtime_identity"] == hook_runtime_identity()
    assert handoff["receipt_id"] == receipt["receipt_id"]

    completed = handle_hook(
        _payload(
            repository,
            "Stop",
            stop_hook_active=True,
            last_assistant_message="receipt included",
        ),
        state_root,
    )
    assert completed == {}
    assert not list((state_root / "turns").glob("*.json"))


def test_observer_records_validation_result_without_retaining_command_or_output(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    state_root = tmp_path / "plugin-data"
    command = "pytest -q --api-token SUPER_SECRET"
    preflight = _payload(
        repository,
        "PreToolUse",
        tool_name="Bash",
        tool_use_id="validation-tool",
        tool_input={"command": command, "workdir": str(repository)},
    )
    postflight = _payload(
        repository,
        "PostToolUse",
        tool_name="Bash",
        tool_use_id="validation-tool",
        tool_input={"command": command, "workdir": str(repository)},
        tool_response={"exit_code": 0, "output": "SUPER_SECRET_OUTPUT"},
    )
    assert handle_hook(preflight, state_root) is None
    assert handle_hook(postflight, state_root) is None
    assert (
        handle_hook(
            _payload(repository, "Stop", stop_hook_active=False),
            state_root,
        )
        == {}
    )

    receipt = json.loads(_receipt_files(state_root)[0].read_text(encoding="utf-8"))
    checks = receipt["operations"]["validation_checks"]
    assert checks[0]["check_id"] == "pytest"
    assert checks[0]["status"] == "PASS"
    assert checks[0]["exit_code"] == 0
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert command not in encoded
    assert "SUPER_SECRET" not in encoded
    assert str(repository) not in encoded


def test_session_start_reports_current_then_stale_handoff(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    state_root = tmp_path / "plugin-data"
    preflight = _payload(
        repository,
        "PreToolUse",
        tool_name="apply_patch",
        tool_use_id="edit",
        tool_input={"command": "bounded"},
    )
    handle_hook(preflight, state_root)
    (repository / "main.py").write_text("print('changed')\n", encoding="utf-8")
    handle_hook(
        _payload(
            repository,
            "PostToolUse",
            tool_name="apply_patch",
            tool_use_id="edit",
            tool_input={"command": "bounded"},
            tool_response={"output": "ok"},
        ),
        state_root,
    )
    handle_hook(_payload(repository, "Stop", stop_hook_active=False), state_root)

    current = handle_hook(
        _payload(
            repository,
            "SessionStart",
            session_id="next-session",
            turn_id="next-turn",
            source="startup",
        ),
        state_root,
    )
    assert current is not None
    assert "is CURRENT" in current["hookSpecificOutput"]["additionalContext"]

    (repository / "main.py").write_text("print('new drift')\n", encoding="utf-8")
    stale = handle_hook(
        _payload(
            repository,
            "SessionStart",
            session_id="third-session",
            turn_id="third-turn",
            source="startup",
        ),
        state_root,
    )
    assert stale is not None
    assert "is STALE_WORKSPACE_STATE" in stale["hookSpecificOutput"]["additionalContext"]


def test_session_start_rejects_a_handoff_from_an_old_hook_generation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    state_root = tmp_path / "plugin-data"
    handle_hook(
        _payload(
            repository,
            "PreToolUse",
            tool_name="apply_patch",
            tool_use_id="edit",
            tool_input={"command": "bounded"},
        ),
        state_root,
    )
    (repository / "main.py").write_text("print('changed')\n", encoding="utf-8")
    handle_hook(
        _payload(
            repository,
            "PostToolUse",
            tool_name="apply_patch",
            tool_use_id="edit",
            tool_input={"command": "bounded"},
            tool_response={"output": "ok"},
        ),
        state_root,
    )
    handle_hook(_payload(repository, "Stop", stop_hook_active=False), state_root)

    handoff_path = next((state_root / "handoffs").glob("*.json"))
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["hook_runtime_identity"]["hook_generation_sha256"] = "0" * 64
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")

    started = handle_hook(
        _payload(
            repository,
            "SessionStart",
            session_id="next-session",
            turn_id="next-turn",
            source="startup",
        ),
        state_root,
    )
    assert started is not None
    context = started["hookSpecificOutput"]["additionalContext"]
    assert "STALE_HOOK_GENERATION" in context
    assert "CURRENT" not in context


def test_duplicate_post_tool_use_updates_one_unified_exec_event(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    state_root = tmp_path / "plugin-data"
    command = "pytest -q"
    pre = _payload(
        repository,
        "PreToolUse",
        tool_name="Bash",
        tool_use_id="unified-exec",
        tool_input={"command": command, "workdir": str(repository)},
    )
    post = _payload(
        repository,
        "PostToolUse",
        tool_name="Bash",
        tool_use_id="unified-exec",
        tool_input={"command": command, "workdir": str(repository)},
        tool_response={"exit_code": 0, "output": "bounded"},
    )
    handle_hook(pre, state_root)
    handle_hook(post, state_root)
    handle_hook(post, state_root)
    handle_hook(_payload(repository, "Stop", stop_hook_active=False), state_root)

    receipt = json.loads(_receipt_files(state_root)[0].read_text(encoding="utf-8"))
    assert receipt["operations"]["observed_count"] == 1
    assert receipt["operations"]["tool_counts"] == {"Bash": 1}
    assert len(receipt["operations"]["validation_checks"]) == 1


def test_observer_uses_distinct_host_bound_workspace_identities(tmp_path: Path) -> None:
    first = _repository(tmp_path, name="first")
    second = _repository(tmp_path, name="second")
    state_root = tmp_path / "plugin-data"
    first_started = handle_hook(_payload(first, "SessionStart", source="startup"), state_root)
    second_started = handle_hook(
        _payload(
            second,
            "SessionStart",
            session_id="second-session",
            turn_id="second-turn",
            source="startup",
        ),
        state_root,
    )
    assert first_started is not None and second_started is not None
    first_context = first_started["hookSpecificOutput"]["additionalContext"]
    second_context = second_started["hookSpecificOutput"]["additionalContext"]
    assert "current Git workspace first" in first_context
    assert "current Git workspace second" in second_context
    assert first_context != second_context
    assert str(first) not in first_context
    assert str(second) not in second_context


def test_non_git_hook_input_fails_open_without_claiming_a_binding(tmp_path: Path) -> None:
    state_root = tmp_path / "plugin-data"
    directory = tmp_path / "plain"
    directory.mkdir()
    result = handle_hook(
        _payload(directory, "SessionStart", source="startup"),
        state_root,
    )
    assert result is None
