"""Deterministic, repository-free conversion from Projection to Context Packet."""

from dataclasses import replace
from hashlib import sha256
from typing import TypeVar

from ..evidence import canonical_json
from ..observation_projection.models import ObservationProjection, OverlayItem, ProjectionMode
from .canonical import canonical_packet, finalize_packet, machine_value, packet_machine_object
from .errors import LLMContextRequestError, LLMUnsupportedTaskDomainError
from .models import (
    CONTEXT_PROTOCOL_VERSION,
    ContextBudget,
    ContextGrowthHierarchy,
    ContextOmission,
    ContextOmissionCategory,
    ContextRequestScope,
    ContextSourceRepresentation,
    EvidenceReference,
    EvidenceReferenceKind,
    LLMContextPacket,
    LLMTaskDomain,
    UserIntentContext,
)


def _canonical_key(value: object) -> bytes:
    return canonical_json(machine_value(value))


T = TypeVar("T")


def _sorted(values: tuple[T, ...]) -> tuple[T, ...]:
    return tuple(sorted(values, key=_canonical_key))


def _validate_budget(value: ContextBudget) -> None:
    for item in (
        value.max_explicit_facts,
        value.max_hierarchy_items,
        value.max_overlays,
        value.max_expansion_descriptors,
    ):
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise LLMContextRequestError("LLM_CONTEXT_BUDGET_INVALID")


def _validate_intent(value: UserIntentContext) -> None:
    if not isinstance(value.question, str) or not value.question.strip():
        raise LLMContextRequestError("LLM_USER_INTENT_INVALID")
    if value.scope_emphasis is not None and not isinstance(value.scope_emphasis, str):
        raise LLMContextRequestError("LLM_USER_INTENT_INVALID")
    if value.user_provided_context is not None and not isinstance(value.user_provided_context, str):
        raise LLMContextRequestError("LLM_USER_INTENT_INVALID")


def task_domain_for_projection(projection: ObservationProjection) -> LLMTaskDomain:
    if projection.facts.mode == ProjectionMode.SNAPSHOT_DIAGNOSTIC:
        return LLMTaskDomain.STATIC_SNAPSHOT
    if projection.facts.mode == ProjectionMode.PAIR_TRACKING:
        return LLMTaskDomain.STATIC_PAIR_COMPARISON
    raise LLMUnsupportedTaskDomainError("LLM_TASK_DOMAIN_UNSUPPORTED")


def _token(kind: EvidenceReferenceKind, payload: object, packet_scope: bytes) -> str:
    raw = canonical_json({"kind": kind.value, "payload": machine_value(payload)})
    return sha256(b"local_steward.llm_context_evidence_reference.v0\0" + packet_scope + raw).hexdigest()


def _entry_reference(value, packet_scope: bytes) -> EvidenceReference:  # type: ignore[no-untyped-def]
    return EvidenceReference(EvidenceReferenceKind.ENTRY, _token(EvidenceReferenceKind.ENTRY, value, packet_scope), entry_reference=value)


def _result_reference(value, packet_scope: bytes) -> EvidenceReference:  # type: ignore[no-untyped-def]
    return EvidenceReference(EvidenceReferenceKind.RESULT_LOCAL, _token(EvidenceReferenceKind.RESULT_LOCAL, value, packet_scope), result_reference=value)


def _accounting_reference(value, packet_scope: bytes) -> EvidenceReference:  # type: ignore[no-untyped-def]
    return EvidenceReference(EvidenceReferenceKind.ACCOUNTING, _token(EvidenceReferenceKind.ACCOUNTING, value, packet_scope), accounting_domain=value)


def _descriptor_reference(value, packet_scope: bytes) -> EvidenceReference:  # type: ignore[no-untyped-def]
    return EvidenceReference(EvidenceReferenceKind.EXPANSION, _token(EvidenceReferenceKind.EXPANSION, value, packet_scope), expansion_descriptor=value)


def _source_reference(value, packet_scope: bytes) -> EvidenceReference:  # type: ignore[no-untyped-def]
    return EvidenceReference(EvidenceReferenceKind.PROJECTION_SOURCE, _token(EvidenceReferenceKind.PROJECTION_SOURCE, value, packet_scope), source_identity=value)


def _unique_references(values: tuple[EvidenceReference, ...]) -> tuple[EvidenceReference, ...]:
    by_token = {value.token: value for value in values}
    return tuple(by_token[key] for key in sorted(by_token))


def _take(values: tuple[T, ...], limit: int) -> tuple[tuple[T, ...], tuple[T, ...]]:
    ordered = _sorted(values)
    return ordered[:limit], ordered[limit:]


def _omission(category: ContextOmissionCategory, values: tuple[object, ...]) -> ContextOmission | None:
    if not values:
        return None
    return ContextOmission(category, len(values), ContextSourceRepresentation.EXPLICIT)


