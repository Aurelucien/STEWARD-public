"""Repository-backed, deterministic Snapshot Diagnostic Projection service."""

from dataclasses import dataclass

from local_steward.duplicate_analysis import compute_verified_snapshot_duplicate_analysis
from local_steward.errors import SnapshotNotFoundError
from local_steward.models import (
    DuplicateAnalysisResult,
    FilesystemEntryV2,
    FilesystemObjectType,
    FilesystemObservationStatus,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    PathAggregateNode,
    PayloadObservationStatus,
    RelationCertainty,
    RelationSet,
    SnapshotEntryReference,
    StewardConfig,
    StorageStructureResult,
)
from local_steward.snapshot_relations import compute_verified_snapshot_relations
from local_steward.snapshots import get_snapshot, verify_snapshot
from local_steward.storage_structure import compute_verified_snapshot_structure

from .canonical import finalize
from .entry_facts import Entry, entry_reference, extract_entry_anchor
from .errors import ObservationProjectionInvariantError, ObservationProjectionRequestError
from .models import (
    Accounting,
    AccountingDomain,
    CoverageAvailability,
    DiagnosticBoundary,
    DiagnosticState,
    EntrySourceSide,
    ExpansionDescriptor,
    HierarchyItem,
    HierarchyPresentationState,
    ObjectKindCount,
    ObservationProjection,
    OverlayItem,
    OverlayKind,
    ProjectionMode,
    ProjectionPolicy,
    ProjectionPreDigest,
    ProjectionSourceIdentity,
    ProjectionSourceValidity,
    ResultKind,
    ResultLocalReference,
    ResultNamespace,
    SelectionReason,
    SnapshotDiagnosticBody,
    SnapshotDiagnosticRequest,
    SnapshotPairSourceIdentity,
    SnapshotSourceIdentity,
    SourcePlanItem,
    SourcePlanState,
    SourceResultIdentity,
    StructureMetrics,
)
from .selection import SelectionCandidate, select_candidates
from .source_plan import SnapshotDiagnosticSourcePlan, plan_snapshot_diagnostic_sources
from .validation import normalize_request, validate_budget


Snapshot = FilesystemSnapshot | FilesystemSnapshotV2


@dataclass(frozen=True, slots=True)
class _LoadedSources:
    primary: Snapshot
    primary_identity: SnapshotSourceIdentity
    structure: StorageStructureResult | None
    duplicate: DuplicateAnalysisResult | None
    relation: RelationSet | None
    relation_pair: SnapshotPairSourceIdentity | None


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
    config: StewardConfig, request: SnapshotDiagnosticRequest, plan: SnapshotDiagnosticSourcePlan
) -> _LoadedSources:
    primary = _load_verified_snapshot(config, request.primary_snapshot_id)
    primary_identity = _snapshot_identity(primary)
    relation_pair: SnapshotPairSourceIdentity | None = None
    if plan.relation_requested:
        if plan.relation_pair is None:
            raise ObservationProjectionInvariantError("UNREACHABLE_SOURCE_PLAN_STATE")
        base_id, target_id = plan.relation_pair
        base = _load_verified_snapshot(config, base_id)
        target = _load_verified_snapshot(config, target_id)
        if base.snapshot_id == target.snapshot_id:
            raise ObservationProjectionRequestError("PAIR_SAME")
        if base.created_at >= target.created_at:
            raise ObservationProjectionRequestError("PAIR_TEMPORAL_INVALID")
        relation_pair = SnapshotPairSourceIdentity(_snapshot_identity(base), _snapshot_identity(target))
    try:
        structure = compute_verified_snapshot_structure(config, primary.snapshot_id) if plan.structure_requested else None
        duplicate = compute_verified_snapshot_duplicate_analysis(config, primary.snapshot_id) if plan.duplicate_requested else None
        relation = (
            compute_verified_snapshot_relations(config, plan.relation_pair[0], plan.relation_pair[1])
            if plan.relation_requested and plan.relation_pair is not None
            else None
        )
    except Exception as error:
        raise ObservationProjectionInvariantError("V2A_RESULT_INCONSISTENT") from error
    return _LoadedSources(primary, primary_identity, structure, duplicate, relation, relation_pair)


def _path_selected(path: str, prefix: str | None) -> bool:
    return prefix is None or prefix == "." or path == prefix or path.startswith(prefix + "/")


