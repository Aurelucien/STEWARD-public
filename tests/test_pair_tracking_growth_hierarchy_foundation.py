"""Foundation tests for Pair Tracking Growth hierarchy model repair."""

from dataclasses import replace

import pytest

from local_steward.observation_projection import (
    Accounting,
    AccountingDomain,
    CoverageAvailability,
    DiagnosticState,
    ExpansionDescriptor,
    HierarchyPresentationState,
    ObservationProjectionInvariantError,
    PairTrackingBody,
    PairTrackingGrowthHierarchyContext,
    PairTrackingGrowthHierarchyItem,
    PairTrackingRequest,
    PathGrowthMetrics,
    ProjectionBudget,
    ProjectionMode,
    ProjectionPolicy,
    ProjectionPreDigest,
    ProjectionSourceIdentity,
    ProjectionSourceValidity,
    ResultKind,
    ResultLocalReference,
    ResultNamespace,
    SelectionReason,
    SnapshotPairSourceIdentity,
    SnapshotSourceIdentity,
    SourcePlanItem,
    SourcePlanState,
    SourceResultIdentity,
    canonical_projection,
    finalize,
    machine_object,
)

BASE_ID = "11111111-1111-1111-1111-111111111111"
TARGET_ID = "22222222-2222-2222-2222-222222222222"


def _policy() -> ProjectionPolicy:
    return ProjectionPolicy(
        0,
        "raw-path",
        ProjectionBudget(4, 4, 4, 2, 2, 2, 1, (("TRACKING_FACT", 2),), 4096),
    )


def _pair() -> SnapshotPairSourceIdentity:
    return SnapshotPairSourceIdentity(
        SnapshotSourceIdentity(BASE_ID, 2, "base-snapshot"),
        SnapshotSourceIdentity(TARGET_ID, 2, "target-snapshot"),
    )


def _growth_identity() -> SourceResultIdentity:
    return SourceResultIdentity(
        ResultKind.GROWTH,
        _pair(),
        "storage_growth",
        1,
        "growth-result-digest",
    )


def _namespace() -> ResultNamespace:
    return ResultNamespace(ResultKind.GROWTH, _pair(), "growth-result-digest")


def _metrics(delta: int = 3) -> PathGrowthMetrics:
    return PathGrowthMetrics(
        10,
        10 + delta,
        delta,
        max(delta, 0),
        1 if delta > 0 else 0,
        max(-delta, 0),
        1 if delta < 0 else 0,
        max(delta, 0),
        1 if delta > 0 else 0,
        max(-delta, 0),
        1 if delta < 0 else 0,
        0,
        0,
        True,
    )


def _descriptor(path: str, local_id: str) -> ExpansionDescriptor:
    return ExpansionDescriptor(
        (BASE_ID, TARGET_ID),
        ResultKind.GROWTH,
        _namespace(),
        scope="managed",
        path_prefix=path or ".",
        limit=20,
        local_id=local_id,
    )


def _item(path: str = "a", local_id: str = "growth-node-a", delta: int = 3) -> PairTrackingGrowthHierarchyItem:
    return PairTrackingGrowthHierarchyItem(
        ResultLocalReference(_namespace(), local_id),
        "managed",
        path,
        _metrics(delta),
        _metrics(delta),
        HierarchyPresentationState.EXPANDED,
        (SelectionReason.GROWTH_CONTRIBUTOR,),
        _descriptor(path, local_id),
    )


def _state(identity: ProjectionSourceIdentity) -> DiagnosticState:
    return DiagnosticState(
        ProjectionSourceValidity.VALID,
        identity,
        CoverageAvailability.COMPLETE,
        CoverageAvailability.COMPLETE,
        CoverageAvailability.UNKNOWN,
        (),
        (),
        0,
        0,
        0,
        0,
        0,
        0,
        SourcePlanState.NOT_REQUESTED,
        None,
        0,
    )


def _facts(
    state: SourcePlanState = SourcePlanState.REQUESTED_AND_PRESENT,
    items: tuple[PairTrackingGrowthHierarchyItem, ...] | None = None,
) -> ProjectionPreDigest:
    pair = _pair()
    identity = ProjectionSourceIdentity(snapshot_pair=pair)
    growth = _growth_identity() if state != SourcePlanState.NOT_REQUESTED else None
    hierarchy_items = (_item(),) if items is None and state == SourcePlanState.REQUESTED_AND_PRESENT else (items or ())
    accounting: tuple[Accounting, ...] = (
        Accounting(AccountingDomain.PAIR_TRACKING_LOCATION, 0, 0, 0),
        Accounting(AccountingDomain.GROWTH_REGULAR_LOCATION, 0, 0, 0),
    )
    if state != SourcePlanState.NOT_REQUESTED:
        accounting += (
            Accounting(
                AccountingDomain.PAIR_TRACKING_GROWTH_HIERARCHY,
                len(hierarchy_items),
                len(hierarchy_items),
                0,
            ),
        )
    context = PairTrackingGrowthHierarchyContext(state, growth, hierarchy_items)
    source_plan = (
        SourcePlanItem(ResultKind.DIFF, SourcePlanState.REQUESTED_AND_PRESENT),
        SourcePlanItem(ResultKind.GROWTH, state, growth),
    )
    descriptors = tuple(item.expansion_descriptor for item in hierarchy_items)
    return ProjectionPreDigest(
        ProjectionMode.PAIR_TRACKING,
        PairTrackingRequest(BASE_ID, TARGET_ID, growth=state),
        _policy(),
        identity,
        source_plan,
        _state(identity),
        accounting,
        descriptors,
        None,
        PairTrackingBody((), (), context, ()),
    )


