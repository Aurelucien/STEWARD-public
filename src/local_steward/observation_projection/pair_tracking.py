"""Repository-backed, deterministic Pair Tracking Projection service."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, TypeAlias

from local_steward.errors import SnapshotNotFoundError
from local_steward.models import (
    FilesystemEntryV2,
    FilesystemObjectType,
    FilesystemObservationStatus,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    GrowthContribution,
    PayloadObservationStatus,
    RelationSet,
    SnapshotDiff,
    SnapshotDiffChangeType,
    SnapshotDiffItem,
    StewardConfig,
    StorageGrowthResult,
)
from local_steward.snapshot_diff import compute_verified_snapshot_diff
from local_steward.snapshot_relations import compute_verified_snapshot_relations
from local_steward.snapshots import get_snapshot, verify_snapshot
from local_steward.storage_growth import compute_verified_snapshot_growth

from .canonical import finalize
from .entry_facts import Entry, entry_reference, extract_entry_anchor
from .errors import ObservationProjectionInvariantError, ObservationProjectionRequestError
from .models import (
    Accounting,
    AccountingDomain,
    ChangeKindCount,
    ContentState,
    CoverageAvailability,
    DiagnosticBoundary,
    DiagnosticCount,
    DiagnosticState,
    EntrySourceSide,
    ExpansionDescriptor,
    ExplicitEntryAnchor,
    HierarchyPresentationState,
    ObservationProjection,
    OverlayItem,
    OverlayKind,
    PairTrackingBody,
    PairTrackingGrowthHierarchyContext,
    PairTrackingGrowthHierarchyItem,
    PairTrackingRequest,
    PathGrowthMetrics,
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
    TrackingItem,
)
from .pair_source_plan import PairTrackingSourcePlan, plan_pair_tracking_sources
from .validation import normalize_reasons, normalize_request, validate_budget


Snapshot: TypeAlias = FilesystemSnapshot | FilesystemSnapshotV2
Location: TypeAlias = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _Sources:
    base: Snapshot
    target: Snapshot
    base_entries: Mapping[Location, Entry]
    target_entries: Mapping[Location, Entry]
    pair_identity: SnapshotPairSourceIdentity
    diff: SnapshotDiff
    growth: StorageGrowthResult | None
    relation: RelationSet | None


@dataclass(frozen=True, slots=True)
class _Candidate:
    diff: SnapshotDiffItem
    reasons: tuple[SelectionReason, ...]
    contribution: GrowthContribution | None
    relation_references: tuple[ResultLocalReference, ...]

    @property
    def location(self) -> Location:
        return (self.diff.scope_id, self.diff.relative_path)

    @property
    def channel(self) -> int:
        if any(reason in {
            SelectionReason.ADDED, SelectionReason.REMOVED, SelectionReason.MODIFIED,
            SelectionReason.UNKNOWN_SIZE, SelectionReason.PAYLOAD_UNKNOWN,
            SelectionReason.CONTENT_CHANGED, SelectionReason.USER_REQUESTED_LOCATION,
        } for reason in self.reasons):
            return 0
        if any(reason in {
            SelectionReason.SIZE_INCREASE, SelectionReason.SIZE_DECREASE,
            SelectionReason.GROWTH_CONTRIBUTOR,
        } for reason in self.reasons):
            return 1
        if any(reason in {
            SelectionReason.AMBIGUOUS_RELATION,
            SelectionReason.RELATION_COMPONENT_REPRESENTATIVE,
            SelectionReason.TRANSITION_ENDPOINT,
        } for reason in self.reasons):
            return 2
        return 3


def _snapshot_identity(snapshot: Snapshot) -> SnapshotSourceIdentity:
    schema_version = snapshot.snapshot_schema_version if isinstance(snapshot, FilesystemSnapshotV2) else 1
    return SnapshotSourceIdentity(snapshot.snapshot_id, schema_version, snapshot.snapshot_digest)


def _load_verified_snapshot(config: StewardConfig, snapshot_id: str) -> Snapshot:
    try:
        verification = verify_snapshot(config, snapshot_id)
    except SnapshotNotFoundError as error:
        raise ObservationProjectionRequestError("SNAPSHOT_MISSING") from error
    except Exception as error:
        raise ObservationProjectionRequestError("SNAPSHOT_REPOSITORY_INVALID") from error
    if verification.status != "VALID":
        codes = {item["code"] for item in verification.errors}
        if any(code.startswith("PAYLOAD_REUSE_SOURCE") for code in codes):
            raise ObservationProjectionRequestError("SNAPSHOT_REUSE_SOURCE_INVALID")
        raise ObservationProjectionRequestError("SNAPSHOT_REPOSITORY_INVALID")
    try:
        return get_snapshot(config, snapshot_id)
    except SnapshotNotFoundError as error:
        raise ObservationProjectionRequestError("SNAPSHOT_MISSING") from error
    except Exception as error:
        raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT") from error


def _load_sources(
    config: StewardConfig, request: PairTrackingRequest, plan: PairTrackingSourcePlan
) -> _Sources:
    base = _load_verified_snapshot(config, request.base_snapshot_id)
    target = _load_verified_snapshot(config, request.target_snapshot_id)
    if base.snapshot_id == target.snapshot_id:
        raise ObservationProjectionRequestError("PAIR_SAME")
    if base.completed_at > target.started_at:
        raise ObservationProjectionRequestError("PAIR_TEMPORAL_INVALID")
    pair = SnapshotPairSourceIdentity(_snapshot_identity(base), _snapshot_identity(target))
    try:
        diff = compute_verified_snapshot_diff(config, base.snapshot_id, target.snapshot_id)
        growth = compute_verified_snapshot_growth(config, base.snapshot_id, target.snapshot_id) if plan.growth_requested else None
        relation = compute_verified_snapshot_relations(config, base.snapshot_id, target.snapshot_id) if plan.relation_requested else None
    except Exception as error:
        raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT") from error
    if diff.left_snapshot_id != base.snapshot_id or diff.right_snapshot_id != target.snapshot_id:
        raise ObservationProjectionInvariantError("SOURCE_RESULT_IDENTITY_MISMATCH")
    if growth is not None and (
        growth.base_snapshot_id != base.snapshot_id or growth.target_snapshot_id != target.snapshot_id
    ):
        raise ObservationProjectionInvariantError("SOURCE_RESULT_IDENTITY_MISMATCH")
    if relation is not None and (
        relation.base_snapshot_id != base.snapshot_id or relation.target_snapshot_id != target.snapshot_id
    ):
        raise ObservationProjectionInvariantError("SOURCE_RESULT_IDENTITY_MISMATCH")
    return _Sources(
        base,
        target,
        _entry_index(base),
        _entry_index(target),
        pair,
        diff,
        growth,
        relation,
    )


def _entry_index(snapshot: Snapshot) -> Mapping[Location, Entry]:
    values: dict[Location, Entry] = {}
    for entry in snapshot.entries:
        location = (entry.scope_id, entry.relative_path)
        if location in values:
            raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT")
        values[location] = entry
    return MappingProxyType(values)


def _path_selected(path: str, prefix: str | None) -> bool:
    return prefix is None or prefix == "." or path == prefix or path.startswith(prefix + "/")


def _selected_universe(sources: _Sources, request: PairTrackingRequest) -> tuple[SnapshotDiffItem, ...]:
    available_scopes = set(sources.base.scope_ids) | set(sources.target.scope_ids)
    if request.scope is not None and request.scope not in available_scopes:
        raise ObservationProjectionRequestError("SCOPE_UNKNOWN")
    scoped = tuple(
        item for item in sources.diff.items
        if request.scope is None or item.scope_id == request.scope
    )
    if request.path_prefix not in {None, "."}:
        selected = tuple(
            item for item in scoped if _path_selected(item.relative_path, request.path_prefix)
        )
        if not selected:
            raise ObservationProjectionRequestError("PATH_PREFIX_INVALID")
        scoped = selected
    seen: set[Location] = set()
    for item in scoped:
        location = (item.scope_id, item.relative_path)
        if location in seen:
            raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT")
        seen.add(location)
    return tuple(sorted(scoped, key=lambda item: (item.scope_id, item.relative_path.encode("utf-8", "surrogateescape"))))


def _result_identity(
    kind: ResultKind,
    identity: SnapshotPairSourceIdentity,
    algorithm: str | None = None,
    algorithm_version: int | None = None,
    digest: str | None = None,
) -> SourceResultIdentity:
    return SourceResultIdentity(kind, identity, algorithm, algorithm_version, digest)


def _namespace(
    kind: ResultKind, identity: SnapshotPairSourceIdentity, digest: str | None = None
) -> ResultNamespace:
    return ResultNamespace(kind, identity, digest)


def _diff_local_id(location: Location) -> str:
    return location[0] + chr(0) + location[1]


def _source_facts(
    sources: _Sources, plan: PairTrackingSourcePlan
) -> tuple[ProjectionSourceIdentity, tuple[SourcePlanItem, ...], dict[ResultKind, ResultNamespace]]:
    if (
        (sources.growth is not None) != plan.growth_requested
        or (sources.relation is not None) != plan.relation_requested
    ):
        raise ObservationProjectionInvariantError("UNREACHABLE_SOURCE_PLAN_STATE")
    snapshots = _result_identity(ResultKind.SNAPSHOT, sources.pair_identity)
    diff = _result_identity(ResultKind.DIFF, sources.pair_identity)
    identities = [snapshots, diff]
    items = [
        SourcePlanItem(ResultKind.SNAPSHOT, SourcePlanState.REQUESTED_AND_PRESENT, snapshots),
        SourcePlanItem(ResultKind.DIFF, SourcePlanState.REQUESTED_AND_PRESENT if sources.diff.items else SourcePlanState.REQUESTED_AND_EMPTY, diff),
    ]
    namespaces = {
        ResultKind.DIFF: _namespace(ResultKind.DIFF, sources.pair_identity),
    }
    if sources.growth is None:
        items.append(SourcePlanItem(ResultKind.GROWTH, SourcePlanState.NOT_REQUESTED))
    else:
        identity = _result_identity(
            ResultKind.GROWTH,
            sources.pair_identity,
            sources.growth.algorithm,
            sources.growth.algorithm_version,
            sources.growth.growth_digest,
        )
        identities.append(identity)
        state = SourcePlanState.REQUESTED_AND_PRESENT if sources.growth.path_nodes else SourcePlanState.REQUESTED_AND_EMPTY
        items.append(SourcePlanItem(ResultKind.GROWTH, state, identity))
        namespaces[ResultKind.GROWTH] = _namespace(ResultKind.GROWTH, sources.pair_identity, sources.growth.growth_digest)
    if sources.relation is None:
        items.append(SourcePlanItem(ResultKind.RELATION, SourcePlanState.NOT_REQUESTED))
    else:
        identity = _result_identity(
            ResultKind.RELATION,
            sources.pair_identity,
            sources.relation.algorithm,
            sources.relation.algorithm_version,
            sources.relation.relation_set_digest,
        )
        identities.append(identity)
        state = SourcePlanState.REQUESTED_AND_PRESENT if (
            sources.relation.relations or sources.relation.ambiguity_groups
        ) else SourcePlanState.REQUESTED_AND_EMPTY
        items.append(SourcePlanItem(ResultKind.RELATION, state, identity))
        namespaces[ResultKind.RELATION] = _namespace(ResultKind.RELATION, sources.pair_identity, sources.relation.relation_set_digest)
    items.append(SourcePlanItem(ResultKind.DUPLICATE, SourcePlanState.NOT_REQUESTED))
    return (
        ProjectionSourceIdentity(snapshot_pair=sources.pair_identity, result_identities=tuple(identities)),
        tuple(sorted(items, key=lambda item: item.result_kind.value)),
        namespaces,
    )


def _content_state(base: Entry | None, target: Entry | None) -> ContentState:
    if not isinstance(base, FilesystemEntryV2) or not isinstance(target, FilesystemEntryV2):
        return ContentState.UNKNOWN
    complete = {PayloadObservationStatus.HASHED, PayloadObservationStatus.EMPTY_FILE_HASHED}
    if base.payload_observation.status not in complete or target.payload_observation.status not in complete:
        return ContentState.UNKNOWN
    return (
        ContentState.VERIFIED_UNCHANGED
        if base.payload_observation.digest == target.payload_observation.digest
        else ContentState.VERIFIED_CHANGED
    )


def _source_entries(
    sources: _Sources, item: SnapshotDiffItem
) -> tuple[Entry | None, Entry | None]:
    """Return original verified Entry facts for one Diff location.

    SnapshotDiff intentionally compares v2 snapshots through its frozen stat
    view.  Pair Tracking keeps that Diff item as the authoritative location
    and change fact, but must obtain payload/provenance facts from the original
    verified Snapshot rather than that stat view.
    """
    location = (item.scope_id, item.relative_path)
    base = sources.base_entries.get(location)
    target = sources.target_entries.get(location)
    if item.left_entry is not None and base is None:
        raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT")
    if item.right_entry is not None and target is None:
        raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT")
    return base, target


def _growth_locations(
    sources: _Sources, universe: tuple[SnapshotDiffItem, ...]
) -> dict[Location, GrowthContribution]:
    if sources.growth is None:
        return {}
    all_locations = {(item.scope_id, item.relative_path) for item in sources.diff.items}
    result: dict[Location, GrowthContribution] = {}
    for contribution in sources.growth.contributions:
        locations = {(reference.scope_id, reference.relative_path) for reference in contribution.entry_references}
        if len(locations) != 1:
            raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT")
        location = next(iter(locations))
        if location not in all_locations or location in result:
            raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT")
        result[location] = contribution
    selected_locations = {(item.scope_id, item.relative_path) for item in universe}
    return {location: value for location, value in result.items() if location in selected_locations}


def _relation_maps(
    sources: _Sources,
    universe: tuple[SnapshotDiffItem, ...],
    namespaces: dict[ResultKind, ResultNamespace],
    policy: ProjectionPolicy,
) -> tuple[
    dict[Location, tuple[ResultLocalReference, ...]],
    dict[Location, tuple[SelectionReason, ...]],
    tuple[OverlayItem, ...],
]:
    if sources.relation is None:
        return {}, {}, ()
    if not isinstance(policy.budget.relation_component_total, int) or not isinstance(
        policy.budget.members_per_component, int
    ):
        raise ObservationProjectionRequestError("BUDGET_INVALID")
    namespace = namespaces[ResultKind.RELATION]
    selected_locations = {(item.scope_id, item.relative_path) for item in universe}
    refs: dict[Location, list[ResultLocalReference]] = {}
    reasons: dict[Location, list[SelectionReason]] = {}
    overlays: list[OverlayItem] = []
    member_limit = policy.budget.members_per_component
    components_left = policy.budget.relation_component_total
    for relation in sources.relation.relations:
        if components_left <= 0:
            break
        components_left -= 1
        local = ResultLocalReference(namespace, relation.relation_id)
        members = relation.source_entries + relation.target_entries
        selected = tuple(
            reference for reference in members
            if (reference.scope_id, reference.relative_path) in selected_locations
        )
        for reference in selected:
            location = (reference.scope_id, reference.relative_path)
            refs.setdefault(location, []).append(local)
            reasons.setdefault(location, []).extend((
                SelectionReason.RELATION_COMPONENT_REPRESENTATIVE,
                SelectionReason.TRANSITION_ENDPOINT,
            ))
        overlays.append(OverlayItem(
            OverlayKind.RELATION_ITEM,
            local,
            len(members),
            selected[:member_limit],
            len(members) - len(selected[:member_limit]),
            relation.certainty,
            ExpansionDescriptor(
                (sources.base.snapshot_id, sources.target.snapshot_id),
                ResultKind.RELATION,
                namespace,
                local_id=relation.relation_id,
            ),
        ))
    for group in sources.relation.ambiguity_groups:
        if components_left <= 0:
            break
        components_left -= 1
        local = ResultLocalReference(namespace, group.ambiguity_group_id)
        members = group.source_entries + group.target_entries
        selected = tuple(
            reference for reference in members
            if (reference.scope_id, reference.relative_path) in selected_locations
        )
        for reference in selected:
            location = (reference.scope_id, reference.relative_path)
            refs.setdefault(location, []).append(local)
            reasons.setdefault(location, []).extend((
                SelectionReason.AMBIGUOUS_RELATION,
                SelectionReason.RELATION_COMPONENT_REPRESENTATIVE,
            ))
        overlays.append(OverlayItem(
            OverlayKind.RELATION_AMBIGUITY_GROUP,
            local,
            len(members),
            selected[:member_limit],
            len(members) - len(selected[:member_limit]),
            None,
            ExpansionDescriptor(
                (sources.base.snapshot_id, sources.target.snapshot_id),
                ResultKind.RELATION,
                namespace,
                local_id=group.ambiguity_group_id,
            ),
        ))
    return (
        {
            location: tuple(sorted(set(values), key=lambda item: item.result_local_id))
            for location, values in refs.items()
        },
        {
            location: normalize_reasons(tuple(values))
            for location, values in reasons.items()
        },
        tuple(overlays),
    )


def _candidate_reasons(
    sources: _Sources,
    item: SnapshotDiffItem,
    request: PairTrackingRequest,
    contribution: GrowthContribution | None,
    relation_references: tuple[ResultLocalReference, ...],
    relation_reasons: tuple[SelectionReason, ...],
) -> tuple[SelectionReason, ...]:
    reasons: list[SelectionReason] = []
    mapping = {
        SnapshotDiffChangeType.ADDED: SelectionReason.ADDED,
        SnapshotDiffChangeType.REMOVED: SelectionReason.REMOVED,
        SnapshotDiffChangeType.MODIFIED: SelectionReason.MODIFIED,
    }
    if item.change_type in mapping:
        reasons.append(mapping[item.change_type])
    base, target = _source_entries(sources, item)
    for entry in (base, target):
        if entry is None:
            continue
        if entry.observation_status != FilesystemObservationStatus.OBSERVED:
            reasons.append(SelectionReason.OBSERVATION_FAILURE)
        if entry.excluded:
            reasons.append(SelectionReason.EXCLUDED)
        if not entry.readable:
            reasons.append(SelectionReason.UNREADABLE)
        if (
            entry.object_type == FilesystemObjectType.REGULAR_FILE
            and not entry.excluded
            and entry.size_bytes is None
        ):
            reasons.append(SelectionReason.UNKNOWN_SIZE)
        if isinstance(entry, FilesystemEntryV2):
            if entry.payload_observation.status == PayloadObservationStatus.NOT_LOCAL:
                reasons.append(SelectionReason.NON_LOCAL)
            elif entry.payload_observation.status not in {
                PayloadObservationStatus.HASHED,
                PayloadObservationStatus.EMPTY_FILE_HASHED,
                PayloadObservationStatus.NOT_REGULAR_FILE,
            }:
                reasons.append(SelectionReason.PAYLOAD_UNKNOWN)
    if _content_state(base, target) == ContentState.VERIFIED_CHANGED:
        reasons.append(SelectionReason.CONTENT_CHANGED)
    if request.scope is not None or request.path_prefix is not None:
        reasons.append(SelectionReason.USER_REQUESTED_LOCATION)
    if contribution is not None:
        if contribution.known_byte_delta is None:
            reasons.append(SelectionReason.UNKNOWN_SIZE)
        elif contribution.known_byte_delta > 0:
            reasons.extend((SelectionReason.SIZE_INCREASE, SelectionReason.GROWTH_CONTRIBUTOR))
        elif contribution.known_byte_delta < 0:
            reasons.extend((SelectionReason.SIZE_DECREASE, SelectionReason.GROWTH_CONTRIBUTOR))
    reasons.extend(relation_reasons)
    return normalize_reasons(tuple(reasons))


def _select_candidates(
    candidates: tuple[_Candidate, ...], policy: ProjectionPolicy
) -> tuple[_Candidate, ...]:
    if not isinstance(policy.budget.tracking_item_total, int):
        raise ObservationProjectionRequestError("BUDGET_INVALID")
    total = policy.budget.tracking_item_total
    if total == 0:
        return ()
    by_location = {candidate.location: candidate for candidate in candidates}
    if len(by_location) != len(candidates):
        raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT")
    ordered = tuple(sorted(
        candidates,
        key=lambda item: (
            item.channel,
            item.diff.scope_id,
            item.diff.relative_path.encode("utf-8", "surrogateescape"),
        ),
    ))
    quotas = {
        key: value for key, value in policy.budget.priority_quotas if isinstance(value, int)
    }
    names = (
        "DIAGNOSTIC_BOUNDARY", "TRACKING_FACT", "RELATIONSHIP_ANCHOR",
        "STRUCTURE_CAPACITY_CONTEXT",
    )
    selected: list[_Candidate] = []
    counts: dict[int, int] = {}
    scope_minimum = policy.budget.scope_minimum_guarantee
    if isinstance(scope_minimum, int) and scope_minimum > 0:
        for scope_id in sorted({item.diff.scope_id for item in ordered}):
            scope_items = [item for item in ordered if item.diff.scope_id == scope_id]
            for item in scope_items[:scope_minimum]:
                if len(selected) >= total:
                    return tuple(selected)
                selected.append(item)
                counts[item.channel] = counts.get(item.channel, 0) + 1
    for item in ordered:
        if item in selected or len(selected) >= total:
            continue
        quota = quotas.get(names[item.channel])
        if quota is not None and counts.get(item.channel, 0) >= quota:
            continue
        selected.append(item)
        counts[item.channel] = counts.get(item.channel, 0) + 1
    return tuple(selected)


def _tracking_descriptor(
    sources: _Sources, namespace: ResultNamespace, item: SnapshotDiffItem
) -> ExpansionDescriptor:
    return ExpansionDescriptor(
        (sources.base.snapshot_id, sources.target.snapshot_id),
        ResultKind.DIFF,
        namespace,
        scope=item.scope_id,
        path_prefix=item.relative_path,
        local_id=_diff_local_id((item.scope_id, item.relative_path)),
    )


def _tracking_items(
    sources: _Sources,
    selected: tuple[_Candidate, ...],
    namespaces: dict[ResultKind, ResultNamespace],
) -> tuple[TrackingItem, ...]:
    diff_namespace = namespaces[ResultKind.DIFF]
    growth_namespace = namespaces.get(ResultKind.GROWTH)
    values: list[TrackingItem] = []
    for candidate in selected:
        item = candidate.diff
        contribution = candidate.contribution
        base, target = _source_entries(sources, item)
        values.append(TrackingItem(
            item.scope_id,
            item.relative_path,
            entry_reference(base) if base is not None else None,
            entry_reference(target) if target is not None else None,
            ResultLocalReference(diff_namespace, _diff_local_id((item.scope_id, item.relative_path))),
            (
                ResultLocalReference(growth_namespace, contribution.growth_contribution_id)
                if contribution is not None and growth_namespace is not None
                else None
            ),
            candidate.relation_references,
            item.change_type,
            _content_state(base, target),
            contribution.known_byte_delta if contribution is not None else None,
            candidate.reasons,
            _tracking_descriptor(sources, diff_namespace, item),
        ))
    return tuple(values)


def _anchors(
    sources: _Sources, selected: tuple[_Candidate, ...], policy: ProjectionPolicy
) -> tuple[ExplicitEntryAnchor, ...]:
    if not isinstance(policy.budget.explicit_entry_total, int):
        raise ObservationProjectionRequestError("BUDGET_INVALID")
    values = []
    for candidate in selected:
        base, target = _source_entries(sources, candidate.diff)
        for side, entry in (
            (EntrySourceSide.BASE, base),
            (EntrySourceSide.TARGET, target),
        ):
            if entry is None:
                continue
            values.append(extract_entry_anchor(
                entry,
                source_side=side,
                reasons=candidate.reasons,
                result_references=candidate.relation_references,
            ))
            if len(values) >= policy.budget.explicit_entry_total:
                return tuple(values)
    return tuple(values)


def _metrics(node, recursive: bool) -> PathGrowthMetrics:  # type: ignore[no-untyped-def]
    prefix = "recursive" if recursive else "direct"
    return PathGrowthMetrics(
        getattr(node, f"{prefix}_base_known_logical_bytes"),
        getattr(node, f"{prefix}_target_known_logical_bytes"),
        getattr(node, f"{prefix}_known_net_logical_delta"),
        getattr(node, f"{prefix}_added_logical_bytes"),
        getattr(node, f"{prefix}_added_location_count"),
        getattr(node, f"{prefix}_removed_logical_bytes"),
        getattr(node, f"{prefix}_removed_location_count"),
        getattr(node, f"{prefix}_same_location_increase_bytes"),
        getattr(node, f"{prefix}_same_location_increase_count"),
        getattr(node, f"{prefix}_same_location_decrease_bytes"),
        getattr(node, f"{prefix}_same_location_decrease_count"),
        getattr(node, f"{prefix}_same_location_unchanged_count"),
        getattr(node, f"{prefix}_unknown_size_contribution_count"),
        node.decomposition_complete,
    )


def _hierarchy(
    sources: _Sources,
    request: PairTrackingRequest,
    selected: tuple[_Candidate, ...],
    namespaces: dict[ResultKind, ResultNamespace],
    policy: ProjectionPolicy,
) -> tuple[PairTrackingGrowthHierarchyContext, Accounting | None]:
    if sources.growth is None:
        return PairTrackingGrowthHierarchyContext(SourcePlanState.NOT_REQUESTED, None, ()), None
    growth_identity = _result_identity(
        ResultKind.GROWTH, sources.pair_identity, sources.growth.algorithm,
        sources.growth.algorithm_version, sources.growth.growth_digest,
    )
    namespace = namespaces[ResultKind.GROWTH]
    scopes = {item.diff.scope_id for item in selected}
    if not scopes:
        scopes = ({request.scope} if request.scope is not None else set(sources.base.scope_ids) | set(sources.target.scope_ids))
    candidates = {
        (node.scope_id, node.relative_directory_path): node
        for node in sources.growth.path_nodes
        if node.scope_id in scopes and (
            request.path_prefix in {None, "."}
            or _path_selected(node.relative_directory_path, request.path_prefix)
            or node.relative_directory_path == "."
        )
    }
    required: set[Location] = set()
    for scope_id in scopes:
        if (scope_id, ".") in candidates:
            required.add((scope_id, "."))
    for candidate in selected:
        parent = candidate.diff.relative_path.rsplit("/", 1)[0] if "/" in candidate.diff.relative_path else "."
        while True:
            key = (candidate.diff.scope_id, parent)
            if key in candidates:
                required.add(key)
            if parent == ".":
                break
            parent = parent.rsplit("/", 1)[0] if "/" in parent else "."
    if not candidates:
        return PairTrackingGrowthHierarchyContext(SourcePlanState.REQUESTED_AND_EMPTY, growth_identity, ()), Accounting(
            AccountingDomain.PAIR_TRACKING_GROWTH_HIERARCHY, 0, 0, 0
        )
    if not isinstance(policy.budget.hierarchy_node_total, int) or policy.budget.hierarchy_node_total < len(required):
        raise ObservationProjectionInvariantError("ACCOUNTING_DECOMPOSITION_UNSUPPORTED")
    ordered_optional = sorted(
        (node for key, node in candidates.items() if key not in required),
        key=lambda node: (
            -abs(node.recursive_known_net_logical_delta),
            node.scope_id,
            node.relative_directory_path.encode("utf-8", "surrogateescape"),
        ),
    )
    kept = list(required)
    for node in ordered_optional:
        if len(kept) >= policy.budget.hierarchy_node_total:
            break
        kept.append((node.scope_id, node.relative_directory_path))
    tracking_locations = {candidate.location for candidate in selected}
    values = []
    for key in sorted(kept, key=lambda item: (item[0], item[1].encode("utf-8", "surrogateescape"))):
        node = candidates[key]
        expanded = any(
            scope_id == node.scope_id and _path_selected(path, node.relative_directory_path)
            for scope_id, path in tracking_locations
        )
        values.append(PairTrackingGrowthHierarchyItem(
            ResultLocalReference(namespace, node.growth_node_id),
            node.scope_id,
            node.relative_directory_path,
            _metrics(node, False),
            _metrics(node, True),
            HierarchyPresentationState.EXPANDED if expanded else HierarchyPresentationState.FOLDED,
            (SelectionReason.GROWTH_CONTRIBUTOR,),
            ExpansionDescriptor(
                (sources.base.snapshot_id, sources.target.snapshot_id),
                ResultKind.GROWTH,
                namespace,
                scope=node.scope_id,
                path_prefix=node.relative_directory_path,
                local_id=node.growth_node_id,
            ),
        ))
    state = SourcePlanState.REQUESTED_AND_PRESENT
    availability = (
        CoverageAvailability.COMPLETE
        if all(node.decomposition_complete for node in candidates.values())
        else CoverageAvailability.PARTIAL
    )
    return PairTrackingGrowthHierarchyContext(state, growth_identity, tuple(values)), Accounting(
        AccountingDomain.PAIR_TRACKING_GROWTH_HIERARCHY,
        len(candidates),
        len(values),
        len(candidates) - len(values),
        availability,
    )


def _accounting(
    universe: tuple[SnapshotDiffItem, ...],
    selected: tuple[_Candidate, ...],
    growth: dict[Location, GrowthContribution],
    relation: RelationSet | None,
    relation_overlays: tuple[OverlayItem, ...],
    hierarchy: Accounting | None,
) -> tuple[Accounting, ...]:
    explicit_locations = {candidate.location for candidate in selected}
    changes = tuple(
        ChangeKindCount(kind, sum(item.change_type == kind for item in universe))
        for kind in sorted(SnapshotDiffChangeType, key=lambda item: item.value)
        if any(item.change_type == kind for item in universe)
    )
    values = [Accounting(
        AccountingDomain.PAIR_TRACKING_LOCATION,
        len(universe),
        len(explicit_locations),
        len(universe) - len(explicit_locations),
        CoverageAvailability.COMPLETE,
        change_kind_counts=changes,
    )]
    if growth:
        attached = {location for location in growth if location in explicit_locations}
        values.append(Accounting(
            AccountingDomain.GROWTH_REGULAR_LOCATION,
            len(growth),
            len(attached),
            len(growth) - len(attached),
            CoverageAvailability.PARTIAL if any(item.known_byte_delta is None for item in growth.values()) else CoverageAvailability.COMPLETE,
            unknown_size_count=sum(item.known_byte_delta is None for item in growth.values()),
        ))
    else:
        values.append(Accounting(
            AccountingDomain.GROWTH_REGULAR_LOCATION, 0, 0, 0,
            CoverageAvailability.UNAVAILABLE,
        ))
    if relation is not None:
        source_count = len(relation.relations) + len(relation.ambiguity_groups)
        certainty_counts = tuple(
            DiagnosticCount(certainty.value, sum(item.certainty == certainty for item in relation.relations))
            for certainty in sorted({item.certainty for item in relation.relations}, key=lambda item: item.value)
        )
        members = sum(
            len(item.source_entries) + len(item.target_entries)
            for item in relation.relations
        ) + sum(
            len(item.source_entries) + len(item.target_entries)
            for item in relation.ambiguity_groups
        )
        values.append(Accounting(
            AccountingDomain.RELATION_OVERLAY,
            source_count,
            len(relation_overlays),
            source_count - len(relation_overlays),
            CoverageAvailability.COMPLETE,
            conflict_counts=certainty_counts,
            overlay_member_count=members,
        ))
    if hierarchy is not None:
        values.append(hierarchy)
    return tuple(values)


def _diagnostic_state(
    sources: _Sources,
    identity: ProjectionSourceIdentity,
    source_plan: tuple[SourcePlanItem, ...],
    universe: tuple[SnapshotDiffItem, ...],
) -> DiagnosticState:
    entries = tuple(
        entry
        for item in universe
        for entry in _source_entries(sources, item)
        if entry is not None
    )
    regular = tuple(entry for entry in entries if entry.object_type == FilesystemObjectType.REGULAR_FILE and not entry.excluded)
    metadata_failure = sum(entry.observation_status != FilesystemObservationStatus.OBSERVED for entry in entries)
    unknown_size = sum(entry.size_bytes is None for entry in regular)
    v2_regular: tuple[FilesystemEntryV2, ...] = tuple(
        entry for entry in regular if isinstance(entry, FilesystemEntryV2)
    )
    hashed = {PayloadObservationStatus.HASHED, PayloadObservationStatus.EMPTY_FILE_HASHED}
    payload = (
        CoverageAvailability.UNAVAILABLE if not regular or not v2_regular
        else CoverageAvailability.COMPLETE if len(v2_regular) == len(regular) and all(
            entry.payload_observation.status in hashed for entry in v2_regular
        ) else CoverageAvailability.PARTIAL
    )
    relation_state = next(item.state for item in source_plan if item.result_kind == ResultKind.RELATION)
    limitations = []
    if sources.growth is not None and not sources.growth.coverage.decomposition_complete:
        limitations.append(DiagnosticBoundary("GROWTH_DECOMPOSITION_PARTIAL"))
    return DiagnosticState(
        ProjectionSourceValidity.VALID,
        identity,
        CoverageAvailability.COMPLETE if metadata_failure == 0 else CoverageAvailability.PARTIAL,
        CoverageAvailability.COMPLETE if unknown_size == 0 else CoverageAvailability.PARTIAL,
        payload,
        (),
        tuple(limitations),
        unknown_size,
        metadata_failure,
        sum(entry.observation_status == FilesystemObservationStatus.PERMISSION_DENIED for entry in entries),
        sum(entry.excluded for entry in entries),
        sum(not entry.readable for entry in entries),
        sum(
            isinstance(entry, FilesystemEntryV2)
            and entry.payload_observation.status == PayloadObservationStatus.NOT_LOCAL
            for entry in entries
        ),
        relation_state,
        None,
        sources.growth.coverage.known_net_logical_delta if sources.growth is not None else None,
    )


def build_pair_tracking_projection(
    config: StewardConfig, request: PairTrackingRequest, policy: ProjectionPolicy
) -> ObservationProjection:
    """Build one complete immutable Pair Tracking Projection without writes."""
    normalized = normalize_request(request)
    if not isinstance(normalized, PairTrackingRequest):
        raise ObservationProjectionRequestError("MODE_UNSUPPORTED")
    if policy.policy_schema_version != 0:
        raise ObservationProjectionRequestError("POLICY_VERSION_UNSUPPORTED")
    validate_budget(policy.budget)
    plan = plan_pair_tracking_sources(normalized, policy)
    sources = _load_sources(config, normalized, plan)
    universe = _selected_universe(sources, normalized)
    identity, source_plan, namespaces = _source_facts(sources, plan)
    growth = _growth_locations(sources, universe)
    relation_refs, relation_reasons, overlays = _relation_maps(sources, universe, namespaces, policy)
    candidates = tuple(
        _Candidate(
            item,
            _candidate_reasons(
                sources,
                item,
                normalized,
                growth.get((item.scope_id, item.relative_path)),
                relation_refs.get((item.scope_id, item.relative_path), ()),
                relation_reasons.get((item.scope_id, item.relative_path), ()),
            ),
            growth.get((item.scope_id, item.relative_path)),
            relation_refs.get((item.scope_id, item.relative_path), ()),
        )
        for item in universe
    )
    selected = _select_candidates(candidates, policy)
    tracking = _tracking_items(sources, selected, namespaces)
    anchors = _anchors(sources, selected, policy)
    hierarchy, hierarchy_accounting = _hierarchy(sources, normalized, selected, namespaces, policy)
    descriptors = tuple(
        sorted(
            tuple(item.expansion_descriptor for item in tracking)
            + tuple(item.expansion_descriptor for item in hierarchy.hierarchy_items)
            + tuple(item.expansion_descriptor for item in overlays),
            key=lambda item: (
                item.result_kind.value,
                item.scope or "",
                (item.path_prefix or "").encode("utf-8", "surrogateescape"),
                item.local_id or "",
            ),
        )
    )
    facts = ProjectionPreDigest(
        ProjectionMode.PAIR_TRACKING,
        normalized,
        policy,
        identity,
        source_plan,
        _diagnostic_state(sources, identity, source_plan, universe),
        _accounting(
            universe, selected, growth, sources.relation, overlays, hierarchy_accounting
        ),
        descriptors,
        None,
        PairTrackingBody(tracking, anchors, hierarchy, overlays),
    )
    return finalize(facts)
