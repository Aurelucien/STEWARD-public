"""Deterministic bounded selection and scope fairness for Snapshot Diagnostic."""

from dataclasses import dataclass

from .entry_facts import Entry, entry_reference
from .models import ProjectionBudget, ResultLocalReference, SelectionReason


_CHANNEL_BY_REASON = {
    SelectionReason.METADATA_FAILURE: 0,
    SelectionReason.OBSERVATION_FAILURE: 0,
    SelectionReason.ACCESS_FAILURE: 0,
    SelectionReason.UNKNOWN_SIZE: 0,
    SelectionReason.PAYLOAD_UNKNOWN: 0,
    SelectionReason.EXCLUDED: 0,
    SelectionReason.UNREADABLE: 0,
    SelectionReason.NON_LOCAL: 0,
    SelectionReason.INTEGRITY_CONFLICT: 0,
    SelectionReason.REUSE_PROVENANCE: 0,
    SelectionReason.AMBIGUOUS_RELATION: 0,
    SelectionReason.USER_REQUESTED_LOCATION: 0,
    SelectionReason.HARD_LINK_REPRESENTATIVE: 2,
    SelectionReason.DUPLICATE_REPRESENTATIVE: 2,
    SelectionReason.RELATION_COMPONENT_REPRESENTATIVE: 2,
    SelectionReason.SCOPE_BOUNDARY_REPRESENTATIVE: 2,
    SelectionReason.OBJECT_HINT_BOUNDARY_REPRESENTATIVE: 2,
    SelectionReason.LOGICAL_BYTE_CONTRIBUTOR: 3,
    SelectionReason.STRUCTURE_ANCHOR: 3,
    SelectionReason.DIAGNOSTIC_NEIGHBOR: 3,
}


@dataclass(frozen=True, slots=True)
class SelectionCandidate:
    entry: Entry
    reasons: tuple[SelectionReason, ...]
    references: tuple[ResultLocalReference, ...] = ()
    include_object_hint: bool = False

    @property
    def key(self) -> tuple[str, bytes]:
        reference = entry_reference(self.entry)
        return (reference.scope_id, reference.relative_path.encode("utf-8", "surrogateescape"))

    @property
    def channel(self) -> int:
        return min((_CHANNEL_BY_REASON.get(reason, 1) for reason in self.reasons), default=1)


def coalesce_candidates(values: tuple[SelectionCandidate, ...]) -> tuple[SelectionCandidate, ...]:
    merged: dict[tuple[str, bytes], SelectionCandidate] = {}
    for value in values:
        current = merged.get(value.key)
        if current is None:
            merged[value.key] = value
            continue
        merged[value.key] = SelectionCandidate(
            current.entry,
            tuple(sorted(set(current.reasons + value.reasons), key=lambda item: item.value)),
            tuple(sorted(set(current.references + value.references), key=lambda item: (item.namespace.result_kind.value, item.result_local_id))),
            current.include_object_hint or value.include_object_hint,
        )
    return tuple(merged[key] for key in sorted(merged))


def select_candidates(
    values: tuple[SelectionCandidate, ...], budget: ProjectionBudget
) -> tuple[SelectionCandidate, ...]:
    """Select bounded facts: one deterministic fair pass, then channel order."""
    if not isinstance(budget.explicit_entry_total, int):
        return ()
    candidates = coalesce_candidates(values)
    total = budget.explicit_entry_total
    if total == 0:
        return ()
    ordered = tuple(sorted(candidates, key=lambda item: (item.channel, *item.key)))
    selected: list[SelectionCandidate] = []
    per_scope = budget.scope_minimum_guarantee if isinstance(budget.scope_minimum_guarantee, int) else 0
    if per_scope > 0:
        scopes = sorted({item.key[0] for item in ordered})
        for scope_id in scopes:
            for item in ordered:
                if item.key[0] == scope_id and item not in selected:
                    selected.append(item)
                    break
            if len(selected) >= total:
                return tuple(selected)
    for item in ordered:
        if item not in selected:
            selected.append(item)
        if len(selected) >= total:
            break
    return tuple(selected)