def test_growth_hierarchy_context_distinguishes_not_requested_and_requested_empty() -> None:
    not_requested = _facts(SourcePlanState.NOT_REQUESTED)
    requested_empty = _facts(SourcePlanState.REQUESTED_AND_EMPTY)
    assert finalize(not_requested).projection_digest != finalize(requested_empty).projection_digest
    not_requested_wire = machine_object(not_requested)["pair_tracking"]["growth_hierarchy"]
    empty_wire = machine_object(requested_empty)["pair_tracking"]["growth_hierarchy"]
    assert not_requested_wire == {"state": "NOT_REQUESTED", "hierarchy_items": []}
    assert empty_wire["state"] == "REQUESTED_AND_EMPTY"
    assert empty_wire["source_result_identity"]["result_kind"] == "GROWTH"


def test_requested_present_growth_hierarchy_canonicalizes_nodes_and_preserves_metrics() -> None:
    first = _item("a", "node-a", 3)
    second = _item("b", "node-b", -2)
    facts = _facts(items=(second, first))
    wire = machine_object(facts)
    items = wire["pair_tracking"]["growth_hierarchy"]["hierarchy_items"]
    assert [item["relative_directory_path"] for item in items] == ["a", "b"]
    assert items[0]["direct_metrics"]["known_net_logical_delta"] == 3
    assert items[1]["recursive_metrics"]["known_net_logical_delta"] == -2
    assert items[0]["presentation"] == "EXPANDED"
    assert finalize(facts).projection_digest == finalize(_facts(items=(first, second))).projection_digest


def test_presentation_and_node_facts_are_digest_sensitive() -> None:
    facts = _facts()
    context = facts.pair_tracking
    assert context is not None
    item = context.growth_hierarchy.hierarchy_items[0]
    folded = replace(item, presentation=HierarchyPresentationState.FOLDED)
    changed = replace(
        facts,
        pair_tracking=replace(
            context,
            growth_hierarchy=replace(context.growth_hierarchy, hierarchy_items=(folded,)),
        ),
    )
    assert finalize(changed).projection_digest != finalize(facts).projection_digest


@pytest.mark.parametrize(
    "facts",
    (
        _facts(SourcePlanState.REQUESTED_AND_EMPTY, (_item(),)),
        replace(
            _facts(SourcePlanState.REQUESTED_AND_PRESENT),
            pair_tracking=PairTrackingBody(
                (),
                (),
                PairTrackingGrowthHierarchyContext(SourcePlanState.NOT_REQUESTED, None, (_item(),)),
                (),
            ),
        ),
        replace(
            _facts(),
            pair_tracking=PairTrackingBody(
                (),
                (),
                PairTrackingGrowthHierarchyContext(
                    SourcePlanState.REQUESTED_AND_PRESENT,
                    None,
                    (_item(),),
                ),
                (),
            ),
        ),
        _facts(items=(_item(), _item())),
        replace(
            _facts(),
            pair_tracking=PairTrackingBody(
                (),
                (),
                PairTrackingGrowthHierarchyContext(
                    SourcePlanState.REQUESTED_AND_PRESENT,
                    _growth_identity(),
                    (replace(_item(), expansion_descriptor=None),),  # type: ignore[arg-type]
                ),
                (),
            ),
        ),
        replace(
            _facts(),
            pair_tracking=PairTrackingBody(
                (),
                (),
                PairTrackingGrowthHierarchyContext(
                    SourcePlanState.REQUESTED_AND_PRESENT,
                    _growth_identity(),
                    (replace(_item(), expansion_descriptor=_descriptor("a", "other-node")),),
                ),
                (),
            ),
        ),
    ),
)
def test_growth_hierarchy_invalid_states_are_hard_failures(facts: ProjectionPreDigest) -> None:
    with pytest.raises(ObservationProjectionInvariantError):
        canonical_projection(facts)


def test_hierarchy_accounting_must_match_retained_nodes() -> None:
    facts = _facts()
    invalid = replace(
        facts,
        accounting=(
            Accounting(AccountingDomain.PAIR_TRACKING_LOCATION, 0, 0, 0),
            Accounting(AccountingDomain.GROWTH_REGULAR_LOCATION, 0, 0, 0),
            Accounting(AccountingDomain.PAIR_TRACKING_GROWTH_HIERARCHY, 1, 0, 1),
        ),
    )
    with pytest.raises(ObservationProjectionInvariantError):
        canonical_projection(invalid)
