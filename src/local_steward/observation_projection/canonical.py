"""Pure canonical machine-object conversion and digesting."""

from dataclasses import fields, is_dataclass
from enum import Enum
from hashlib import sha256
from typing import TypeAlias

from ..evidence import canonical_json
from .models import (
    ALGORITHM,
    ALGORITHM_VERSION,
    DIGEST_DOMAIN,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    ObservationProjection,
    PairTrackingGrowthHierarchyItem,
    ProjectionPreDigest,
)
from .validation import validate_predigest

JsonValue: TypeAlias = str | int | bool | None | list["JsonValue"] | dict[str, "JsonValue"]

_ORDERED_FIELDS = frozenset(
    {
        "accounting", "anchor_references", "boundary_references", "change_kind_counts",
        "conflict_counts", "conflicts", "duplicate_overlays", "expansion_descriptors",
        "explicit_entry_anchors", "explicit_member_references", "hard_link_alias_overlays",
        "hierarchy_items", "limitations", "object_kind_counts", "priority_quotas",
        "relation_overlays", "relation_references", "result_identities", "result_references",
        "selection_reasons", "source_plan", "tracking_items",
    }
)


def _ordered(field_name, values):  # type: ignore[no-untyped-def]
    if field_name == "hierarchy_items" and all(
        isinstance(item, PairTrackingGrowthHierarchyItem) for item in values
    ):
        return tuple(sorted(
            values,
            key=lambda item: (
                item.scope_id,
                item.relative_directory_path.encode("utf-8", "surrogateescape"),
                item.node_reference.result_local_id.encode("utf-8", "surrogateescape"),
            ),
        ))
    if field_name not in _ORDERED_FIELDS:
        return values
    return tuple(sorted(values, key=lambda item: canonical_json(_wire(item))))  # type: ignore[no-untyped-call]


def _wire(value):  # type: ignore[no-untyped-def]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_wire(item) for item in value]  # type: ignore[no-untyped-call]
    if is_dataclass(value):
        wire: dict[str, JsonValue] = {}
        for field in fields(value):
            item = getattr(value, field.name)
            if item is not None:
                wire[field.name] = _wire(_ordered(field.name, item))  # type: ignore[no-untyped-call]
        return wire
    raise TypeError(f"unsupported Projection canonical value: {type(value)!r}")


def machine_object(facts: ProjectionPreDigest) -> dict[str, JsonValue]:
    validate_predigest(facts)
    wire = _wire(facts)  # type: ignore[no-untyped-call]
    if not isinstance(wire, dict):
        raise TypeError("Projection facts must serialize to an object")
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "algorithm": ALGORITHM,
        "algorithm_version": ALGORITHM_VERSION,
        "domain": DIGEST_DOMAIN,
        **wire,
    }


def canonical_projection(facts: ProjectionPreDigest) -> bytes:
    return canonical_json(machine_object(facts))


def projection_digest(facts: ProjectionPreDigest) -> str:
    return sha256(canonical_projection(facts)).hexdigest()


def finalize(facts: ProjectionPreDigest) -> ObservationProjection:
    return ObservationProjection(facts, projection_digest(facts))
