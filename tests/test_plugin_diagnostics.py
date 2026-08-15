import json
from pathlib import Path

from local_steward.codex_hooks import (
    HANDOFF_SCHEMA_NAME,
    HANDOFF_SCHEMA_VERSION,
    RECEIPT_SCHEMA_NAME,
    RECEIPT_SCHEMA_VERSION,
    hook_runtime_identity,
)
from local_steward.plugin_diagnostics import collect_plugin_diagnostic
from local_steward.codex_identity import PLUGIN_BASE_VERSION


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _plugin(root: Path) -> None:
    runtime = hook_runtime_identity()
    implementation = runtime["hook_implementation_sha256"]
    _write_json(
        root / ".codex-plugin" / "plugin.json",
        {
            "name": "steward-exoskeleton",
            "version": f"{PLUGIN_BASE_VERSION}+codex.test",
            "skills": "./skills/",
            "mcpServers": "./.mcp.json",
        },
    )
    _write_json(
        root / ".mcp.json",
        {
            "mcpServers": {
                "local-steward-native": {
                    "command": "python",
                    "env": {
                        "LOCAL_STEWARD_NATIVE_CONFIG": str(root / "config.toml"),
                        "LOCAL_STEWARD_NATIVE_HOST_POLICY": str(root / "policy.json"),
                    },
                }
            }
        },
    )
    (root / "skills" / "steward-codex").mkdir(parents=True)
    (root / "skills" / "steward-codex" / "SKILL.md").write_text(
        "# steward-codex\n", encoding="utf-8"
    )
    command = f"python -m local_steward.codex_hooks --implementation-sha256 {implementation}"
    _write_json(
        root / "hooks" / "hooks.json",
        {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": command}]}]
                for event in (
                    "SessionStart",
                    "PreToolUse",
                    "PostToolUse",
                    "Stop",
                    "SessionEnd",
                )
            }
        },
    )


def test_diagnostic_is_path_safe_and_classifies_current_and_legacy_state(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private-source-name"
    cache = tmp_path / "private-cache-name"
    state = tmp_path / "private-state-name"
    _plugin(source)
    _plugin(cache)
    runtime = hook_runtime_identity()
    _write_json(
        state / "receipts" / "current.json",
        {
            "schema_name": RECEIPT_SCHEMA_NAME,
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "hook_runtime_identity": runtime,
        },
    )
    _write_json(
        state / "receipts" / "legacy.json",
        {"schema_name": RECEIPT_SCHEMA_NAME, "schema_version": 1},
    )
    _write_json(
        state / "handoffs" / "stale.json",
        {
            "schema_name": HANDOFF_SCHEMA_NAME,
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "hook_runtime_identity": {
                **runtime,
                "hook_generation_sha256": "0" * 64,
            },
        },
    )
    (state / "locks").mkdir()
    (state / "locks" / "legacy.lock").write_bytes(b"")

    result = collect_plugin_diagnostic(source, cache, state)

    assert result["status"] == "HEALTHY"
    assert result["source_cache"]["equal"] is True
    assert result["hooks"]["persisted_trust"] == "NOT_INFERRED_FROM_FILES"
    assert result["state"]["receipts"]["counts"] == {
        "current": 1,
        "other_generation": 0,
        "legacy": 1,
        "unreadable": 0,
    }
    assert result["state"]["handoffs"]["counts"]["other_generation"] == 1
    assert result["state"]["legacy_lock_residue_present"] is True
    serialized = json.dumps(result, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "private-source-name" not in serialized


def test_diagnostic_reports_source_cache_and_hook_identity_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    cache = tmp_path / "cache"
    _plugin(source)
    _plugin(cache)
    hooks_path = cache / "hooks" / "hooks.json"
    hooks_path.write_text("{}", encoding="utf-8")

    result = collect_plugin_diagnostic(source, cache)

    assert result["status"] == "INCONSISTENT"
    assert "SOURCE_CACHE_MISMATCH" in result["issues"]
    assert "HOOK_SOURCE_CACHE_MISMATCH" in result["issues"]
    assert result["state"] == {"status": "NOT_CHECKED"}
