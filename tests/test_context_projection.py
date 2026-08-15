"""Isolated acceptance for the additive 0.6.0 Context Projection contract."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.agent_session import create_steward_session
from local_steward.native_mcp_server import NativeStewardDispatcher, create_codex_host_policy
from local_steward.native_mcp_server.protocol import HISTORY_TOOL
from local_steward.scan_budget import make_budget
from local_steward.snapshot_acquisition import SnapshotAcquisitionRequest, acquire_snapshot
from local_steward.snapshots import create_snapshot
from local_steward.models import ScanBudget

from .test_protocol_completion import prepared_config


@pytest.fixture(autouse=True)
def _admit_task_owned_temporary_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())


def _fixture(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = prepared_config(tmp_path)
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "a.txt").write_text("a", encoding="utf-8")
    nested = scope / "nested"
    nested.mkdir()
    (nested / "b.txt").write_text("bb", encoding="utf-8")
    config = replace(
        config,
        scopes=(replace(config.scopes[0], raw_path=str(scope), normalized_path=scope),),
    )
    snapshot = create_snapshot(config, (), make_budget())
    return config, snapshot


def _supported_pair(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = prepared_config(tmp_path)
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "a.txt").write_text("a", encoding="utf-8")
    config = replace(
        config,
        scopes=(replace(config.scopes[0], raw_path=str(scope), normalized_path=scope),),
    )
    base = acquire_snapshot(
        config, SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True)
    )
    (scope / "b.txt").write_text("b", encoding="utf-8")
    target = acquire_snapshot(
        config, SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True)
    )
    assert base.snapshot_id is not None and target.snapshot_id is not None
    return config, base.snapshot_id, target.snapshot_id


def _data_manifest(config) -> tuple[tuple[str, bytes, int], ...]:  # type: ignore[no-untyped-def]
    return tuple(
        (
            path.relative_to(config.paths.data_dir).as_posix(),
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(config.paths.data_dir.rglob("*"))
        if path.is_file()
    )


async def _dispatch(session, arguments):  # type: ignore[no-untyped-def]
    return await NativeStewardDispatcher(session, create_codex_host_policy()).dispatch(
        HISTORY_TOOL, arguments
    )


@pytest.mark.anyio
async def test_explicit_general_projection_is_deterministic_and_legacy_is_unchanged(
    tmp_path: Path,
) -> None:
    config, snapshot = _fixture(tmp_path)
    session = create_steward_session(config)
    dispatcher = NativeStewardDispatcher(session, create_codex_host_policy())
    before_manifest = _data_manifest(config)
    legacy = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "ANALYZE_SNAPSHOT",
            "selector": {"policy": "EXACT_ID", "snapshot_id": snapshot.snapshot_id},
            "question": "Describe the historical Snapshot.",
            "limit": 10,
        },
    )
    first = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "ANALYZE_SNAPSHOT",
            "selector": {"policy": "EXACT_ID", "snapshot_id": snapshot.snapshot_id},
            "analysis_profile": "GENERAL",
            "question": "Describe the historical Snapshot.",
            "limit": 10,
        },
    )
    second = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "ANALYZE_SNAPSHOT",
            "selector": {"policy": "EXACT_ID", "snapshot_id": snapshot.snapshot_id},
            "analysis_profile": "GENERAL",
            "question": "Describe the historical Snapshot.",
            "limit": 10,
        },
    )
    assert legacy.isError is False
    assert "context_pack" in legacy.structuredContent["result"]
    assert first.isError is False
    assert first.structuredContent == second.structuredContent
    projection = first.structuredContent["result"]["context_projection"]
    assert projection["projection_kind"] == "GENERAL"
    assert projection["source"]["snapshot_id"] == snapshot.snapshot_id
    assert projection["source"]["verification_status"] == "VALID"
    assert projection["context_projection_digest"]
    assert all(item["anchor_ids"] for item in projection["observed_facts"])
    assert _data_manifest(config) == before_manifest
    assert not tuple(config.paths.data_dir.glob("state.db-*"))


@pytest.mark.anyio
async def test_structure_projection_has_bound_continuation_and_rejects_mismatch(
    tmp_path: Path,
) -> None:
    config, snapshot = _fixture(tmp_path)
    dispatcher = NativeStewardDispatcher(create_steward_session(config), create_codex_host_policy())
    first = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "ANALYZE_SNAPSHOT",
            "selector": {"policy": "EXACT_ID", "snapshot_id": snapshot.snapshot_id},
            "analysis_profile": "STRUCTURE_OVERVIEW",
            "question": "Summarize the structure.",
            "limit": 1,
            "offset": 0,
        },
    )
    assert first.isError is False
    projection = first.structuredContent["result"]["context_projection"]
    assert projection["continuation"]["has_more"] is True
    continuation = projection["continuation"]
    second = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "ANALYZE_SNAPSHOT",
            "selector": {"policy": "EXACT_ID", "snapshot_id": snapshot.snapshot_id},
            "analysis_profile": "STRUCTURE_OVERVIEW",
            "question": "Summarize the structure.",
            "limit": 1,
            "offset": continuation["next_offset"],
            "continuation": {
                "request_digest": continuation["request_digest"],
                "offset": continuation["next_offset"],
            },
        },
    )
    assert second.isError is False
    second_projection = second.structuredContent["result"]["context_projection"]
    assert second_projection["continuation"]["offset"] == continuation["next_offset"]
    mismatch = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "ANALYZE_SNAPSHOT",
            "selector": {"policy": "EXACT_ID", "snapshot_id": snapshot.snapshot_id},
            "analysis_profile": "STRUCTURE_OVERVIEW",
            "question": "Summarize the structure.",
            "limit": 1,
            "offset": continuation["next_offset"],
            "continuation": {"request_digest": "0" * 64, "offset": continuation["next_offset"]},
        },
    )
    assert mismatch.isError is True
    assert mismatch.structuredContent["error"]["code"] == "STEWARD_NATIVE_ARGUMENT_INVALID"
    assert mismatch.structuredContent["error"]["cause_code"] == "CONTINUATION_MISMATCH"


@pytest.mark.anyio
async def test_deferred_and_unknown_profiles_fail_without_a_projection(
    tmp_path: Path,
) -> None:
    config, snapshot = _fixture(tmp_path)
    dispatcher = NativeStewardDispatcher(create_steward_session(config), create_codex_host_policy())
    for profile in ("STORAGE_HOTSPOTS", "NOT_A_PROFILE"):
        result = await dispatcher.dispatch(
            HISTORY_TOOL,
            {
                "action": "ANALYZE_SNAPSHOT",
                "selector": {"policy": "EXACT_ID", "snapshot_id": snapshot.snapshot_id},
                "analysis_profile": profile,
                "question": "Summarize.",
            },
        )
        assert result.isError is True
        assert result.structuredContent["result"] is None
        assert result.structuredContent["error"]["cause_code"] == "UNSUPPORTED_PROFILE"


@pytest.mark.anyio
async def test_change_triage_requires_explicit_base_and_reuses_review_facts(
    tmp_path: Path,
) -> None:
    config, base_id, target_id = _supported_pair(tmp_path)
    dispatcher = NativeStewardDispatcher(create_steward_session(config), create_codex_host_policy())
    missing_base = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "ANALYZE_SNAPSHOT",
            "selector": {"policy": "EXACT_ID", "snapshot_id": target_id},
            "analysis_profile": "CHANGE_TRIAGE",
            "question": "Summarize changes.",
        },
    )
    assert missing_base.isError is True
    assert missing_base.structuredContent["error"]["cause_code"] == "BASE_SELECTOR_REQUIRED"
    result = await dispatcher.dispatch(
        HISTORY_TOOL,
        {
            "action": "ANALYZE_SNAPSHOT",
            "selector": {"policy": "EXACT_ID", "snapshot_id": target_id},
            "base_selector": {"policy": "EXACT_ID", "snapshot_id": base_id},
            "analysis_profile": "CHANGE_TRIAGE",
            "question": "Summarize changes.",
            "limit": 10,
        },
    )
    assert result.isError is False
    projection = result.structuredContent["result"]["context_projection"]
    assert projection["projection_kind"] == "CHANGE_TRIAGE"
    assert projection["source"]["base_snapshot_id"] == base_id
    assert projection["source"]["target_snapshot_id"] == target_id
    assert any(item["object_kind"] == "CHANGE_EVENT" for item in projection["observed_facts"])