def _universe(snapshot: Snapshot, request: SnapshotDiagnosticRequest) -> tuple[Entry, ...]:
    if request.scope is not None and request.scope not in snapshot.scope_ids:
        raise ObservationProjectionRequestError("SCOPE_UNKNOWN")
    scoped = tuple(entry for entry in snapshot.entries if request.scope is None or entry.scope_id == request.scope)
    if request.path_prefix is not None and request.path_prefix != ".":
        selected = tuple(entry for entry in scoped if _path_selected(entry.relative_path, request.path_prefix))
        if not selected:
            raise ObservationProjectionRequestError("PATH_PREFIX_INVALID")
        return selected
    return scoped


def _namespace(kind: ResultKind, identity: SnapshotSourceIdentity | SnapshotPairSourceIdentity, digest: str | None) -> ResultNamespace:
    return ResultNamespace(kind, identity, digest)


def _result_identity(
    kind: ResultKind,
    identity: SnapshotSourceIdentity | SnapshotPairSourceIdentity,
    algorithm: str | None = None,
    algorithm_version: int | None = None,
    digest: str | None = None,
) -> SourceResultIdentity:
    return SourceResultIdentity(kind, identity, algorithm, algorithm_version, digest)


def _source_facts(sources: _LoadedSources) -> tuple[ProjectionSourceIdentity, tuple[SourcePlanItem, ...]]:
    identities = [_result_identity(ResultKind.SNAPSHOT, sources.primary_identity, digest=sources.primary.snapshot_digest)]
    items = [SourcePlanItem(ResultKind.SNAPSHOT, SourcePlanState.REQUESTED_AND_PRESENT, identities[0])]
    if sources.structure is not None:
        identity = _result_identity(ResultKind.STRUCTURE, sources.primary_identity, sources.structure.algorithm, sources.structure.algorithm_version, sources.structure.structure_digest)
        identities.append(identity)
        items.append(SourcePlanItem(ResultKind.STRUCTURE, SourcePlanState.REQUESTED_AND_PRESENT if sources.structure.path_nodes else SourcePlanState.REQUESTED_AND_EMPTY, identity))
    else:
        items.append(SourcePlanItem(ResultKind.STRUCTURE, SourcePlanState.NOT_REQUESTED))
    if sources.duplicate is not None:
        identity = _result_identity(ResultKind.DUPLICATE, sources.primary_identity, sources.duplicate.algorithm, sources.duplicate.algorithm_version, sources.duplicate.analysis_digest)
        identities.append(identity)
        state = SourcePlanState.REQUESTED_AND_PRESENT if (sources.duplicate.payload_equality_groups or sources.duplicate.hard_link_alias_sets) else SourcePlanState.REQUESTED_AND_EMPTY
        items.append(SourcePlanItem(ResultKind.DUPLICATE, state, identity))
    else:
        items.append(SourcePlanItem(ResultKind.DUPLICATE, SourcePlanState.NOT_REQUESTED))
    if sources.relation is not None and sources.relation_pair is not None:
        identity = _result_identity(ResultKind.RELATION, sources.relation_pair, sources.relation.algorithm, sources.relation.algorithm_version, sources.relation.relation_set_digest)
        identities.append(identity)
        state = SourcePlanState.REQUESTED_AND_PRESENT if (sources.relation.relations or sources.relation.ambiguity_groups) else SourcePlanState.REQUESTED_AND_EMPTY
        items.append(SourcePlanItem(ResultKind.RELATION, state, identity))
    else:
        items.append(SourcePlanItem(ResultKind.RELATION, SourcePlanState.NOT_REQUESTED))
    return ProjectionSourceIdentity(primary_snapshot=sources.primary_identity, result_identities=tuple(identities)), tuple(sorted(items, key=lambda item: item.result_kind.value))


