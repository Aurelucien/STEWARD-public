"""Foundation readiness tests for the complete Projection v0 model contract."""

from dataclasses import FrozenInstanceError

import pytest

from local_steward.models import FilesystemObjectType, FilesystemObservationStatus, PayloadObservationProvenance, RelationCertainty, SnapshotDiffChangeType, SnapshotEntryReference
from local_steward.observation_projection import (
    Accounting, AccountingDomain, ContentState, CoverageAvailability,
    DiagnosticBoundary, DiagnosticState, EntryMetadataFacts, EntryObjectHintFacts,
    EntryPayloadFacts, EntrySizeFacts, EntrySizeState, EntrySourceSide,
    ExpansionDescriptor, ExplicitEntryAnchor, HierarchyItem, HierarchyPresentationState,
    ObservationProjectionInvariantError, OverlayItem, OverlayKind, PairTrackingBody,
    PairTrackingGrowthHierarchyContext, PairTrackingRequest, PayloadFactState, ProjectionBudget, ProjectionMode,
    ProjectionPolicy, ProjectionPreDigest, ProjectionSourceIdentity,
    ProjectionSourceValidity, ResultKind, ResultLocalReference, ResultNamespace,
    SelectionReason, SnapshotDiagnosticBody, SnapshotDiagnosticRequest,
    SnapshotPairSourceIdentity, SnapshotSourceIdentity, SourcePlanItem, SourcePlanState,
    SourceResultIdentity, StructureMetrics, TrackingItem, canonical_projection, finalize,
    machine_object, normalize_reasons, normalize_request,
)


PRIMARY_ID = "11111111-1111-1111-1111-111111111111"
TARGET_ID = "22222222-2222-2222-2222-222222222222"


def _policy() -> ProjectionPolicy:
    return ProjectionPolicy(0, "raw-path", ProjectionBudget(4, 4, 4, 2, 2, 2, 1, (("DIAGNOSTIC_BOUNDARY", 2),), 4096), True, True)


def _snapshot() -> SnapshotSourceIdentity:
    return SnapshotSourceIdentity(PRIMARY_ID, 2, "snapshot-digest")


def _pair() -> SnapshotPairSourceIdentity:
    return SnapshotPairSourceIdentity(_snapshot(), SnapshotSourceIdentity(TARGET_ID, 2, "target-digest"))


def _namespace(kind: ResultKind = ResultKind.STRUCTURE) -> ResultNamespace:
    return ResultNamespace(kind, _snapshot(), "result-digest")


def _reference(kind: ResultKind = ResultKind.STRUCTURE, local_id: str = "node-1") -> ResultLocalReference:
    return ResultLocalReference(_namespace(kind), local_id)


def _descriptor(kind: ResultKind = ResultKind.STRUCTURE) -> ExpansionDescriptor:
    return ExpansionDescriptor((PRIMARY_ID,), kind, _namespace(kind), scope="managed", path_prefix="a", limit=20)


def _state(identity: ProjectionSourceIdentity | None = None) -> DiagnosticState:
    return DiagnosticState(
        ProjectionSourceValidity.VALID, identity or ProjectionSourceIdentity(primary_snapshot=_snapshot()),
        CoverageAvailability.PARTIAL, CoverageAvailability.PARTIAL, CoverageAvailability.UNKNOWN,
        (DiagnosticBoundary("INTEGRITY_CONFLICT", _reference()),),
        (DiagnosticBoundary("SCOPE_OVERLAP", _reference()),), 1, 1, 1, 1, 1, 1,
        SourcePlanState.NOT_REQUESTED, 10, None,
    )


def _anchor() -> ExplicitEntryAnchor:
    entry = SnapshotEntryReference(PRIMARY_ID, "managed", "a/file")
    metadata = EntryMetadataFacts(FilesystemObservationStatus.OBSERVED, 0o644, 1, 2, 3, 4, None, None, True, False, False, False, None)
    size = EntrySizeFacts(EntrySizeState.KNOWN, 10)
    payload = EntryPayloadFacts(PayloadFactState.VERIFIED, "sha256", 1, "payload", PayloadObservationProvenance.DIRECT_READ)
    return ExplicitEntryAnchor(entry, EntrySourceSide.PRIMARY, FilesystemObjectType.REGULAR_FILE, metadata, size, payload, EntryObjectHintFacts(7, 8, 1), (SelectionReason.UNKNOWN_SIZE, SelectionReason.STRUCTURE_ANCHOR), (_reference(),))


