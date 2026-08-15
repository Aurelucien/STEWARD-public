"""Read-only, path-safe diagnostics for the installed STEWARD Codex plugin."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
from typing import Any

from .codex_hooks import (
    HANDOFF_SCHEMA_NAME,
    HANDOFF_SCHEMA_VERSION,
    RECEIPT_SCHEMA_NAME,
    RECEIPT_SCHEMA_VERSION,
    hook_runtime_identity,
)
from .codex_identity import (
    MCP_SERVER_NAME,
    PLUGIN_BASE_VERSION,
    PLUGIN_NAME,
    SKILL_NAME,
    integration_identity_machine_object,
)


DIAGNOSTIC_SCHEMA_NAME = "local_steward.codex_plugin_diagnostic"
DIAGNOSTIC_SCHEMA_VERSION = 1
MAX_DIAGNOSTIC_FILE_BYTES = 1024 * 1024
HOOK_EVENTS = frozenset(
    {"SessionStart", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"}
)

JsonObject = dict[str, Any]


class PluginDiagnosticError(Exception):
    """A plugin diagnostic input is unavailable or structurally unsafe."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_regular_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PluginDiagnosticError("required plugin file is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PluginDiagnosticError("plugin diagnostic refuses non-regular files")
    if metadata.st_size > MAX_DIAGNOSTIC_FILE_BYTES:
        raise PluginDiagnosticError("plugin diagnostic file exceeds the read bound")
    try:
        return path.read_bytes()
    except OSError as error:
        raise PluginDiagnosticError("required plugin file is unreadable") from error