def _entry_reasons(entry: Entry, request: SnapshotDiagnosticRequest) -> tuple[SelectionReason, ...]:
    reasons: list[SelectionReason] = []
    if entry.observation_status != FilesystemObservationStatus.OBSERVED:
        reasons.append(SelectionReason.METADATA_FAILURE)
        if entry.observation_status == FilesystemObservationStatus.PERMISSION_DENIED:
            reasons.append(SelectionReason.ACCESS_FAILURE)
    if entry.object_type == FilesystemObjectType.REGULAR_FILE and entry.size_bytes is None:
        reasons.append(SelectionReason.UNKNOWN_SIZE)
    if entry.excluded:
        reasons.append(SelectionReason.EXCLUDED)
    if not entry.readable:
        reasons.append(SelectionReason.UNREADABLE)
    if isinstance(entry, FilesystemEntryV2):
        status = entry.payload_observation.status
        if status == PayloadObservationStatus.NOT_LOCAL:
            reasons.append(SelectionReason.NON_LOCAL)
        elif entry.object_type == FilesystemObjectType.REGULAR_FILE and status not in {PayloadObservationStatus.HASHED, PayloadObservationStatus.EMPTY_FILE_HASHED}:
            reasons.append(SelectionReason.PAYLOAD_UNKNOWN)
        if entry.payload_observation.provenance is not None:
            reasons.append(SelectionReason.REUSE_PROVENANCE)
    if request.scope is not None or request.path_prefix is not None:
        reasons.append(SelectionReason.USER_REQUESTED_LOCATION)
    return tuple(sorted(set(reasons), key=lambda item: item.value))


def _representative_candidates(
    universe: tuple[Entry, ...], sources: _LoadedSources, request: SnapshotDiagnosticRequest
) -> tuple[SelectionCandidate, ...]:
    by_reference = {entry_reference(entry): entry for entry in universe}
    values = [SelectionCandidate(entry, _entry_reasons(entry, request)) for entry in universe if _entry_reasons(entry, request)]
    if sources.structure is not None:
        namespace = _namespace(ResultKind.STRUCTURE, sources.primary_identity, sources.structure.structure_digest)
        root_ids = {
            node.scope_id: node.path_node_id
            for node in sources.structure.path_nodes
            if node.relative_directory_path == "."
        }
        largest = sorted(
            (entry for entry in universe if entry.object_type == FilesystemObjectType.REGULAR_FILE and not entry.excluded and entry.size_bytes is not None),
            key=lambda entry: (-(entry.size_bytes or 0), entry.scope_id, entry.relative_path.encode("utf-8", "surrogateescape")),
        )
        for entry in largest:
            root_id = root_ids.get(entry.scope_id)
            references = () if root_id is None else (ResultLocalReference(namespace, root_id),)
            values.append(SelectionCandidate(entry, (SelectionReason.LOGICAL_BYTE_CONTRIBUTOR,), references))
    if sources.duplicate is not None:
        namespace = _namespace(ResultKind.DUPLICATE, sources.primary_identity, sources.duplicate.analysis_digest)
        for group in sources.duplicate.payload_equality_groups:
            member = next((item for item in group.member_entries if item in by_reference), None)
            if member is not None:
                values.append(SelectionCandidate(by_reference[member], (SelectionReason.DUPLICATE_REPRESENTATIVE,), (ResultLocalReference(namespace, group.payload_group_id),), True))
        for alias in sources.duplicate.hard_link_alias_sets:
            member = next((item for item in alias.member_entries if item in by_reference), None)
            if member is not None:
                values.append(SelectionCandidate(by_reference[member], (SelectionReason.HARD_LINK_REPRESENTATIVE,), (ResultLocalReference(namespace, alias.alias_set_id),), True))
    if sources.relation is not None and sources.relation_pair is not None:
        namespace = _namespace(ResultKind.RELATION, sources.relation_pair, sources.relation.relation_set_digest)
        for relation in sources.relation.relations:
            relevant = tuple(reference for reference in relation.source_entries + relation.target_entries if reference in by_reference)
            reason = SelectionReason.AMBIGUOUS_RELATION if relation.certainty == RelationCertainty.AMBIGUOUS else SelectionReason.RELATION_COMPONENT_REPRESENTATIVE
            for reference in relevant[:1]:
                values.append(SelectionCandidate(by_reference[reference], (reason,), (ResultLocalReference(namespace, relation.relation_id),), True))
    return tuple(values)


def _metrics(node: PathAggregateNode, recursive: bool) -> StructureMetrics:
    prefix = "recursive" if recursive else "direct"
    return StructureMetrics(
        getattr(node, f"{prefix}_regular_file_count"),
        getattr(node, f"{prefix}_known_logical_bytes"),
        getattr(node, f"{prefix}_unknown_size_regular_count"),
        getattr(node, f"{prefix}_directory_count"),
        getattr(node, f"{prefix}_symlink_count"),
        getattr(node, f"{prefix}_special_object_count"),
    )


