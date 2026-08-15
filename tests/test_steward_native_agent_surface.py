"""Isolated acceptance for R4D-R3D Codex-hosted native Agent tools."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from experiments.steward_exoskeleton.r4d_r3d_plugin_candidate import (
    CONFIG_ENVIRONMENT_VARIABLE,
    HOST_POLICY_ENVIRONMENT_VARIABLE,
    MCP_SERVER_NAME,
    PLUGIN_ID,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    bind_r4d_r3d_plugin_runtime,
    build_r4d_r3d_plugin_candidate,
)
from local_steward.agent_authority import (
    AuthorityAdmission,
    AuthorityPath,
    AuthoritySource,
    RiskClass,
    StewardAuthorityContextError,
    StewardAuthorityRequiredError,
    authority_context_machine_object,
    create_authority_context,
    load_authority_context,
    require_authority,
)
from local_steward.agent_session import create_steward_session
from local_steward.codex_identity import (
    HOOK_IDENTITY,
    MCP_SERVER_NAME as IDENTITY_MCP_SERVER_NAME,
    NATIVE_SERVER_VERSION,
    NATIVE_SURFACE_IDENTITY,
    PLUGIN_BASE_VERSION,
    PLUGIN_NAME as IDENTITY_PLUGIN_NAME,
    SKILL_NAME,
    integration_identity_machine_object,
)
from local_steward.native_mcp_server import (
    NativeStewardDispatcher,
    create_codex_host_policy,
    host_policy_machine_object,
    load_codex_host_policy,
)
from local_steward.native_mcp_server.adapter import _success_text
from local_steward.native_mcp_server.host_policy import APPROVAL_EVIDENCE
from local_steward.native_mcp_server.protocol import (
    CODE_TOOL,
    DOCUMENT_TOOL,
    HISTORY_TOOL,
    INPUT_SCHEMAS,
    RECOVERY_TOOL,
    SERVER_INSTRUCTIONS,
    SERVER_VERSION,
    TOOL_NAMES,
    UPDATE_TOOL,
    tool_descriptors,
)
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot

from .test_document_inspection_product import _write_pdf, _write_xlsx
from .test_protocol_completion import prepared_config


ROOT = Path(__file__).resolve().parents[1]


def test_compact_structure_text_repeats_exact_media_facts_for_delivery() -> None:
    text = _success_text(
        DOCUMENT_TOOL,
        {
            "document": {
                "view": "STRUCTURE",
                "source_format": "MP4",
                "returned_count": 3,
                "has_more": False,
                "media": {
                    "duration_ms": 3000,
                    "decoded_frame_count": 0,
                    "decoded_audio_bytes": 0,
                },
            },
            "evidence_packet": {
                "facts": [
                    {"kind": "video_video_stream"},
                    {"kind": "video_audio_stream"},
                ],
                "verification": {"status": "OBSERVATION_COMPLETE"},
            },
        },
    )

    assert '"duration_ms":3000' in text
    assert '"track_kinds":["video","audio"]' in text
    assert '"verification":"OBSERVATION_COMPLETE"' in text


def test_codex_integration_identity_is_one_path_free_tuple() -> None:
    identity = integration_identity_machine_object()
    assert identity == {
        "schema_name": "local_steward.codex_integration_identity",
        "schema_version": 1,
        "plugin_name": IDENTITY_PLUGIN_NAME,
        "plugin_base_version": PLUGIN_BASE_VERSION,
        "skill_name": SKILL_NAME,
        "mcp_server_name": IDENTITY_MCP_SERVER_NAME,
        "native_surface_identity": NATIVE_SURFACE_IDENTITY,
        "native_server_version": NATIVE_SERVER_VERSION,
        "hook_identity": HOOK_IDENTITY,
    }
    assert identity["plugin_name"] == PLUGIN_NAME
    assert identity["plugin_base_version"] == PLUGIN_VERSION
    assert identity["mcp_server_name"] == MCP_SERVER_NAME
    assert json.dumps(identity, sort_keys=True, separators=(",", ":")) in SERVER_INSTRUCTIONS
    assert not any("/" in str(value) for value in identity.values())


@pytest.fixture(autouse=True)
def _admit_task_owned_temporary_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())


def _session(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = prepared_config(tmp_path)
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "a.txt").write_text("a", encoding="utf-8")
    config = replace(
        config,
        scopes=(replace(config.scopes[0], raw_path=str(scope), normalized_path=scope),),
    )
    return config, scope, create_steward_session(config)


def test_r3c_authority_context_remains_truthful_historical_contract(tmp_path: Path) -> None:
    _config, _scope, session = _session(tmp_path)
    path = AuthorityPath("managed", "a.txt")
    context = create_authority_context(
        session,
        task_identity="historical-r4d-r3c-task",
        source=AuthoritySource.EXPLICIT_REQUEST,
        admissions=(
            AuthorityAdmission(RiskClass.HISTORICAL_READ, ("STATUS",)),
            AuthorityAdmission(
                RiskClass.CURRENT_CONTENT_READ,
                ("READ_DOCUMENT",),
                scope_ids=("managed",),
                paths=(path,),
            ),
        ),
    )
    require_authority(
        context,
        session,
        RiskClass.CURRENT_CONTENT_READ,
        "READ_DOCUMENT",
        scope_id="managed",
        path=path,
    )
    with pytest.raises(StewardAuthorityRequiredError):
        require_authority(
            context,
            session,
            RiskClass.CURRENT_CONTENT_READ,
            "READ_DOCUMENT",
        )
    payload = authority_context_machine_object(context)
    authority_path = tmp_path / "authority.json"
    authority_path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_authority_context(authority_path, session) == context
    payload["source"] = "HOST_APPROVAL"
    authority_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StewardAuthorityContextError):
        load_authority_context(authority_path, session)


def test_codex_host_policy_is_exact_tamper_evident_and_not_approval_evidence(
    tmp_path: Path,
) -> None:
    policy = create_codex_host_policy()
    payload = host_policy_machine_object(policy)
    path = tmp_path / "host-policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_codex_host_policy(path) == policy
    assert [item.approval_mode for item in policy.tools] == [
        "approve",
        "approve",
        "approve",
        "prompt",
        "prompt",
    ]
    payload["tools"][3]["approval_mode"] = "approve"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_codex_host_policy(path)


@pytest.mark.anyio
async def test_native_history_and_update_use_one_codex_host_policy(tmp_path: Path) -> None:
    config, _scope, session = _session(tmp_path)
    first = create_snapshot(config, (), make_budget())
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())
    status = await dispatcher.dispatch(HISTORY_TOOL, {"action": "STATUS"})
    inventory = await dispatcher.dispatch(HISTORY_TOOL, {"action": "LIST_SNAPSHOTS", "limit": 10})
    inspection = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "INSPECT_SNAPSHOT",
            "selector": {"policy": "LATEST_VALID", "scope_id": "managed"},
            "scope_id": "managed",
            "limit": 10,
        },
    )
    acquisition = await dispatcher.dispatch(
        UPDATE_TOOL,
        {
            "action": "ACQUIRE",
            "scope": {"policy": "ONLY_COMPATIBLE"},
            "max_entries": 100,
            "max_duration_seconds": 10,
        },
    )
    assert status.isError is inventory.isError is inspection.isError is False
    assert acquisition.isError is False
    assert inspection.structuredContent["selection"][0]["snapshot_id"] == first.snapshot_id
    created = acquisition.structuredContent["result"]["created_identity"]
    assert created == {
        "run_id": acquisition.structuredContent["result"]["acquisition"]["run_id"],
        "snapshot_id": acquisition.structuredContent["result"]["acquisition"]["snapshot_id"],
    }
    assert acquisition.structuredContent["authority"]["tool_approval_mode"] == "prompt"
    assert acquisition.structuredContent["authority"]["approval_evidence"] == APPROVAL_EVIDENCE


@pytest.mark.anyio
async def test_native_document_accepts_user_named_absolute_path_without_sidecar(
    tmp_path: Path,
) -> None:
    _config, scope, session = _session(tmp_path)
    document = scope / "named.pdf"
    _write_pdf(document)
    result = await NativeStewardDispatcher(session, create_codex_host_policy()).dispatch(
        DOCUMENT_TOOL, {"absolute_path": str(document), "limit": 10}
    )
    assert result.isError is False
    assert result.structuredContent["selection"] == [
        {
            "object_kind": "CURRENT_DOCUMENT",
            "policy": "ONLY_COMPATIBLE",
            "input_kind": "USER_ABSOLUTE",
            "scope_id": "managed",
            "relative_path": "named.pdf",
        }
    ]
    page = result.structuredContent["result"]["document"]
    assert page["status"] == "COMPLETE"
    assert page["source_format"] == "PDF"
    assert "searchable PDF page 1" in page["items"][0]["text_or_value"]
    assert str(scope) not in json.dumps(result.structuredContent, sort_keys=True)


@pytest.mark.anyio
async def test_flat_document_schema_keeps_semantic_source_validation(tmp_path: Path) -> None:
    _config, _scope, session = _session(tmp_path)
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    invalid_requests = (
        {"action": "CAPABILITIES", "query": "file.pdf"},
        {"action": "EVIDENCE", "content_query": "term"},
        {"action": "READ", "scope_id": "managed"},
        {"action": "READ", "query": "file.pdf", "absolute_path": "/tmp/file.pdf"},
        {"action": "READ", "query": "file.pdf", "diagnostic_detail": "FULL"},
    )
    for arguments in invalid_requests:
        result = await dispatcher.dispatch(DOCUMENT_TOOL, arguments)
        assert result.isError is True
        assert result.structuredContent["error"]["code"] == (
            "STEWARD_NATIVE_ARGUMENT_INVALID"
        )


@pytest.mark.anyio
async def test_native_document_actions_route_compact_views_and_locations(tmp_path: Path) -> None:
    _config, scope, session = _session(tmp_path)
    pdf = scope / "named.pdf"
    workbook = scope / "facts.xlsx"
    _write_pdf(pdf)
    _write_xlsx(workbook)
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    structure = await dispatcher.dispatch(
        DOCUMENT_TOOL, {"action": "AUTO", "absolute_path": str(pdf), "limit": 10}
    )
    location = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {
            "action": "LOCATE",
            "absolute_path": str(pdf),
            "content_query": "searchable PDF",
            "limit": 10,
        },
    )
    tables = await dispatcher.dispatch(
        DOCUMENT_TOOL,
        {"action": "EXTRACT_TABLE", "absolute_path": str(workbook), "limit": 10},
    )

    structure_page = structure.structuredContent["result"]["document"]
    assert structure_page["view"] == "STRUCTURE"
    assert structure_page["projection"] == "GROUNDED_EVIDENCE_ONLY"
    structure_packet = structure.structuredContent["result"]["evidence_packet"]
    assert structure_packet["source"]["backend_name"] == "PyMuPDFNativeStructure"
    assert structure_packet["facts"][0]["kind"] == "pdf_document"
    location_packet = location.structuredContent["result"]["evidence_packet"]
    assert location_packet["bounds"]["content_search"]["matched_item_count"] == 1
    assert location_packet["facts"][0]["fact_kind"] == "CONTENT_MATCH"
    assert len(
        json.dumps(structure.structuredContent, separators=(",", ":")).encode("utf-8")
    ) < 4_000
    table_page = tables.structuredContent["result"]["document"]
    assert table_page["view"] == "TABLES"
    assert {item["role"] for item in table_page["items"]} == {"TABLE_CELL"}


@pytest.mark.anyio
async def test_native_document_admits_one_host_file_without_persisting_a_scope(
    tmp_path: Path,
) -> None:
    config, _scope, session = _session(tmp_path)
    external = tmp_path / "host-selected"
    external.mkdir()
    document = external / "outside.pdf"
    _write_pdf(document)
    before_scopes = config.scopes

    result = await NativeStewardDispatcher(session, create_codex_host_policy()).dispatch(
        DOCUMENT_TOOL,
        {"action": "STRUCTURE", "absolute_path": str(document), "limit": 10},
    )

    assert result.isError is False
    assert config.scopes == before_scopes
    assert result.structuredContent["selection"] == [
        {
            "object_kind": "CURRENT_DOCUMENT",
            "policy": "HOST_AUTHORIZED_EXACT_PATH",
            "input_kind": "USER_ABSOLUTE",
            "scope_id": "steward_host_file",
            "relative_path": "outside.pdf",
            "scope_lifetime": "OPERATION",
            "persistence_effect": "NONE",
            "authority_boundary": "CODEX_HOST_TOOL_POLICY",
        }
    ]
    packet = result.structuredContent["result"]["evidence_packet"]
    assert packet["source"]["relative_path"] == "outside.pdf"
    assert packet["facts"]
    assert str(external) not in json.dumps(result.structuredContent, sort_keys=True)

    link = external / "linked.pdf"
    link.symlink_to(document)
    rejected = await NativeStewardDispatcher(session, create_codex_host_policy()).dispatch(
        DOCUMENT_TOOL,
        {"action": "STRUCTURE", "absolute_path": str(link)},
    )
    assert rejected.isError is True
    assert rejected.structuredContent["error"]["cause_code"] == (
        "STEWARD_PATH_RESOLUTION_INVALID"
    )


@pytest.mark.anyio
async def test_native_surface_rejects_model_side_authority_and_cross_task_fields(
    tmp_path: Path,
) -> None:
    _config, _scope, session = _session(tmp_path)
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())
    forbidden = {
        "confirmed": True,
        "authority_context": {"source": "MODEL"},
        "config_path": "/tmp/other.toml",
        "task_reference_id": "steward-task-run-forged",
    }
    for field, value in forbidden.items():
        result = await dispatcher.dispatch(
            RECOVERY_TOOL,
            {"run_id": "00000000-0000-4000-8000-000000000000", field: value},
        )
        assert result.isError is True
        assert result.structuredContent["error"] == {
            "code": "STEWARD_NATIVE_ARGUMENT_INVALID",
            "cause_code": None,
            "message": "The STEWARD tool arguments are invalid.",
        }
    encoded = json.dumps(INPUT_SCHEMAS, sort_keys=True)
    for forbidden_name in (
        "confirmed",
        "authority_context",
        "config_path",
        "task_reference_id",
        "anchor_task_reference_id",
    ):
        assert forbidden_name not in encoded


@pytest.mark.anyio
async def test_native_errors_publish_stable_class_and_preserve_safe_cause(tmp_path: Path) -> None:
    _config, _scope, session = _session(tmp_path)
    result = await NativeStewardDispatcher(session, create_codex_host_policy()).dispatch(
        HISTORY_TOOL,
        {
            "action": "INSPECT_SNAPSHOT",
            "selector": {
                "policy": "EXACT_ID",
                "snapshot_id": "00000000-0000-4000-8000-000000000000",
            },
        },
    )
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "STEWARD_NATIVE_SELECTION_INVALID"
    assert result.structuredContent["error"]["cause_code"] == "STEWARD_SELECTION_NOT_FOUND"


def test_native_document_deadline_contains_the_adaptive_parser_and_release_window(
    tmp_path: Path,
) -> None:
    _config, _scope, session = _session(tmp_path)
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())

    assert dispatcher._deadline_seconds(DOCUMENT_TOOL) == 660.0
    assert dispatcher._deadline_seconds(HISTORY_TOOL) == 120.0

    overridden = NativeStewardDispatcher(session, create_codex_host_policy(), timeout_seconds=0.25)
    assert overridden._deadline_seconds(DOCUMENT_TOOL) == 0.25
    assert overridden._deadline_seconds(HISTORY_TOOL) == 0.25


def _candidate(tmp_path: Path, *, plugin_version: str = PLUGIN_VERSION):  # type: ignore[no-untyped-def]
    prepared_config(tmp_path)
    config_path = tmp_path / "config" / "steward.toml"
    output = tmp_path / "candidate"
    output.mkdir()
    return build_r4d_r3d_plugin_candidate(
        repository_root=ROOT,
        output_parent=output,
        python_executable=Path(sys.executable),
        config_path=config_path,
        plugin_version=plugin_version,
    )


def test_candidate_has_one_skill_one_server_and_explicit_codex_policy(tmp_path: Path) -> None:
    first = _candidate(tmp_path / "first")
    second = _candidate(tmp_path / "second")
    assert first.safe_descriptor() == second.safe_descriptor()
    root = first.plugin_root
    assert root.name == PLUGIN_NAME
    assert {item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file()} == {
        ".codex-host-policy.json",
        ".codex-plugin/plugin.json",
        ".mcp.json",
        "hooks/hooks.json",
        "skills/steward-codex/SKILL.md",
        "skills/steward-codex/agents/openai.yaml",
        "skills/steward-codex/references/audio-routing.md",
        "skills/steward-codex/references/document-routing.md",
        "skills/steward-codex/references/evidence-delivery.md",
        "skills/steward-codex/references/execution-continuity.md",
        "skills/steward-codex/references/history-and-lifecycle.md",
        "skills/steward-codex/references/video-routing.md",
    }
    manifest = json.loads((root / ".codex-plugin/plugin.json").read_text())
    mcp = json.loads((root / ".mcp.json").read_text())
    hooks = json.loads((root / "hooks/hooks.json").read_text())
    assert manifest["version"] == PLUGIN_VERSION
    assert set(mcp["mcpServers"]) == {MCP_SERVER_NAME}
    binding = mcp["mcpServers"][MCP_SERVER_NAME]
    assert binding["args"] == ["-m", "local_steward.native_mcp_server"]
    assert set(binding["env"]) == {
        CONFIG_ENVIRONMENT_VARIABLE,
        HOST_POLICY_ENVIRONMENT_VARIABLE,
    }
    assert "AUTHORITY_CONTEXT" not in json.dumps(mcp)
    skill = (root / "skills/steward-codex/SKILL.md").read_text()
    agent = (root / "skills/steward-codex/agents/openai.yaml").read_text()
    assert skill.startswith("---\nname: steward-codex\n")
    assert "STEWARD_PLUGIN_IDENTITY_MISMATCH" in skill
    assert "steward_code_execution" in skill
    assert "STEWARD_HOST_OBSERVER_V1_ACTIVE" in skill
    assert "$steward-codex" in agent
    assert set(hooks["hooks"]) == {
        "SessionStart",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    }
    hook_command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "local_steward.codex_hooks" in hook_command
    assert "${PLUGIN_DATA}/steward-host-observer-v1" in hook_command
    assert first.hook_implementation_sha256 in hook_command
    policy = tomllib.loads(first.codex_policy_path.read_text())
    server_policy = policy["plugins"][PLUGIN_ID]["mcp_servers"][MCP_SERVER_NAME]
    assert server_policy["default_tools_approval_mode"] == "writes"
    assert server_policy["tools"][HISTORY_TOOL]["approval_mode"] == "approve"
    assert server_policy["tools"][DOCUMENT_TOOL]["approval_mode"] == "approve"
    assert server_policy["tools"][CODE_TOOL]["approval_mode"] == "approve"
    assert server_policy["tools"][UPDATE_TOOL]["approval_mode"] == "prompt"
    assert server_policy["tools"][RECOVERY_TOOL]["approval_mode"] == "prompt"
    assert str(tmp_path) not in json.dumps(first.safe_descriptor(), sort_keys=True)

    descriptors = {item.name: item for item in tool_descriptors()}
    assert descriptors[HISTORY_TOOL].annotations.readOnlyHint is True
    assert descriptors[DOCUMENT_TOOL].annotations.readOnlyHint is True
    assert descriptors[CODE_TOOL].annotations.readOnlyHint is True
    assert descriptors[UPDATE_TOOL].annotations.destructiveHint is False
    assert descriptors[RECOVERY_TOOL].annotations.destructiveHint is True
    assert "STEWARD_CODEX_NATIVE_V27" in SERVER_INSTRUCTIONS
    assert "local_steward.codex_integration_identity" in SERVER_INSTRUCTIONS
    assert "STEWARD_HOST_OBSERVER_V1_ACTIVE" in SERVER_INSTRUCTIONS
    assert all(name in SERVER_INSTRUCTIONS for name in TOOL_NAMES)


def test_personal_skill_identity_is_distinct_from_repository_product_skill() -> None:
    source = (
        ROOT
        / "experiments"
        / "steward_exoskeleton"
        / "r4d_r3d_plugin_source"
        / "skills"
        / "steward-codex"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "name: steward-codex" in source
    assert "CLI/Python remain product surfaces" in source
    assert "STEWARD_PLUGIN_IDENTITY_MISMATCH" in source
    execution = (
        ROOT
        / "experiments"
        / "steward_exoskeleton"
        / "r4d_r3d_plugin_source"
        / "skills"
        / "steward-codex"
        / "references"
        / "execution-continuity.md"
    ).read_text(encoding="utf-8")
    assert "hooks already observe" in execution
    routing = (
        ROOT
        / "experiments"
        / "steward_exoskeleton"
        / "r4d_r3d_plugin_source"
        / "skills"
        / "steward-codex"
        / "references"
        / "document-routing.md"
    ).read_text(encoding="utf-8")
    assert "quality-gated local OCR" in routing
    assert "PDF_ANNOTATION" in routing
    assert "WORD_COMMENT" in routing
    assert "WORKBOOK_CHART" in routing
    assert "PRESENTATION_SPEAKER_NOTES" in routing
    assert "PDF_NATIVE_STRUCTURE_BODY_NOT_PARSED" in routing
    assert "timed-out `EVIDENCE`" in routing
    assert "On `TIMEOUT`" in source
    assert "`EVIDENCE_SET`" in source
    assert "across files" in source


def test_candidate_version_override_is_path_free_and_default_is_current(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path / "override", plugin_version="0.13.1")
    manifest = json.loads((candidate.plugin_root / ".codex-plugin/plugin.json").read_text())
    assert manifest["version"] == "0.13.1"
    assert candidate.safe_descriptor()["plugin_version"] == "0.13.1"
    assert PLUGIN_VERSION == "0.33.0"
    assert str(tmp_path) not in json.dumps(candidate.safe_descriptor(), sort_keys=True)


@pytest.mark.anyio
@pytest.mark.parametrize("plugin_version", [PLUGIN_VERSION, "0.13.1"])
async def test_candidate_official_client_discovers_five_risk_tools(
    tmp_path: Path, plugin_version: str
) -> None:
    candidate = _candidate(tmp_path, plugin_version=plugin_version)
    payload = json.loads((candidate.plugin_root / ".mcp.json").read_text())
    binding = payload["mcpServers"][MCP_SERVER_NAME]
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    assert Path(binding["command"]) == Path(sys.executable)
    parameters = StdioServerParameters(
        command=binding["command"],
        args=binding["args"],
        env=binding["env"],
        cwd=unrelated_cwd,
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            tools = await client.list_tools()
            resources = await client.list_resources()
            prompts = await client.list_prompts()
    assert initialized.serverInfo.name == MCP_SERVER_NAME
    assert initialized.serverInfo.version == SERVER_VERSION == "24"
    assert initialized.instructions == SERVER_INSTRUCTIONS
    assert tuple(item.name for item in tools.tools) == TOOL_NAMES
    assert resources.resources == [] and prompts.prompts == []


@pytest.mark.anyio
async def test_official_client_meta_reaches_native_thread_attribution(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    payload = json.loads((candidate.plugin_root / ".mcp.json").read_text())
    binding = payload["mcpServers"][MCP_SERVER_NAME]
    parameters = StdioServerParameters(
        command=binding["command"], args=binding["args"], env=binding["env"]
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool(
                HISTORY_TOOL,
                {},
                meta={"openai/session": "official-client-thread-006"},
            )

    attribution = result.structuredContent["thread_attribution"]
    assert result.isError is True
    assert attribution["status"] == "HOST_BOUND"
    assert attribution["thread_reference"] == "official-client-thread-006"
    assert attribution["authorization_effect"] == "NONE"


def test_copied_candidate_must_rebind_host_policy_to_stable_root(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path / "source")
    installed_root = tmp_path / "installed" / PLUGIN_NAME
    installed_root.parent.mkdir()
    shutil.copytree(candidate.plugin_root, installed_root)
    original = json.loads((candidate.plugin_root / ".mcp.json").read_text())
    config_path = Path(original["mcpServers"][MCP_SERVER_NAME]["env"][CONFIG_ENVIRONMENT_VARIABLE])

    digest = bind_r4d_r3d_plugin_runtime(
        plugin_root=installed_root,
        python_executable=Path(sys.executable),
        config_path=config_path,
    )
    rebound = json.loads((installed_root / ".mcp.json").read_text())
    policy_path = Path(
        rebound["mcpServers"][MCP_SERVER_NAME]["env"][HOST_POLICY_ENVIRONMENT_VARIABLE]
    )

    assert policy_path == installed_root / ".codex-host-policy.json"
    assert str(candidate.plugin_root) not in json.dumps(rebound)
    assert len(digest) == 64


def test_candidate_tampered_host_policy_fails_without_path_or_traceback(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    payload = json.loads((candidate.plugin_root / ".mcp.json").read_text())
    binding = payload["mcpServers"][MCP_SERVER_NAME]
    policy_path = Path(binding["env"][HOST_POLICY_ENVIRONMENT_VARIABLE])
    policy = json.loads(policy_path.read_text())
    policy["tools"][3]["approval_mode"] = "approve"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    completed = subprocess.run(
        [binding["command"], *binding["args"]],
        env=binding["env"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.strip() == (
        "STEWARD native session or Codex host policy is unavailable."
    )
    assert str(tmp_path) not in completed.stderr
    assert "Traceback" not in completed.stderr


def test_legacy_confirmation_flags_are_confined_to_private_product_bridge() -> None:
    native_root = ROOT / "src" / "local_steward" / "native_mcp_server"
    occurrences = {
        path.name: path.read_text().count("confirmed=True")
        for path in native_root.glob("*.py")
        if "confirmed=True" in path.read_text()
    }
    assert occurrences == {"product_bridge.py": 2}