def _body() -> SnapshotDiagnosticBody:
    metrics = StructureMetrics(1, 10, 0, 0, 0, 0)
    hierarchy = HierarchyItem(_reference(), "managed", "", False, metrics, metrics, HierarchyPresentationState.EXPANDED, (SelectionReason.STRUCTURE_ANCHOR,), (_anchor().entry_reference,), (), _descriptor())
    duplicate = OverlayItem(OverlayKind.DUPLICATE_GROUP, _reference(ResultKind.DUPLICATE, "group-1"), 3, (_anchor().entry_reference,), 2, None, _descriptor(ResultKind.DUPLICATE))
    alias = OverlayItem(OverlayKind.HARD_LINK_ALIAS_SET, _reference(ResultKind.DUPLICATE, "alias-1"), 2, (_anchor().entry_reference,), 1, None, _descriptor(ResultKind.DUPLICATE))
    relation = OverlayItem(OverlayKind.RELATION_ITEM, _reference(ResultKind.RELATION, "relation-1"), 2, (_anchor().entry_reference,), 1, RelationCertainty.CANDIDATE, _descriptor(ResultKind.RELATION))
    return SnapshotDiagnosticBody((hierarchy,), (_anchor(),), (duplicate,), (alias,), (relation,))


def _facts() -> ProjectionPreDigest:
    identity = ProjectionSourceIdentity(primary_snapshot=_snapshot(), result_identities=(SourceResultIdentity(ResultKind.STRUCTURE, _snapshot(), "storage_structure", 0, "result-digest"),))
    return ProjectionPreDigest(
        ProjectionMode.SNAPSHOT_DIAGNOSTIC, SnapshotDiagnosticRequest(PRIMARY_ID), _policy(), identity,
        (SourcePlanItem(ResultKind.SNAPSHOT, SourcePlanState.REQUESTED_AND_PRESENT, SourceResultIdentity(ResultKind.SNAPSHOT, _snapshot())), SourcePlanItem(ResultKind.STRUCTURE, SourcePlanState.REQUESTED_AND_PRESENT, identity.result_identities[0])),
        _state(identity),
        (Accounting(AccountingDomain.SNAPSHOT_DIAGNOSTIC_ENTRY, 2, 1, 1, CoverageAvailability.COMPLETE, 10, 10, 0, 1),),
        (_descriptor(),), _body(),
    )


def test_complete_snapshot_diagnostic_readiness_and_digest_determinism() -> None:
    facts = _facts()
    assert finalize(facts).projection_digest == finalize(facts).projection_digest
    assert finalize(facts).projection_digest == "1bc63281844ae6685fd972be794b724aecb48cc10f885591699c08892c24d564"
    wire = machine_object(facts)
    assert wire["source_identity"]["primary_snapshot"]["snapshot_id"] == PRIMARY_ID
    assert wire["snapshot_diagnostic"]["hierarchy_items"][0]["presentation"] == "EXPANDED"
    assert wire["snapshot_diagnostic"]["duplicate_overlays"][0]["total_member_count"] == 3
    assert wire["diagnostic_state"]["allocation_state"] == "UNKNOWN"


def test_complete_pair_tracking_model_is_constructible_without_a_service() -> None:
    pair = _pair()
    identity = ProjectionSourceIdentity(snapshot_pair=pair)
    namespace = ResultNamespace(ResultKind.DIFF, pair)
    descriptor = ExpansionDescriptor((PRIMARY_ID, TARGET_ID), ResultKind.DIFF, namespace, limit=10)
    item = TrackingItem("managed", "a", SnapshotEntryReference(PRIMARY_ID, "managed", "a"), SnapshotEntryReference(TARGET_ID, "managed", "a"), ResultLocalReference(namespace, "diff-1"), None, (), SnapshotDiffChangeType.MODIFIED, ContentState.UNKNOWN, None, (SelectionReason.MODIFIED,), descriptor)
    facts = ProjectionPreDigest(ProjectionMode.PAIR_TRACKING, PairTrackingRequest(PRIMARY_ID, TARGET_ID), _policy(), identity, (SourcePlanItem(ResultKind.DIFF, SourcePlanState.REQUESTED_AND_PRESENT), SourcePlanItem(ResultKind.GROWTH, SourcePlanState.NOT_REQUESTED)), _state(identity), (Accounting(AccountingDomain.PAIR_TRACKING_LOCATION, 1, 1, 0), Accounting(AccountingDomain.GROWTH_REGULAR_LOCATION, 0, 0, 0, CoverageAvailability.UNAVAILABLE)), (descriptor,), None, PairTrackingBody((item,), (), PairTrackingGrowthHierarchyContext(SourcePlanState.NOT_REQUESTED, None, ()), ()))
    assert finalize(facts).facts.pair_tracking is not None