def _hierarchy(
    sources: _LoadedSources, request: SnapshotDiagnosticRequest, selected: tuple[SelectionCandidate, ...], policy: ProjectionPolicy
) -> tuple[HierarchyItem, ...]:
    if sources.structure is None:
        return ()
    if not isinstance(policy.budget.hierarchy_node_total, int):
        raise ObservationProjectionRequestError("BUDGET_INVALID")
    namespace = _namespace(ResultKind.STRUCTURE, sources.primary_identity, sources.structure.structure_digest)
    nodes = {(node.scope_id, node.relative_directory_path): node for node in sources.structure.path_nodes}
    required: set[tuple[str, str]] = set()
    scopes = (request.scope,) if request.scope is not None else sources.primary.scope_ids
    for scope_id in scopes:
        if (scope_id, ".") in nodes:
            required.add((scope_id, "."))
    for candidate in selected:
        path = candidate.entry.relative_path
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        while True:
            if (candidate.entry.scope_id, parent) in nodes:
                required.add((candidate.entry.scope_id, parent))
            if parent == ".":
                break
            parent = parent.rsplit("/", 1)[0] if "/" in parent else "."
    if len(required) > policy.budget.hierarchy_node_total:
        raise ObservationProjectionInvariantError("ACCOUNTING_DECOMPOSITION_UNSUPPORTED")
    optional = sorted(
        (node for key, node in nodes.items() if key not in required and (request.scope is None or node.scope_id == request.scope) and _path_selected(node.relative_directory_path, request.path_prefix)),
        key=lambda node: (-node.recursive_known_logical_bytes, node.scope_id, node.relative_directory_path.encode("utf-8", "surrogateescape")),
    )
    kept = list(required)
    for node in optional:
        if len(kept) >= policy.budget.hierarchy_node_total:
            break
        kept.append((node.scope_id, node.relative_directory_path))
    selected_refs = tuple(entry_reference(item.entry) for item in selected)
    result: list[HierarchyItem] = []
    for key in sorted(kept, key=lambda item: (item[0], item[1].encode("utf-8", "surrogateescape"))):
        node = nodes[key]
        anchors = tuple(reference for reference in selected_refs if reference.scope_id == node.scope_id and _path_selected(reference.relative_path, node.relative_directory_path))
        result.append(HierarchyItem(
            ResultLocalReference(namespace, node.path_node_id), node.scope_id, node.relative_directory_path,
            node.observed_directory_entry, _metrics(node, False), _metrics(node, True),
            HierarchyPresentationState.EXPANDED if anchors else HierarchyPresentationState.FOLDED,
            (SelectionReason.STRUCTURE_ANCHOR,), anchors, (),
            ExpansionDescriptor((sources.primary.snapshot_id,), ResultKind.STRUCTURE, namespace, node.scope_id, node.relative_directory_path, request.depth, request.rank, request.min_bytes, local_id=node.path_node_id),
        ))
    return tuple(result)


def _overlay_descriptor(
    snapshot_ids: tuple[str, ...], kind: ResultKind, namespace: ResultNamespace, local_id: str
) -> ExpansionDescriptor:
    return ExpansionDescriptor(snapshot_ids, kind, namespace, local_id=local_id)


def _duplicate_overlays(
    sources: _LoadedSources, universe: tuple[Entry, ...], policy: ProjectionPolicy
) -> tuple[tuple[OverlayItem, ...], tuple[OverlayItem, ...]]:
    if sources.duplicate is None:
        return (), ()
    if not isinstance(policy.budget.duplicate_alias_component_total, int) or not isinstance(policy.budget.members_per_component, int):
        raise ObservationProjectionRequestError("BUDGET_INVALID")
    universe_refs = {entry_reference(entry) for entry in universe}
    namespace = _namespace(ResultKind.DUPLICATE, sources.primary_identity, sources.duplicate.analysis_digest)
    def item(
        kind: OverlayKind,
        local_id: str,
        members: tuple[SnapshotEntryReference, ...],
        limit: int,
    ) -> OverlayItem:
        explicit = tuple(reference for reference in members if reference in universe_refs)[:limit]
        return OverlayItem(kind, ResultLocalReference(namespace, local_id), len(members), explicit, len(members) - len(explicit), None, _overlay_descriptor((sources.primary.snapshot_id,), ResultKind.DUPLICATE, namespace, local_id))
    groups = tuple(item(OverlayKind.DUPLICATE_GROUP, group.payload_group_id, group.member_entries, policy.budget.members_per_component) for group in sources.duplicate.payload_equality_groups[:policy.budget.duplicate_alias_component_total])
    aliases = tuple(item(OverlayKind.HARD_LINK_ALIAS_SET, alias.alias_set_id, alias.member_entries, policy.budget.members_per_component) for alias in sources.duplicate.hard_link_alias_sets[:policy.budget.duplicate_alias_component_total])
    return groups, aliases


