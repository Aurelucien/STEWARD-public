"""Declarative Codex host-policy binding for the native STEWARD MCP server."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from ..agent_authority import RiskClass


HOST_POLICY_SCHEMA_NAME = "local_steward.codex_host_policy"
HOST_POLICY_SCHEMA_VERSION = 1
HOST_KIND = "CODEX"
HOST_AUTHORITY_BOUNDARY = "CODEX_MCP_TOOL_APPROVAL_POLICY"
APPROVAL_EVIDENCE = "HOST_ENFORCED_NOT_ATTESTED_TO_SERVER"


@dataclass(frozen=True, slots=True)
class HostToolPolicy:
    tool_name: str
    risk_class: RiskClass
    approval_mode: str
    read_only: bool
    destructive: bool


@dataclass(frozen=True, slots=True)
class CodexHostPolicy:
    schema_name: str
    schema_version: int
    host_kind: str
    authority_boundary: str
    default_tools_approval_mode: str
    tools: tuple[HostToolPolicy, ...]
    policy_digest: str

    def tool(self, tool_name: str) -> HostToolPolicy:
        for policy in self.tools:
            if policy.tool_name == tool_name:
                return policy
        raise KeyError(tool_name)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _payload(tools: tuple[HostToolPolicy, ...]) -> dict[str, Any]:
    return {
        "schema_name": HOST_POLICY_SCHEMA_NAME,
        "schema_version": HOST_POLICY_SCHEMA_VERSION,
        "host_kind": HOST_KIND,
        "authority_boundary": HOST_AUTHORITY_BOUNDARY,
        "default_tools_approval_mode": "writes",
        "tools": [
            {
                "tool_name": item.tool_name,
                "risk_class": item.risk_class.value,
                "approval_mode": item.approval_mode,
                "read_only": item.read_only,
                "destructive": item.destructive,
            }
            for item in tools
        ],
    }


def create_codex_host_policy() -> CodexHostPolicy:
    """Return the frozen installation policy expected from the Codex host."""
    tools = (
        HostToolPolicy(
            "steward_history", RiskClass.HISTORICAL_READ, "approve", True, False
        ),
        HostToolPolicy(
            "steward_read_document", RiskClass.CURRENT_CONTENT_READ, "approve", True, False
        ),
        HostToolPolicy(
            "steward_code_execution", RiskClass.CODE_WORKSPACE_READ, "approve", True, False
        ),
        HostToolPolicy(
            "steward_update_snapshot", RiskClass.DERIVED_STATE_APPEND, "prompt", False, False
        ),
        HostToolPolicy(
            "steward_recover_snapshot_run",
            RiskClass.RECOVERY_OR_ADMIN,
            "prompt",
            False,
            True,
        ),
    )
    payload = _payload(tools)
    digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return CodexHostPolicy(
        HOST_POLICY_SCHEMA_NAME,
        HOST_POLICY_SCHEMA_VERSION,
        HOST_KIND,
        HOST_AUTHORITY_BOUNDARY,
        "writes",
        tools,
        digest,
    )


def host_policy_machine_object(policy: CodexHostPolicy) -> dict[str, Any]:
    payload = _payload(policy.tools)
    payload["policy_digest"] = policy.policy_digest
    return payload


def load_codex_host_policy(path: Path) -> CodexHostPolicy:
    """Fail closed unless an installation record matches the frozen host policy."""
    expected = create_codex_host_policy()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Codex host policy is unavailable") from error
    if value != host_policy_machine_object(expected):
        raise ValueError("Codex host policy does not match the native tool contract")
    return expected


def host_authority_machine_object(
    policy: CodexHostPolicy, tool_name: str
) -> dict[str, Any]:
    """Publish policy provenance without claiming receipt of an approval decision."""
    try:
        approval_mode: str | None = policy.tool(tool_name).approval_mode
    except KeyError:
        approval_mode = None
    return {
        "boundary": policy.authority_boundary,
        "host_kind": policy.host_kind,
        "policy_digest": policy.policy_digest,
        "default_tools_approval_mode": policy.default_tools_approval_mode,
        "tool_approval_mode": approval_mode,
        "approval_evidence": APPROVAL_EVIDENCE,
    }