def _packet_references(packet: LLMContextPacket) -> tuple[EvidenceReference, ...]:
    packet_scope = sha256(canonical_packet(packet)).digest()
    values: list[EvidenceReference] = [_source_reference(packet.source_identity, packet_scope)]
    for anchor in packet.explicit_entry_anchors:
        values.append(_entry_reference(anchor.entry_reference, packet_scope))
        values.extend(_result_reference(result_reference, packet_scope) for result_reference in anchor.result_references)
    for tracking_item in packet.tracking_items:
        if tracking_item.base_entry_reference is not None:
            values.append(_entry_reference(tracking_item.base_entry_reference, packet_scope))
        if tracking_item.target_entry_reference is not None:
            values.append(_entry_reference(tracking_item.target_entry_reference, packet_scope))
        values.append(_result_reference(tracking_item.diff_reference, packet_scope))
        if tracking_item.growth_contribution_reference is not None:
            values.append(_result_reference(tracking_item.growth_contribution_reference, packet_scope))
        values.extend(_result_reference(reference, packet_scope) for reference in tracking_item.relation_references)
    for hierarchy_item in packet.hierarchy_items:
        values.append(_result_reference(hierarchy_item.node_reference, packet_scope))
        values.extend(_result_reference(reference, packet_scope) for reference in hierarchy_item.boundary_references)
    if packet.growth_hierarchy is not None:
        values.extend(_result_reference(growth_item.node_reference, packet_scope) for growth_item in packet.growth_hierarchy.hierarchy_items)
    values.extend(_result_reference(overlay.component_reference, packet_scope) for overlay in packet.overlays)
    values.extend(_accounting_reference(accounting.domain, packet_scope) for accounting in packet.independent_accounting)
    values.extend(_descriptor_reference(descriptor, packet_scope) for descriptor in packet.expansion_descriptors)
    return _unique_references(tuple(values))


def build_context_packet(
    projection: ObservationProjection,
    user_intent: UserIntentContext,
    budget: ContextBudget,
) -> LLMContextPacket:
    """Build one bounded packet without repository, V2A, or Projection-service access."""
    _validate_budget(budget)
    _validate_intent(user_intent)
    facts = projection.facts
    task_domain = task_domain_for_projection(projection)
    anchors = facts.snapshot_diagnostic.explicit_entry_anchors if facts.snapshot_diagnostic else ()
    tracking_items = facts.pair_tracking.tracking_items if facts.pair_tracking else ()
    selected_anchor_values, omitted_anchor_values = _take(tuple(anchors), budget.max_explicit_facts)
    remaining = max(0, budget.max_explicit_facts - len(selected_anchor_values))
    selected_tracking_values, omitted_tracking_values = _take(tuple(tracking_items), remaining)
    hierarchy = facts.snapshot_diagnostic.hierarchy_items if facts.snapshot_diagnostic else ()
    selected_hierarchy_values, omitted_hierarchy_values = _take(tuple(hierarchy), budget.max_hierarchy_items)
    growth_hierarchy = None
    omitted_growth_values: tuple[object, ...] = ()
    if facts.pair_tracking is not None:
        source = facts.pair_tracking.growth_hierarchy
        selected_growth_values, omitted_growth_values = _take(
            tuple(source.hierarchy_items), budget.max_hierarchy_items
        )
        growth_hierarchy = ContextGrowthHierarchy(source.source_result_identity, source.state, selected_growth_values)
    overlays: tuple[OverlayItem, ...] = ()
    if facts.snapshot_diagnostic is not None:
        body = facts.snapshot_diagnostic
        overlays = body.duplicate_overlays + body.hard_link_alias_overlays + body.relation_overlays
    elif facts.pair_tracking is not None:
        overlays = facts.pair_tracking.relation_overlays
    selected_overlay_values, omitted_overlay_values = _take(tuple(overlays), budget.max_overlays)
    selected_descriptor_values, omitted_descriptor_values = _take(
        tuple(facts.expansion_descriptors), budget.max_expansion_descriptors
    )
    omissions = tuple(
        item
        for item in (
            _omission(ContextOmissionCategory.EXPLICIT_ENTRY_ANCHORS, omitted_anchor_values),
            _omission(ContextOmissionCategory.TRACKING_ITEMS, omitted_tracking_values),
            _omission(ContextOmissionCategory.HIERARCHY_ITEMS, omitted_hierarchy_values + omitted_growth_values),
            _omission(ContextOmissionCategory.OVERLAYS, omitted_overlay_values),
            _omission(ContextOmissionCategory.EXPANSION_DESCRIPTORS, omitted_descriptor_values),
        )
        if item is not None
    )
    request = facts.normalized_request
    packet = LLMContextPacket(
        CONTEXT_PROTOCOL_VERSION,
        task_domain,
        user_intent,
        budget,
        facts.mode,
        projection.projection_digest,
        facts.source_identity,
        ContextRequestScope(request.scope, request.path_prefix),
        facts.resolved_policy,
        tuple(_sorted(facts.source_plan)),
        facts.diagnostic_state,
        tuple(selected_anchor_values),
        tuple(selected_tracking_values),
        tuple(selected_hierarchy_values),
        growth_hierarchy,
        tuple(selected_overlay_values),
        tuple(_sorted(facts.accounting)),
        tuple(selected_descriptor_values),
        tuple(_sorted(omissions)),
        (),
        "",
    )
    packet = replace(packet, evidence_references=_packet_references(packet))
    return finalize_packet(packet)


def render_context_packet(packet: LLMContextPacket) -> str:
    """Render packet facts as one ordinary JSON object; untrusted data stays data."""
    return canonical_json(packet_machine_object(packet, include_digest=True)).decode("utf-8")
