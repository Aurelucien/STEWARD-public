"""Pure deterministic components behind Snapshot Diagnostic Projection."""

from local_steward.models import FilesystemEntry, FilesystemObjectType, FilesystemObservationStatus
from local_steward.observation_projection import (
    ProjectionBudget,
    ProjectionPolicy,
    SelectionReason,
    SnapshotDiagnosticRequest,
    SourcePlanState,
)
from local_steward.observation_projection.entry_facts import extract_entry_anchor
from local_steward.observation_projection.models import EntrySourceSide
from local_steward.observation_projection.selection import SelectionCandidate, coalesce_candidates, select_candidates
from local_steward.observation_projection.source_plan import plan_snapshot_diagnostic_sources


SNAPSHOT_ID = "11111111-1111-1111-1111-111111111111"
SECOND_ID = "22222222-2222-2222-2222-222222222222"


def _policy(*, duplicate: bool = False, relation: bool = False, budget: int = 3) -> ProjectionPolicy:
    return ProjectionPolicy(0, "raw-path", ProjectionBudget(budget, 8, 0, 2, 2, 1, 1, (("DIAGNOSTIC_BOUNDARY", budget),), 1_000), duplicate, relation)


def _entry(scope: str, path: str, *, size: int | None = 1) -> FilesystemEntry:
    return FilesystemEntry(
        f"{scope}:{path}", SNAPSHOT_ID, scope, path, FilesystemObjectType.REGULAR_FILE,
        1, 2, 0o644, 1, 2, size, 3, 4, None, 1, None, True, False, False,
        FilesystemObservationStatus.OBSERVED, None, None,
    )


def test_source_planner_only_requests_explicit_sources() -> None:
    hierarchy = plan_snapshot_diagnostic_sources(SnapshotDiagnosticRequest(SNAPSHOT_ID), _policy())
    assert hierarchy.structure_requested and not hierarchy.duplicate_requested and not hierarchy.relation_requested
    duplicate = plan_snapshot_diagnostic_sources(SnapshotDiagnosticRequest(SNAPSHOT_ID, hierarchy_requested=False, duplicate_overlay=SourcePlanState.REQUESTED_AND_EMPTY), _policy(duplicate=True))
    assert not duplicate.structure_requested and duplicate.duplicate_requested
    relation = plan_snapshot_diagnostic_sources(SnapshotDiagnosticRequest(SNAPSHOT_ID, relation_context_pair=(SNAPSHOT_ID, SECOND_ID)), _policy(relation=True))
    assert relation.relation_requested and relation.relation_pair == (SNAPSHOT_ID, SECOND_ID)


def test_selection_coalesces_reasons_and_preserves_scope_fairness() -> None:
    values = (
        SelectionCandidate(_entry("dense", "a"), (SelectionReason.UNKNOWN_SIZE,)),
        SelectionCandidate(_entry("dense", "a"), (SelectionReason.LOGICAL_BYTE_CONTRIBUTOR,)),
        SelectionCandidate(_entry("dense", "b"), (SelectionReason.UNKNOWN_SIZE,)),
        SelectionCandidate(_entry("other", "c"), (SelectionReason.UNKNOWN_SIZE,)),
    )
    merged = coalesce_candidates(values)
    assert len(merged) == 3
    selected = select_candidates(merged, _policy(budget=2).budget)
    assert {item.entry.scope_id for item in selected} == {"dense", "other"}
    assert SelectionReason.LOGICAL_BYTE_CONTRIBUTOR in selected[0].reasons


def test_entry_extractor_is_pure_and_keeps_unknown_size_as_unknown() -> None:
    entry = _entry("managed", "unknown", size=None)
    anchor = extract_entry_anchor(entry, source_side=EntrySourceSide.PRIMARY, reasons=(SelectionReason.UNKNOWN_SIZE,))
    assert anchor.entry_reference.relative_path == "unknown"
    assert anchor.size_facts.size_bytes is None
    assert anchor.size_facts.state.value == "UNKNOWN"
    assert not hasattr(anchor, "entries")