def _relation_overlays(
    sources: _LoadedSources, universe: tuple[Entry, ...], policy: ProjectionPolicy
) -> tuple[OverlayItem, ...]:
    if sources.relation is None or sources.relation_pair is None:
        return ()
    if not isinstance(policy.budget.relation_component_total, int) or not isinstance(policy.budget.members_per_component, int):
        raise ObservationProjectionRequestError("BUDGET_INVALID")
    universe_refs = {entry_reference(entry) for entry in universe}
    namespace = _namespace(ResultKind.RELATION, sources.relation_pair, sources.relation.relation_set_digest)
    values: list[OverlayItem] = []
    for relation in sources.relation.relations[:policy.budget.relation_component_total]:
        members = tuple(reference for reference in relation.source_entries + relation.target_entries if reference in universe_refs)
        if not members:
            continue
        values.append(OverlayItem(OverlayKind.RELATION_ITEM, ResultLocalReference(namespace, relation.relation_id), len(relation.source_entries) + len(relation.target_entries), members[:policy.budget.members_per_component], len(members) - len(members[:policy.budget.members_per_component]), relation.certainty, _overlay_descriptor((sources.relation_pair.base.snapshot_id, sources.relation_pair.target.snapshot_id), ResultKind.RELATION, namespace, relation.relation_id)))
    return tuple(values)


def _coverage(entries: tuple[Entry, ...]) -> tuple[CoverageAvailability, CoverageAvailability, CoverageAvailability, int, int, int, int, int, int]:
    regular = tuple(entry for entry in entries if entry.object_type == FilesystemObjectType.REGULAR_FILE and not entry.excluded)
    metadata_failures = sum(entry.observation_status != FilesystemObservationStatus.OBSERVED for entry in entries)
    access_failures = sum(entry.observation_status == FilesystemObservationStatus.PERMISSION_DENIED for entry in entries)
    excluded = sum(entry.excluded for entry in entries)
    unreadable = sum(not entry.readable for entry in entries)
    unknown_size = sum(entry.size_bytes is None for entry in regular)
    non_local = sum(isinstance(entry, FilesystemEntryV2) and entry.payload_observation.status == PayloadObservationStatus.NOT_LOCAL for entry in entries)
    metadata = CoverageAvailability.COMPLETE if metadata_failures == 0 else CoverageAvailability.PARTIAL
    size = CoverageAvailability.COMPLETE if unknown_size == 0 else CoverageAvailability.PARTIAL
    if not entries or not isinstance(entries[0], FilesystemEntryV2):
        payload = CoverageAvailability.UNAVAILABLE
    else:
        hashed = {PayloadObservationStatus.HASHED, PayloadObservationStatus.EMPTY_FILE_HASHED}
        payload = CoverageAvailability.COMPLETE if all(entry.payload_observation.status in hashed for entry in regular if isinstance(entry, FilesystemEntryV2)) else CoverageAvailability.PARTIAL
    return metadata, size, payload, unknown_size, metadata_failures, access_failures, excluded, unreadable, non_local


def _accounting(entries: tuple[Entry, ...], selected: tuple[SelectionCandidate, ...]) -> Accounting:
    explicit = {entry_reference(item.entry) for item in selected}
    source_bytes = sum(entry.size_bytes or 0 for entry in entries if entry.object_type == FilesystemObjectType.REGULAR_FILE and not entry.excluded)
    explicit_bytes = sum(item.entry.size_bytes or 0 for item in selected if item.entry.object_type == FilesystemObjectType.REGULAR_FILE and not item.entry.excluded)
    counts = tuple(ObjectKindCount(kind, sum(entry.object_type == kind for entry in entries)) for kind in sorted(FilesystemObjectType, key=lambda item: item.value) if any(entry.object_type == kind for entry in entries))
    unknown = sum(entry.object_type == FilesystemObjectType.REGULAR_FILE and entry.size_bytes is None and not entry.excluded for entry in entries)
    return Accounting(AccountingDomain.SNAPSHOT_DIAGNOSTIC_ENTRY, len(entries), len(explicit), len(entries) - len(explicit), CoverageAvailability.COMPLETE, source_bytes, explicit_bytes, source_bytes - explicit_bytes, unknown, counts)