def _json_object(path: Path) -> JsonObject:
    try:
        value = json.loads(_read_regular_file(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PluginDiagnosticError("required plugin JSON is invalid") from error
    if not isinstance(value, dict):
        raise PluginDiagnosticError("required plugin JSON is not an object")
    return value


def _safe_directory(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PluginDiagnosticError("plugin diagnostic directory is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PluginDiagnosticError("plugin diagnostic refuses non-directories")
    return path


def _normalized_plugin_payload(relative: str, payload: bytes) -> bytes:
    if relative != ".mcp.json":
        return payload
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload
    if not isinstance(value, dict):
        return payload
    servers = value.get("mcpServers")
    if isinstance(servers, dict):
        for server in servers.values():
            if not isinstance(server, dict):
                continue
            environment = server.get("env")
            if not isinstance(environment, dict):
                continue
            for key in (
                "LOCAL_STEWARD_NATIVE_CONFIG",
                "LOCAL_STEWARD_NATIVE_HOST_POLICY",
            ):
                if key in environment:
                    environment[key] = "<BOUND>"
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _tree_digest(root: Path) -> tuple[str, int]:
    root = _safe_directory(root)
    records: list[bytes] = []
    file_count = 0
    try:
        paths = sorted(root.rglob("*"))
    except OSError as error:
        raise PluginDiagnosticError("plugin tree is unreadable") from error
    for path in paths:
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts:
            continue
        try:
            metadata = path.lstat()
        except OSError as error:
            raise PluginDiagnosticError("plugin tree entry is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise PluginDiagnosticError("plugin tree contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise PluginDiagnosticError("plugin tree contains a non-regular entry")
        payload = _read_regular_file(path)
        normalized = _normalized_plugin_payload(relative.as_posix(), payload)
        records.append(
            relative.as_posix().encode("utf-8")
            + b"\0"
            + _sha256_bytes(normalized).encode("ascii")
        )
        file_count += 1
    return _sha256_bytes(b"\n".join(records)), file_count


def _hook_commands(manifest: JsonObject) -> tuple[frozenset[str], set[str]]:
    hooks = manifest.get("hooks")
    if not isinstance(hooks, dict):
        return frozenset(), set()
    events = frozenset(key for key in hooks if isinstance(key, str))
    commands: set[str] = set()
    for configurations in hooks.values():
        if not isinstance(configurations, list):
            continue
        for configuration in configurations:
            if not isinstance(configuration, dict):
                continue
            handlers = configuration.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if isinstance(handler, dict) and isinstance(handler.get("command"), str):
                    commands.add(handler["command"])
    return events, commands


def _generation_counts(
    root: Path,
    *,
    schema_name: str,
    schema_version: int,
    generation_sha256: str,
) -> JsonObject:
    counts = {"current": 0, "other_generation": 0, "legacy": 0, "unreadable": 0}
    if not root.exists():
        return {"status": "ABSENT", "counts": counts}
    _safe_directory(root)
    for path in sorted(root.glob("*.json")):
        try:
            value = _json_object(path)
        except PluginDiagnosticError:
            counts["unreadable"] += 1
            continue
        if value.get("schema_name") != schema_name or value.get("schema_version") != schema_version:
            counts["legacy"] += 1
            continue
        runtime = value.get("hook_runtime_identity")
        if not isinstance(runtime, dict):
            counts["legacy"] += 1
        elif runtime.get("hook_generation_sha256") == generation_sha256:
            counts["current"] += 1
        else:
            counts["other_generation"] += 1
    status = "READABLE" if counts["unreadable"] == 0 else "PARTIAL"
    return {"status": status, "counts": counts}


def collect_plugin_diagnostic(
    plugin_root: Path,
    cache_root: Path,
    state_root: Path | None = None,
) -> JsonObject:
    """Inspect plugin artifacts without publishing their host paths or inferring trust."""

    source_digest, source_files = _tree_digest(plugin_root)
    cache_digest, cache_files = _tree_digest(cache_root)
    source_manifest = _json_object(plugin_root / ".codex-plugin" / "plugin.json")
    cache_manifest = _json_object(cache_root / ".codex-plugin" / "plugin.json")
    source_hooks_bytes = _read_regular_file(plugin_root / "hooks" / "hooks.json")
    cache_hooks_bytes = _read_regular_file(cache_root / "hooks" / "hooks.json")
    try:
        hooks_value = json.loads(source_hooks_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PluginDiagnosticError("hook manifest is invalid") from error
    if not isinstance(hooks_value, dict):
        raise PluginDiagnosticError("hook manifest is not an object")
    hook_events, hook_commands = _hook_commands(hooks_value)
    runtime = hook_runtime_identity()
    implementation_sha256 = runtime["hook_implementation_sha256"]
    generation_sha256 = runtime["hook_generation_sha256"]
    command_identity_matches = bool(hook_commands) and all(
        f"--implementation-sha256 {implementation_sha256}" in command
        for command in hook_commands
    )
    version = source_manifest.get("version")
    identity_matches = (
        source_manifest.get("name") == PLUGIN_NAME
        and isinstance(version, str)
        and (version == PLUGIN_BASE_VERSION or version.startswith(f"{PLUGIN_BASE_VERSION}+"))
        and cache_manifest == source_manifest
        and (plugin_root / "skills" / SKILL_NAME / "SKILL.md").is_file()
        and (cache_root / "skills" / SKILL_NAME / "SKILL.md").is_file()
        and MCP_SERVER_NAME in _json_object(plugin_root / ".mcp.json").get("mcpServers", {})
    )
    issues: list[str] = []
    if source_digest != cache_digest or source_files != cache_files:
        issues.append("SOURCE_CACHE_MISMATCH")
    if not identity_matches:
        issues.append("PLUGIN_IDENTITY_MISMATCH")
    if source_hooks_bytes != cache_hooks_bytes:
        issues.append("HOOK_SOURCE_CACHE_MISMATCH")
    if hook_events != HOOK_EVENTS:
        issues.append("HOOK_EVENT_SET_MISMATCH")
    if len(hook_commands) != 1 or not command_identity_matches:
        issues.append("HOOK_IMPLEMENTATION_IDENTITY_MISMATCH")

    state: JsonObject = {"status": "NOT_CHECKED"}
    if state_root is not None:
        _safe_directory(state_root)
        receipts = _generation_counts(
            state_root / "receipts",
            schema_name=RECEIPT_SCHEMA_NAME,
            schema_version=RECEIPT_SCHEMA_VERSION,
            generation_sha256=str(generation_sha256),
        )
        handoffs = _generation_counts(
            state_root / "handoffs",
            schema_name=HANDOFF_SCHEMA_NAME,
            schema_version=HANDOFF_SCHEMA_VERSION,
            generation_sha256=str(generation_sha256),
        )
        locks_root = state_root / "locks"
        lock_count = 0
        if locks_root.exists():
            _safe_directory(locks_root)
            lock_count = sum(1 for path in locks_root.iterdir() if path.is_file())
        state = {
            "status": "READABLE",
            "receipts": receipts,
            "handoffs": handoffs,
            "lock_file_count": lock_count,
            "legacy_lock_residue_present": lock_count > 0,
        }
        if receipts["status"] == "PARTIAL" or handoffs["status"] == "PARTIAL":
            issues.append("HOOK_STATE_PARTIAL")

    return {
        "schema_name": DIAGNOSTIC_SCHEMA_NAME,
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "status": "HEALTHY" if not issues else "INCONSISTENT",
        "issues": issues,
        "identity": {
            "expected": integration_identity_machine_object(),
            "installed_version": version,
            "matches": identity_matches,
        },
        "source_cache": {
            "equal": source_digest == cache_digest and source_files == cache_files,
            "source_tree_sha256": source_digest,
            "cache_tree_sha256": cache_digest,
            "source_file_count": source_files,
            "cache_file_count": cache_files,
        },
        "hooks": {
            "source_cache_equal": source_hooks_bytes == cache_hooks_bytes,
            "manifest_sha256": _sha256_bytes(source_hooks_bytes),
            "events": sorted(hook_events),
            "single_command_definition": len(hook_commands) == 1,
            "implementation_identity_matches": command_identity_matches,
            "hook_runtime_identity": runtime,
            "persisted_trust": "NOT_INFERRED_FROM_FILES",
        },
        "state": state,
        "boundary": {
            "read_only": True,
            "host_paths_published": False,
            "prompt_command_output_content_read": False,
            "trust_requires_host_acceptance": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path)
    arguments = parser.parse_args()
    try:
        result = collect_plugin_diagnostic(
            arguments.plugin_root,
            arguments.cache_root,
            arguments.state_root,
        )
    except PluginDiagnosticError as error:
        result = {
            "schema_name": DIAGNOSTIC_SCHEMA_NAME,
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "status": "UNAVAILABLE",
            "reason": str(error),
            "boundary": {"host_paths_published": False},
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    raise SystemExit(0 if result["status"] == "HEALTHY" else 2)


if __name__ == "__main__":
    main()
