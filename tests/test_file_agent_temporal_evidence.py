"""Offline contract coverage for one-Entry Temporal Evidence Fusion C1."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path

import pytest

from local_steward.file_agent import SharedToolBudget, ToolBudgetLimits, ToolExecutionContext
from local_steward.file_agent.runtime import (
    AgentRuntime,
    AgentTurnRequest,
    CombinedBudget,
    CombinedBudgetLimits,
    ComparisonOutcome,
    CurrentMetadataObservation,
    CurrentState,
    ModelFinalAnswer,
    ModelToolCall,
    ModelTurnResult,
    ProjectOwnedBoundedTextMcp,
    ProjectOwnedCurrentMetadataObserver,
    RuntimeFailure,
    ScopeBinding,
    ScopeBindings,
    TemporalEvidenceRelationService,
    ToolRegistry,
    register_temporal_evidence_tool,
)
from local_steward.file_agent.runtime.preflight import ScriptedFakeToolCallingModel
from local_steward.models import FilesystemObjectType, SnapshotEntryReference
from local_steward.payload_hashing import PayloadLocality, default_payload_hash_policy
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot, get_snapshot

from .test_protocol_completion import prepared_config


def _local(_path: Path) -> PayloadLocality:
    return PayloadLocality.LOCAL


def _fixture(
    tmp_path: Path,
    *,
    content: bytes = b"alpha",
    v2: bool = True,
    algorithm_version: int = 1,
):
    config = prepared_config(tmp_path)
    root = tmp_path / "current"
    root.mkdir()
    source = root / "sample.txt"
    source.write_bytes(content)
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=root),))
    policy = None
    if v2:
        policy = replace(default_payload_hash_policy(), algorithm_version=algorithm_version)
    snapshot = create_snapshot(config, (), make_budget(), policy, locality_provider=_local)
    bindings = ScopeBindings(
        (ScopeBinding("managed", root),),
        (str(root),),
        ("managed",),
    )
    context = ToolExecutionContext(config, SharedToolBudget())
    service = TemporalEvidenceRelationService(
        context,
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
    )
    arguments = {
        "snapshot_id": snapshot.snapshot_id,
        "scope_id": "managed",
        "relative_path": "sample.txt",
    }
    return config, root, snapshot, bindings, service, arguments


def _outcomes(relation) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {item.field: item.outcome.value for item in relation.field_comparisons}


class _SequenceMetadata:
    def __init__(self, *values: CurrentMetadataObservation) -> None:
        self.values = list(values)

    def observe(self, _scope_id: str, _relative_path: str) -> CurrentMetadataObservation:
        return self.values.pop(0)


class _TrackingMetadata:
    calls: int = 0

    def observe(self, scope_id: str, relative_path: str) -> CurrentMetadataObservation:
        self.calls += 1
        return CurrentMetadataObservation(CurrentState.UNAVAILABLE, scope_id, relative_path)


def _run(runtime: AgentRuntime, call: ModelToolCall):
    model = ScriptedFakeToolCallingModel(
        (
            ModelTurnResult(tool_call=call),
            ModelTurnResult(final_answer=ModelFinalAnswer("done")),
        )
    )
    return asyncio.run(runtime.run(AgentTurnRequest("offline relation"), model))


def test_unchanged_and_changed_metadata_are_compared_without_identity_claims(tmp_path: Path) -> None:
    _config, root, _snapshot, _bindings, service, arguments = _fixture(tmp_path)

    unchanged = service.compare(arguments)
    unchanged_outcomes = _outcomes(unchanged)
    assert unchanged.current_state == CurrentState.PRESENT
    assert unchanged_outcomes["object_type"] == "SAME"
    assert unchanged_outcomes["size_bytes"] == "SAME"
    assert unchanged_outcomes["mode"] == "SAME"

    (root / "sample.txt").write_bytes(b"alpha-expanded")
    changed_service = TemporalEvidenceRelationService(
        ToolExecutionContext(service.context.config, SharedToolBudget()),
        service.bindings,
        ProjectOwnedBoundedTextMcp(service.bindings),
    )
    changed = changed_service.compare(arguments)
    changed_outcomes = _outcomes(changed)
    assert changed_outcomes["size_bytes"] == "DIFFERENT"
    assert changed_outcomes["mtime_ns"] == "DIFFERENT"
    serialized = str(changed.payload())
    assert "SAME_LOGICAL_LOCATION" in serialized
    assert all(term not in serialized for term in ("SAME_FILE", "SAME_OBJECT", "IDENTICAL_FILE"))


def test_absence_and_unavailability_are_distinct(tmp_path: Path) -> None:
    _config, root, _snapshot, bindings, service, arguments = _fixture(tmp_path)
    (root / "sample.txt").unlink()
    absent = service.compare(arguments)
    assert absent.current_state == CurrentState.ABSENT
    assert set(_outcomes(absent).values()) == {"NOT_COMPARABLE"}

    unavailable = CurrentMetadataObservation(
        CurrentState.UNAVAILABLE,
        "managed",
        "sample.txt",
        reason_code="CURRENT_PERMISSION_DENIED",
    )
    unavailable_service = TemporalEvidenceRelationService(
        ToolExecutionContext(service.context.config, SharedToolBudget()),
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
        current_metadata=_SequenceMetadata(unavailable),
    )
    result = unavailable_service.compare(arguments)
    assert result.current_state == CurrentState.UNAVAILABLE
    assert set(_outcomes(result).values()) == {"UNKNOWN"}
    assert result.observation_facts == ("CURRENT_PERMISSION_DENIED",)


def test_payload_equal_and_unequal_use_only_complete_bounded_digest_evidence(tmp_path: Path) -> None:
    _config, root, snapshot, bindings, service, arguments = _fixture(tmp_path)
    payload_arguments = {**arguments, "include_payload_comparison": True}

    equal = service.compare(payload_arguments)
    assert equal.payload_comparison.outcome == ComparisonOutcome.SAME
    assert equal.payload_comparison.historical is not None
    assert equal.payload_comparison.current is not None
    assert equal.content_bytes_observed == len(b"alpha")
    assert "content" not in equal.payload()

    (root / "sample.txt").write_bytes(b"bravo")
    unequal_service = TemporalEvidenceRelationService(
        ToolExecutionContext(service.context.config, SharedToolBudget()),
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
    )
    unequal = unequal_service.compare(
        {"snapshot_id": snapshot.snapshot_id, "scope_id": "managed", "relative_path": "sample.txt", "include_payload_comparison": True}
    )
    assert unequal.payload_comparison.outcome == ComparisonOutcome.DIFFERENT
    assert unequal.payload_comparison.reason_code == "PAYLOAD_DIGEST_DIFFERENT"


def test_missing_and_incompatible_historical_digests_never_trigger_current_read(tmp_path: Path) -> None:
    _config, _root, _snapshot, bindings, service, arguments = _fixture(tmp_path / "v1", v2=False)
    reads: list[int] = []

    def counted_read(descriptor: int, size: int) -> bytes:
        reads.append(size)
        return os.read(descriptor, size)

    missing_service = TemporalEvidenceRelationService(
        service.context,
        bindings,
        ProjectOwnedBoundedTextMcp(bindings, read_bytes=counted_read),
    )
    missing = missing_service.compare({**arguments, "include_payload_comparison": True})
    assert missing.payload_comparison.outcome == ComparisonOutcome.UNKNOWN
    assert missing.payload_comparison.reason_code == "HISTORICAL_PAYLOAD_UNKNOWN"
    assert reads == []

    _config2, _root2, _snapshot2, bindings2, service2, arguments2 = _fixture(tmp_path / "v2")
    assert service2.historical_resolver is not None
    resolved = service2.historical_resolver.resolve(
        SnapshotEntryReference(str(arguments2["snapshot_id"]), "managed", "sample.txt")
    )
    assert hasattr(resolved.entry, "payload_observation")
    incompatible_entry = replace(
        resolved.entry,
        payload_observation=replace(resolved.entry.payload_observation, algorithm_version=2),  # type: ignore[union-attr]
    )

    class IncompatibleResolver:
        def resolve(self, _reference):  # type: ignore[no-untyped-def]
            return replace(resolved, entry=incompatible_entry)

    incompatible_service = TemporalEvidenceRelationService(
        service2.context,
        bindings2,
        ProjectOwnedBoundedTextMcp(bindings2, read_bytes=counted_read),
        historical_resolver=IncompatibleResolver(),
    )
    incompatible = incompatible_service.compare({**arguments2, "include_payload_comparison": True})
    assert incompatible.payload_comparison.outcome == ComparisonOutcome.NOT_COMPARABLE
    assert incompatible.payload_comparison.reason_code == "PAYLOAD_INCOMPATIBLE"
    assert incompatible.current_source_kinds == ("CURRENT_FILESYSTEM_METADATA",)


def test_binary_and_oversized_current_payloads_are_unknown_not_different(tmp_path: Path) -> None:
    _config, _root, _snapshot, _bindings, binary_service, binary_arguments = _fixture(
        tmp_path / "binary", content=b"\xff\xfe\x00\x80"
    )
    binary = binary_service.compare({**binary_arguments, "include_payload_comparison": True})
    assert binary.current_state == CurrentState.PRESENT
    assert binary.payload_comparison.outcome == ComparisonOutcome.UNKNOWN
    assert binary.payload_comparison.reason_code == "CURRENT_PAYLOAD_UNKNOWN"
    assert binary.payload_comparison.current is None
    assert binary.content_bytes_observed == 0
    assert "observed_content_sha256" not in str(binary.payload())

    _config2, _root2, _snapshot2, bindings2, service2, arguments2 = _fixture(
        tmp_path / "oversized", content=b"x" * 8193
    )
    reads: list[int] = []

    def counted_read(descriptor: int, size: int) -> bytes:
        reads.append(size)
        return os.read(descriptor, size)

    oversized_service = TemporalEvidenceRelationService(
        service2.context,
        bindings2,
        ProjectOwnedBoundedTextMcp(bindings2, read_bytes=counted_read),
    )
    oversized = oversized_service.compare({**arguments2, "include_payload_comparison": True})
    assert oversized.payload_comparison.outcome == ComparisonOutcome.UNKNOWN
    assert oversized.payload_comparison.reason_code == "CURRENT_PAYLOAD_UNKNOWN"
    assert oversized.current_source_kinds == ("CURRENT_FILESYSTEM_METADATA",)
    assert reads == []


def test_cross_observation_mutation_rejects_mixed_state_comparisons(tmp_path: Path) -> None:
    _config, _root, _snapshot, bindings, service, arguments = _fixture(tmp_path)
    observer = ProjectOwnedCurrentMetadataObserver(bindings)
    before = observer.observe("managed", "sample.txt")
    assert before._coherence_token is not None
    after = replace(before, _coherence_token=(99, 1, 2, 3, 4, 5))
    mutation_service = TemporalEvidenceRelationService(
        service.context,
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
        current_metadata=_SequenceMetadata(before, after),
    )
    relation = mutation_service.compare({**arguments, "include_payload_comparison": True})
    assert relation.current_state == CurrentState.CHANGED_DURING_OBSERVATION
    assert set(_outcomes(relation).values()) == {"UNKNOWN"}
    assert relation.payload_comparison.outcome == ComparisonOutcome.UNKNOWN
    assert relation.payload_comparison.current is None


def test_missing_metadata_is_unknown_and_never_equal(tmp_path: Path) -> None:
    _config, _root, _snapshot, bindings, service, arguments = _fixture(tmp_path)
    assert service.historical_resolver is not None
    historical = service.historical_resolver.resolve(
        SnapshotEntryReference(str(arguments["snapshot_id"]), "managed", "sample.txt")
    )

    class Resolver:
        def resolve(self, _reference):  # type: ignore[no-untyped-def]
            return replace(historical, entry=replace(historical.entry, uid=None))

    current = ProjectOwnedCurrentMetadataObserver(bindings).observe("managed", "sample.txt")
    missing_service = TemporalEvidenceRelationService(
        ToolExecutionContext(service.context.config, SharedToolBudget()),
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
        historical_resolver=Resolver(),
        current_metadata=_SequenceMetadata(replace(current, uid=None)),
    )
    relation = missing_service.compare(arguments)
    uid = next(item for item in relation.field_comparisons if item.field == "uid")
    assert uid.historical.state.value == "UNKNOWN"
    assert uid.current.state.value == "UNKNOWN"
    assert uid.outcome == ComparisonOutcome.UNKNOWN

    unknown_type_service = TemporalEvidenceRelationService(
        ToolExecutionContext(service.context.config, SharedToolBudget()),
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
        current_metadata=_SequenceMetadata(
            replace(current, object_type=FilesystemObjectType.UNKNOWN)
        ),
    )
    unknown_type = unknown_type_service.compare(arguments)
    object_type = next(
        item for item in unknown_type.field_comparisons if item.field == "object_type"
    )
    assert object_type.current.state.value == "UNKNOWN"
    assert object_type.outcome == ComparisonOutcome.UNKNOWN


def test_symlink_target_is_observed_without_following_or_publishing_target_content(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    root = tmp_path / "current"
    root.mkdir()
    (root / "target.txt").write_text("secret target", encoding="utf-8")
    (root / "link.txt").symlink_to("target.txt")
    config = replace(config, scopes=(replace(config.scopes[0], normalized_path=root),))
    snapshot = create_snapshot(config, (), make_budget())
    bindings = ScopeBindings((ScopeBinding("managed", root),), (str(root),), ("managed",))
    service = TemporalEvidenceRelationService(
        ToolExecutionContext(config, SharedToolBudget()), bindings, ProjectOwnedBoundedTextMcp(bindings)
    )
    relation = service.compare(
        {"snapshot_id": snapshot.snapshot_id, "scope_id": "managed", "relative_path": "link.txt"}
    )
    outcomes = _outcomes(relation)
    assert outcomes["object_type"] == "SAME"
    assert outcomes["symlink_target_raw"] == "SAME"
    assert "secret target" not in str(relation.payload())


def test_result_provenance_digest_and_historical_content_boundary_are_deterministic(tmp_path: Path) -> None:
    _config, _root, _snapshot, bindings, service, arguments = _fixture(tmp_path)
    first = service.compare(arguments)
    second = TemporalEvidenceRelationService(
        ToolExecutionContext(service.context.config, SharedToolBudget()),
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
    ).compare(arguments)
    assert first.payload() == second.payload()
    assert first.result_digest == second.result_digest
    payload = first.payload()
    assert payload["source_kind"] == "HISTORICAL_CURRENT_RELATION"
    assert payload["historical_provenance"]["repository_verification"] == "VALID"  # type: ignore[index]
    assert payload["current_source_provenance"]["source_kinds"] == ["CURRENT_FILESYSTEM_METADATA"]  # type: ignore[index]
    assert all(key not in str(payload).lower() for key in ("historical_content", "historical_text"))
    assert "evidence_relative_path" not in str(payload)


def test_payload_is_skipped_by_default_and_tool_schema_exposes_no_fact_inputs(tmp_path: Path) -> None:
    _config, _root, _snapshot, _bindings, service, arguments = _fixture(tmp_path)
    registry = ToolRegistry()
    register_temporal_evidence_tool(registry, service)
    assert tuple(item.name for item in registry.tools) == ("compare_historical_current",)
    schema = registry.tools[0].input_schema
    assert set(schema["properties"]) == {
        "snapshot_id",
        "scope_id",
        "relative_path",
        "include_payload_comparison",
    }
    assert schema["additionalProperties"] is False
    relation = service.compare(arguments)
    assert relation.payload_comparison.reason_code == "PAYLOAD_NOT_REQUESTED"
    assert relation.content_bytes_observed == 0
    assert relation.current_source_kinds == ("CURRENT_FILESYSTEM_METADATA",)


def test_runtime_reuses_dynamic_content_and_serialized_budgets(tmp_path: Path) -> None:
    _config, _root, _snapshot, bindings, service, arguments = _fixture(tmp_path)
    registry = ToolRegistry()
    register_temporal_evidence_tool(registry, service)
    without_payload = _run(
        AgentRuntime(registry),
        ModelToolCall("relation-1", "compare_historical_current", arguments),
    )
    assert without_payload.failure_code is None
    assert without_payload.budget.usage.content_bytes_reserved == 0
    assert without_payload.budget.usage.filesystem_tool_calls_used == 1

    payload_service = TemporalEvidenceRelationService(
        ToolExecutionContext(service.context.config, SharedToolBudget()),
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
    )
    payload_registry = ToolRegistry()
    register_temporal_evidence_tool(payload_registry, payload_service)
    with_payload = _run(
        AgentRuntime(payload_registry),
        ModelToolCall(
            "relation-2",
            "compare_historical_current",
            {**arguments, "include_payload_comparison": True},
        ),
    )
    assert with_payload.failure_code is None
    assert with_payload.budget.usage.content_bytes_reserved == 8192
    assert with_payload.budget.usage.content_bytes_observed == len(b"alpha")

    blocked_service = TemporalEvidenceRelationService(
        ToolExecutionContext(service.context.config, SharedToolBudget()),
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
    )
    blocked_registry = ToolRegistry()
    register_temporal_evidence_tool(blocked_registry, blocked_service)
    blocked = _run(
        AgentRuntime(blocked_registry, CombinedBudget(CombinedBudgetLimits(max_content_bytes=0))),
        ModelToolCall(
            "relation-3",
            "compare_historical_current",
            {**arguments, "include_payload_comparison": True},
        ),
    )
    assert blocked.failure_code == "BUDGET_EXHAUSTED"
    assert blocked_service.context.budget.usage.calls == 0

    serialized_service = TemporalEvidenceRelationService(
        ToolExecutionContext(service.context.config, SharedToolBudget()),
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
    )
    serialized_registry = ToolRegistry()
    register_temporal_evidence_tool(serialized_registry, serialized_service)
    serialized = _run(
        AgentRuntime(
            serialized_registry,
            CombinedBudget(CombinedBudgetLimits(max_serialized_bytes=1)),
        ),
        ModelToolCall("relation-4", "compare_historical_current", arguments),
    )
    assert serialized.final_answer is None
    assert serialized.failure_code == "BUDGET_EXHAUSTED"
    assert serialized.traces[0].status == "ERROR"


def test_invalid_historical_scope_binding_and_shared_budget_fail_safely(tmp_path: Path) -> None:
    _config, _root, _snapshot, bindings, service, arguments = _fixture(tmp_path)
    with pytest.raises(RuntimeFailure) as missing:
        service.compare({**arguments, "snapshot_id": "missing-snapshot"})
    assert missing.value.code == "STEWARD_TOOL_FAILED"

    unbound = ScopeBindings((), (), ())
    unbound_service = TemporalEvidenceRelationService(
        ToolExecutionContext(service.context.config, SharedToolBudget()),
        unbound,
        ProjectOwnedBoundedTextMcp(unbound),
    )
    with pytest.raises(RuntimeFailure) as scope:
        unbound_service.compare(arguments)
    assert scope.value.code == "SCOPE_BINDING_FAILED"

    exhausted = TemporalEvidenceRelationService(
        ToolExecutionContext(
            service.context.config,
            SharedToolBudget(ToolBudgetLimits(max_steward_calls_per_turn=0)),
        ),
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
    )
    with pytest.raises(RuntimeFailure) as budget:
        exhausted.compare(arguments)
    assert budget.value.code == "BUDGET_EXHAUSTED"


def test_corrupt_historical_evidence_is_rejected_before_current_observation(tmp_path: Path) -> None:
    config, _root, snapshot, bindings, service, arguments = _fixture(tmp_path)
    persisted = get_snapshot(config, snapshot.snapshot_id)
    assert persisted.evidence_relative_path is not None
    evidence_path = config.paths.evidence_dir / persisted.evidence_relative_path
    evidence_path.write_text("{}", encoding="utf-8")
    tracking = _TrackingMetadata()
    corrupt_service = TemporalEvidenceRelationService(
        ToolExecutionContext(config, SharedToolBudget()),
        bindings,
        ProjectOwnedBoundedTextMcp(bindings),
        current_metadata=tracking,
    )
    with pytest.raises(RuntimeFailure) as corrupt:
        corrupt_service.compare(arguments)
    assert corrupt.value.code == "STEWARD_TOOL_FAILED"
    assert tracking.calls == 0
    assert service.current_metadata is not tracking


def test_v01_recovery_carries_historical_failure_without_new_state_machine(tmp_path: Path) -> None:
    _config, _root, _snapshot, _bindings, service, arguments = _fixture(tmp_path)
    registry = ToolRegistry()
    register_temporal_evidence_tool(registry, service)
    result = _run(
        AgentRuntime(registry),
        ModelToolCall(
            "missing-history",
            "compare_historical_current",
            {**arguments, "snapshot_id": "missing-snapshot"},
        ),
    )
    assert result.final_answer == "done"
    assert result.failure_code is None
    assert result.traces[0].status == "ERROR"
    assert result.traces[0].failure_code == "STEWARD_TOOL_FAILED"
