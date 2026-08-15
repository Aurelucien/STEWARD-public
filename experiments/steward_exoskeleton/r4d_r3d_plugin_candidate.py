"""Build one isolated R4D-R3D Codex-hosted STEWARD plugin candidate."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_steward.native_mcp_server import (
    create_codex_host_policy,
    host_policy_machine_object,
)
from local_steward.codex_identity import (
    MCP_SERVER_NAME,
    PLUGIN_BASE_VERSION,
    PLUGIN_NAME,
    SKILL_NAME,
)


PLUGIN_ID = f"{PLUGIN_NAME}@personal"
CONFIG_ENVIRONMENT_VARIABLE = "LOCAL_STEWARD_NATIVE_CONFIG"
HOST_POLICY_ENVIRONMENT_VARIABLE = "LOCAL_STEWARD_NATIVE_HOST_POLICY"
PLUGIN_VERSION = PLUGIN_BASE_VERSION


@dataclass(frozen=True)
class R4DR3DPluginCandidate:
    plugin_root: Path
    codex_policy_path: Path
    plugin_version: str
    plugin_manifest_sha256: str
    source_skill_sha256: str
    server_source_sha256: str
    hook_implementation_sha256: str
    hooks_manifest_sha256: str
    host_policy_sha256: str
    codex_policy_sha256: str
    sanitized_mcp_sha256: str
    sanitized_collection_sha256: str

    def safe_descriptor(self) -> dict[str, str]:
        return {
            "plugin_name": PLUGIN_NAME,
            "plugin_version": self.plugin_version,
            "skill_name": SKILL_NAME,
            "mcp_server_name": MCP_SERVER_NAME,
            "plugin_manifest_sha256": self.plugin_manifest_sha256,
            "source_skill_sha256": self.source_skill_sha256,
            "server_source_sha256": self.server_source_sha256,
            "hook_implementation_sha256": self.hook_implementation_sha256,
            "hooks_manifest_sha256": self.hooks_manifest_sha256,
            "host_policy_sha256": self.host_policy_sha256,
            "codex_policy_sha256": self.codex_policy_sha256,
            "sanitized_mcp_sha256": self.sanitized_mcp_sha256,
            "sanitized_collection_sha256": self.sanitized_collection_sha256,
        }


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tree_digest(root: Path, replacements: dict[str, str] | None = None) -> str:
    records: list[bytes] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        if replacements and path.name in {".mcp.json", "hooks.json"}:
            text = payload.decode("utf-8")
            for raw, token in replacements.items():
                text = text.replace(raw, token)
            payload = text.encode("utf-8")
        records.append(relative.encode("utf-8") + b"\0" + _sha256(payload).encode("ascii"))
    return _sha256(b"\n".join(records))


def _require_absolute_file(path: Path, *, executable: bool = False) -> Path:
    if not path.is_absolute() or not path.is_file():
        raise ValueError("binding path must be an absolute regular file")
    if executable and not os.access(path, os.X_OK):
        raise ValueError("runtime binding must be executable")
    return path


def plugin_manifest(plugin_version: str = PLUGIN_VERSION) -> dict[str, Any]:
    return {
        "name": PLUGIN_NAME,
        "version": plugin_version,
        "description": "Natural Codex-hosted STEWARD evidence workflows.",
        "author": {"name": "STEWARD local project"},
        "skills": "./skills/",
        "mcpServers": "./.mcp.json",
        "interface": {
            "displayName": "STEWARD Codex",
            "shortDescription": "Natural governed local evidence workflows.",
            "longDescription": (
                "Adds one naturally routed STEWARD Skill and one unified local service with "
                "Codex-owned tool approval, deterministic object resolution, verified history, "
                "adaptive text, stream-staged large-document evidence, native-located citations "
                "and memory-bounded page-local OCR for scan-heavy PDFs, region-aware visual "
                "document parsing, bounded local speech transcription, "
                "on-demand word alignment and anonymous speaker-turn evidence, bounded local "
                "video scenes, subtitles, frames, OCR and audiovisual evidence, "
                "host-observed cross-workspace receipts "
                "and handoff, and explicit Snapshot lifecycle operations."
            ),
            "developerName": "STEWARD local project",
            "category": "Productivity",
            "capabilities": [
                "Verified historical evidence",
                "Adaptive local text and visual document parsing",
                "Format-native hierarchy-aware document evidence",
                "Snapshot-guided bounded multi-document evidence synthesis",
                "Tolerant stream-staged large-document query mapping",
                "Native Excel formula and structured-reference extraction",
                "Quality-gated local OCR without duplicate whole-document retries",
                "Memory-bounded page-local OCR for scan-heavy PDFs",
                "PDF annotations, form fields, outline and repair diagnostics",
                "Office comments, notes, revisions, accessibility and chart facts",
                "Component-scoped recovery for malformed auxiliary package parts",
                "Decoder-aware bounded raster projection",
                "Bounded local audio probe, VAD and source-pinned ASR timeline evidence",
                "On-demand aligned audio words and anonymous speaker turns",
                "Quality-aware multilingual audio routing and diagnostics",
                "Bounded local video scenes, representative frames and embedded subtitles",
                "Source-distinguishable video ASR, frame OCR and temporal evidence",
                "Whole-source local visual-semantic video candidate retrieval",
                "Code execution grounding",
                "Host-observed workspace handoff",
                "Snapshot lifecycle",
            ],
            "defaultPrompt": "Use STEWARD to complete this governed local evidence workflow.",
        },
    }


def codex_approval_policy_toml() -> str:
    prefix = f'plugins."{PLUGIN_ID}".mcp_servers."local-steward-native"'
    return (
        f"[{prefix}]\n"
        "enabled = true\n"
        'default_tools_approval_mode = "writes"\n\n'
        f"[{prefix}.tools.steward_history]\n"
        'approval_mode = "approve"\n\n'
        f"[{prefix}.tools.steward_read_document]\n"
        'approval_mode = "approve"\n\n'
        f"[{prefix}.tools.steward_code_execution]\n"
        'approval_mode = "approve"\n\n'
        f"[{prefix}.tools.steward_update_snapshot]\n"
        'approval_mode = "prompt"\n\n'
        f"[{prefix}.tools.steward_recover_snapshot_run]\n"
        'approval_mode = "prompt"\n'
    )


def mcp_manifest(
    python_executable: Path, config_path: Path, host_policy_path: Path
) -> dict[str, Any]:
    python_executable = _require_absolute_file(python_executable, executable=True)
    config_path = _require_absolute_file(config_path)
    host_policy_path = _require_absolute_file(host_policy_path)
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": str(python_executable),
                "args": ["-m", "local_steward.native_mcp_server"],
                "env": {
                    CONFIG_ENVIRONMENT_VARIABLE: str(config_path),
                    HOST_POLICY_ENVIRONMENT_VARIABLE: str(host_policy_path),
                },
            }
        }
    }


def hooks_manifest(
    python_executable: Path,
    hook_implementation_sha256: str,
) -> dict[str, Any]:
    """Return the quiet fail-open lifecycle observer declaration."""

    python_executable = _require_absolute_file(python_executable, executable=True)
    if len(hook_implementation_sha256) != 64:
        raise ValueError("hook implementation digest is invalid")
    try:
        int(hook_implementation_sha256, 16)
    except ValueError as error:
        raise ValueError("hook implementation digest is invalid") from error
    command = (
        f"{shlex.quote(str(python_executable))} -m local_steward.codex_hooks "
        ' --state-dir "${PLUGIN_DATA}/steward-host-observer-v1" '
        f"--implementation-sha256 {hook_implementation_sha256}"
    )

    def handler(*, timeout: int, context_limit: int | None = None) -> dict[str, Any]:
        value: dict[str, Any] = {
            "type": "command",
            "command": command,
            "timeout": timeout,
        }
        if context_limit is not None:
            value["additionalContextLimit"] = context_limit
        return value

    return {
        "description": "Quiet host-observed STEWARD execution continuity.",
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [handler(timeout=5, context_limit=700)],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "^(Bash|apply_patch)$",
                    "hooks": [handler(timeout=6)],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "^(Bash|apply_patch)$",
                    "hooks": [handler(timeout=6)],
                }
            ],
            "Stop": [{"hooks": [handler(timeout=8)]}],
            "SessionEnd": [{"hooks": [handler(timeout=3)]}],
        },
    }


def bind_r4d_r3d_plugin_runtime(
    *,
    plugin_root: Path,
    python_executable: Path,
    config_path: Path,
) -> str:
    """Bind a copied candidate to its stable installed plugin root.

    Candidate manifests contain absolute local runtime bindings. Any copy must
    therefore be rebound after it reaches its final path; copying a temporary
    manifest verbatim would leave the installed service dependent on a build
    directory.
    """

    if not plugin_root.is_absolute() or not plugin_root.is_dir() or plugin_root.is_symlink():
        raise ValueError("plugin root must be an absolute regular directory")
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    host_policy_path = plugin_root / ".codex-host-policy.json"
    if not manifest_path.is_file() or not host_policy_path.is_file():
        raise ValueError("plugin identity or host policy is unavailable")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("plugin identity is invalid") from error
    if not isinstance(manifest, dict) or manifest.get("name") != PLUGIN_NAME:
        raise ValueError("plugin identity does not match STEWARD")
    payload = _canonical_bytes(mcp_manifest(python_executable, config_path, host_policy_path))
    (plugin_root / ".mcp.json").write_bytes(payload)
    return _sha256(payload)


def build_r4d_r3d_plugin_candidate(
    *,
    repository_root: Path,
    output_parent: Path,
    python_executable: Path,
    config_path: Path,
    plugin_version: str = PLUGIN_VERSION,
) -> R4DR3DPluginCandidate:
    if not repository_root.is_absolute() or not output_parent.is_absolute():
        raise ValueError("repository and output roots must be absolute")
    if not isinstance(plugin_version, str) or not plugin_version.strip():
        raise ValueError("plugin version must be a non-empty string")
    source_skill = (
        repository_root
        / "experiments"
        / "steward_exoskeleton"
        / "r4d_r3d_plugin_source"
        / "skills"
        / SKILL_NAME
    )
    server_source = repository_root / "src" / "local_steward" / "native_mcp_server"
    hook_source = repository_root / "src" / "local_steward" / "codex_hooks.py"
    if not (repository_root / "pyproject.toml").is_file():
        raise ValueError("repository identity is unavailable")
    if (
        not (source_skill / "SKILL.md").is_file()
        or not server_source.is_dir()
        or not hook_source.is_file()
    ):
        raise ValueError("STEWARD Skill, server, or hook source is unavailable")
    if not output_parent.is_dir():
        raise ValueError("candidate parent must already exist")
    python_executable = _require_absolute_file(python_executable, executable=True)
    config_path = _require_absolute_file(config_path)
    plugin_root = output_parent / PLUGIN_NAME
    if plugin_root.exists() or plugin_root.is_symlink():
        raise FileExistsError("candidate destination already exists")
    (plugin_root / ".codex-plugin").mkdir(parents=True)
    shutil.copytree(source_skill, plugin_root / "skills" / SKILL_NAME)
    (plugin_root / "hooks").mkdir()
    hook_implementation_sha256 = _sha256(hook_source.read_bytes())
    hooks_bytes = _canonical_bytes(hooks_manifest(python_executable, hook_implementation_sha256))
    (plugin_root / "hooks" / "hooks.json").write_bytes(hooks_bytes)
    host_policy_bytes = _canonical_bytes(host_policy_machine_object(create_codex_host_policy()))
    host_policy_path = plugin_root / ".codex-host-policy.json"
    host_policy_path.write_bytes(host_policy_bytes)
    manifest_bytes = _canonical_bytes(plugin_manifest(plugin_version))
    policy_bytes = codex_approval_policy_toml().encode("utf-8")
    codex_policy_path = output_parent / "steward-exoskeleton-codex-policy.toml"
    (plugin_root / ".codex-plugin" / "plugin.json").write_bytes(manifest_bytes)
    bind_r4d_r3d_plugin_runtime(
        plugin_root=plugin_root,
        python_executable=python_executable,
        config_path=config_path,
    )
    mcp_bytes = (plugin_root / ".mcp.json").read_bytes()
    codex_policy_path.write_bytes(policy_bytes)
    replacements = {
        str(python_executable): "<STEWARD_PYTHON>",
        str(config_path): "<STEWARD_CONFIG>",
        str(host_policy_path): "<STEWARD_HOST_POLICY>",
    }
    sanitized_mcp = mcp_bytes.decode("utf-8")
    for raw, token in replacements.items():
        sanitized_mcp = sanitized_mcp.replace(raw, token)
    return R4DR3DPluginCandidate(
        plugin_root,
        codex_policy_path,
        plugin_version,
        _sha256(manifest_bytes),
        _tree_digest(source_skill),
        _tree_digest(server_source),
        hook_implementation_sha256,
        _sha256(hooks_bytes),
        _sha256(host_policy_bytes),
        _sha256(policy_bytes),
        _sha256(sanitized_mcp.encode("utf-8")),
        _tree_digest(plugin_root, replacements),
    )