def test_snapshot_and_ordered_pair_source_identities_have_distinct_canonical_forms() -> None:
    snapshot_identity = ProjectionSourceIdentity(primary_snapshot=_snapshot())
    pair_identity = ProjectionSourceIdentity(snapshot_pair=_pair())
    snapshot_state = _state(snapshot_identity)
    pair_state = _state(pair_identity)
    assert snapshot_state.source_identity.primary_snapshot is not None
    assert pair_state.source_identity.snapshot_pair is not None
    assert pair_state.source_identity.snapshot_pair.base.snapshot_id == PRIMARY_ID
    assert pair_state.source_identity.snapshot_pair.target.snapshot_id == TARGET_ID


def test_diagnostic_state_is_typed_immutable_and_keeps_physical_boundaries_unknown() -> None:
    state = _state()
    assert state.metadata_coverage == CoverageAvailability.PARTIAL
    assert state.reclaimable_space_state.value == "UNKNOWN"
    with pytest.raises(FrozenInstanceError):
        state.unknown_size_count = 0  # type: ignore[misc]


def test_anchor_fact_groups_cover_known_unknown_unsupported_and_reused_payload() -> None:
    entry = SnapshotEntryReference(PRIMARY_ID, "managed", "a")
    known = ExplicitEntryAnchor(entry, EntrySourceSide.PRIMARY, FilesystemObjectType.REGULAR_FILE, None, EntrySizeFacts(EntrySizeState.KNOWN, 0), EntryPayloadFacts(PayloadFactState.VERIFIED, "sha256", 1, "d", PayloadObservationProvenance.REUSED_FROM_VERIFIED_SNAPSHOT, PRIMARY_ID), None, ())
    unknown = ExplicitEntryAnchor(entry, EntrySourceSide.PRIMARY, FilesystemObjectType.REGULAR_FILE, None, EntrySizeFacts(EntrySizeState.UNKNOWN, None), EntryPayloadFacts(PayloadFactState.UNKNOWN), None, ())
    unsupported = ExplicitEntryAnchor(entry, EntrySourceSide.PRIMARY, FilesystemObjectType.DIRECTORY, None, EntrySizeFacts(EntrySizeState.UNSUPPORTED, None), None, None, ())
    assert known.payload_facts is not None
    assert known.payload_facts.provenance == PayloadObservationProvenance.REUSED_FROM_VERIFIED_SNAPSHOT
    assert unknown.size_facts.state == EntrySizeState.UNKNOWN
    assert unsupported.payload_facts is None


def test_hierarchy_and_overlay_models_preserve_view_and_membership_boundaries() -> None:
    body = _body()
    item = body.hierarchy_items[0]
    assert item.direct_metrics.known_logical_bytes == 10
    assert item.recursive_metrics.regular_file_count == 1
    assert item.presentation == HierarchyPresentationState.EXPANDED
    assert body.duplicate_overlays[0].aggregate_member_count == 2
    assert body.hard_link_alias_overlays[0].kind == OverlayKind.HARD_LINK_ALIAS_SET
    assert body.relation_overlays[0].certainty == RelationCertainty.CANDIDATE