def _diagnostic_state(
    sources: _LoadedSources,
    source_identity: ProjectionSourceIdentity,
    plan: tuple[SourcePlanItem, ...],
    entries: tuple[Entry, ...],
) -> DiagnosticState:
    metadata, size, payload, unknown, metadata_failures, access_failures, excluded, unreadable, non_local = _coverage(entries)
    conflicts: list[DiagnosticBoundary] = []
    limitations: list[DiagnosticBoundary] = []
    if sources.structure is not None:
        limitations.extend(DiagnosticBoundary(item.code) for item in sources.structure.limitations)
    if sources.duplicate is not None:
        conflicts.extend(DiagnosticBoundary(item.code) for item in sources.duplicate.integrity_conflicts)
    relation_state = next(item.state for item in plan if item.result_kind == ResultKind.RELATION)
    known = sum(entry.size_bytes or 0 for entry in entries if entry.object_type == FilesystemObjectType.REGULAR_FILE and not entry.excluded)
    return DiagnosticState(ProjectionSourceValidity.VALID, source_identity, metadata, size, payload, tuple(sorted(conflicts, key=lambda item: item.code)), tuple(sorted(limitations, key=lambda item: item.code)), unknown, metadata_failures, access_failures, excluded, unreadable, non_local, relation_state, known, None)


def build_snapshot_diagnostic_projection(
    config: StewardConfig, request: SnapshotDiagnosticRequest, policy: ProjectionPolicy
) -> ObservationProjection:
    """Build one complete immutable Snapshot Diagnostic Projection without writes."""
    normalized = normalize_request(request)
    if not isinstance(normalized, SnapshotDiagnosticRequest):
        raise ObservationProjectionRequestError("MODE_UNSUPPORTED")
    if policy.policy_schema_version != 0:
        raise ObservationProjectionRequestError("POLICY_VERSION_UNSUPPORTED")
    validate_budget(policy.budget)
    plan = plan_snapshot_diagnostic_sources(normalized, policy)
    sources = _load_sources(config, normalized, plan)
    universe = _universe(sources.primary, normalized)
    source_identity, source_plan = _source_facts(sources)
    candidates = _representative_candidates(universe, sources, normalized)
    selected = select_candidates(candidates, policy.budget)
    anchors = tuple(
        extract_entry_anchor(
            item.entry,
            source_side=EntrySourceSide.PRIMARY,
            reasons=item.reasons,
            result_references=item.references,
            include_object_hint=item.include_object_hint,
        )
        for item in selected
    )
    hierarchy = _hierarchy(sources, normalized, selected, policy)
    duplicate_groups, hard_link_aliases = _duplicate_overlays(sources, universe, policy)
    relation_overlays = _relation_overlays(sources, universe, policy)
    descriptors = tuple(sorted(
        tuple(item.expansion_descriptor for item in hierarchy)
        + tuple(item.expansion_descriptor for item in duplicate_groups + hard_link_aliases + relation_overlays)
        + (ExpansionDescriptor((sources.primary.snapshot_id,), ResultKind.SNAPSHOT, _namespace(ResultKind.SNAPSHOT, sources.primary_identity, sources.primary.snapshot_digest), normalized.scope, normalized.path_prefix),),
        key=lambda item: (item.result_kind.value, item.scope or "", item.path_prefix or "", item.local_id or ""),
    ))
    facts = ProjectionPreDigest(
        ProjectionMode.SNAPSHOT_DIAGNOSTIC,
        normalized,
        policy,
        source_identity,
        source_plan,
        _diagnostic_state(sources, source_identity, source_plan, universe),
        (_accounting(universe, selected),),
        descriptors,
        SnapshotDiagnosticBody(hierarchy, anchors, duplicate_groups, hard_link_aliases, relation_overlays),
    )
    return finalize(facts)