def test_all_accounting_domains_are_typed_and_known_byte_invariant_is_enforced() -> None:
    domains = tuple(
        Accounting(domain, 1, 0, 1, CoverageAvailability.PARTIAL)
        for domain in AccountingDomain
    )
    assert {item.domain for item in domains} == set(AccountingDomain)
    facts = _facts()
    invalid = ProjectionPreDigest(
        facts.mode, facts.normalized_request, facts.resolved_policy, facts.source_identity,
        facts.source_plan, facts.diagnostic_state,
        (Accounting(AccountingDomain.SNAPSHOT_DIAGNOSTIC_ENTRY, 1, 1, 0, CoverageAvailability.COMPLETE, 1, None, 0),),
        facts.expansion_descriptors, facts.snapshot_diagnostic,
    )
    with pytest.raises(ObservationProjectionInvariantError):
        canonical_projection(invalid)


def test_fact_groups_keep_unknown_distinct_from_absent_and_anchors_are_immutable() -> None:
    unknown = ExplicitEntryAnchor(SnapshotEntryReference(PRIMARY_ID, "managed", "unknown"), EntrySourceSide.PRIMARY, FilesystemObjectType.REGULAR_FILE, None, EntrySizeFacts(EntrySizeState.UNKNOWN, None), EntryPayloadFacts(PayloadFactState.UNKNOWN), None, (SelectionReason.PAYLOAD_UNKNOWN,))
    wire = machine_object(_facts())
    assert "payload_facts" in wire["snapshot_diagnostic"]["explicit_entry_anchors"][0]
    assert unknown.size_facts.state == EntrySizeState.UNKNOWN
    with pytest.raises(FrozenInstanceError):
        unknown.source_side = EntrySourceSide.BASE  # type: ignore[misc]


def test_mode_identity_and_accounting_invariants_fail() -> None:
    facts = _facts()
    invalid = ProjectionPreDigest(facts.mode, facts.normalized_request, facts.resolved_policy, ProjectionSourceIdentity(snapshot_pair=_pair()), facts.source_plan, _state(ProjectionSourceIdentity(snapshot_pair=_pair())), facts.accounting, facts.expansion_descriptors, facts.snapshot_diagnostic)
    with pytest.raises(ObservationProjectionInvariantError):
        canonical_projection(invalid)
    bad_accounting = Accounting(AccountingDomain.SNAPSHOT_DIAGNOSTIC_ENTRY, 1, 0, 0)
    invalid = ProjectionPreDigest(facts.mode, facts.normalized_request, facts.resolved_policy, facts.source_identity, facts.source_plan, facts.diagnostic_state, (bad_accounting,), facts.expansion_descriptors, facts.snapshot_diagnostic)
    with pytest.raises(ObservationProjectionInvariantError):
        canonical_projection(invalid)


def test_canonical_order_and_normalization_are_stable() -> None:
    request = PairTrackingRequest(PRIMARY_ID, TARGET_ID, path_prefix="a/b")
    assert normalize_request(normalize_request(request)) == request
    assert normalize_reasons((SelectionReason.UNKNOWN_SIZE, SelectionReason.ADDED, SelectionReason.UNKNOWN_SIZE)) == (SelectionReason.ADDED, SelectionReason.UNKNOWN_SIZE)
    assert canonical_projection(_facts()) == canonical_projection(_facts())


def test_canonical_order_is_independent_of_unordered_model_collections() -> None:
    facts = _facts()
    body = facts.snapshot_diagnostic
    assert body is not None
    reversed_body = SnapshotDiagnosticBody(
        body.hierarchy_items,
        body.explicit_entry_anchors,
        tuple(reversed(body.duplicate_overlays)),
        tuple(reversed(body.hard_link_alias_overlays)),
        tuple(reversed(body.relation_overlays)),
    )
    reordered = ProjectionPreDigest(
        facts.mode, facts.normalized_request, facts.resolved_policy, facts.source_identity,
        tuple(reversed(facts.source_plan)), facts.diagnostic_state,
        tuple(reversed(facts.accounting)), tuple(reversed(facts.expansion_descriptors)), reversed_body,
    )
    assert canonical_projection(reordered) == canonical_projection(facts)


def test_canonical_facts_are_digest_sensitive_but_not_runtime_sensitive() -> None:
    facts = _facts()
    changed = ProjectionPreDigest(
        facts.mode, SnapshotDiagnosticRequest(PRIMARY_ID, scope="managed"), facts.resolved_policy,
        facts.source_identity, facts.source_plan, facts.diagnostic_state, facts.accounting,
        facts.expansion_descriptors, facts.snapshot_diagnostic,
    )
    assert finalize(changed).projection_digest != finalize(facts).projection_digest
