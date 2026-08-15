"""Strict JSON decoding for the public Observation Projection CLI boundary."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import NoReturn

from .errors import ObservationProjectionRequestError
from .models import (
    PairTrackingRequest,
    ProjectionBudget,
    ProjectionMode,
    ProjectionPolicy,
    SnapshotDiagnosticRequest,
    SourcePlanState,
)


JsonObject = dict[str, object]


def _invalid() -> NoReturn:
    raise ObservationProjectionRequestError("PROJECTION_JSON_INVALID")


def _object_pairs(pairs: Iterable[tuple[str, object]]) -> JsonObject:
    value: JsonObject = {}
    for key, item in pairs:
        if key in value:
            _invalid()
        value[key] = item
    return value


def load_json_object(path: Path) -> JsonObject:
    """Read one UTF-8 JSON object without accepting duplicate object keys."""
    try:
        text = path.read_text(encoding="utf-8")
        value: object = json.loads(text, object_pairs_hook=_object_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ObservationProjectionRequestError("PROJECTION_JSON_INVALID") from error
    return _json_object(value)


def _json_object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        _invalid()
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            _invalid()
        result[key] = item
    return result


def _array(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        _invalid()
    return tuple(item for item in value)


def _exact_fields(value: JsonObject, fields: frozenset[str]) -> None:
    if set(value) != fields:
        _invalid()


def _string(value: object) -> str:
    if not isinstance(value, str):
        _invalid()
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid()
    return value


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    return _integer(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        _invalid()
    return value


def _state(value: object) -> SourcePlanState:
    try:
        return SourcePlanState(_string(value))
    except ValueError as error:
        raise ObservationProjectionRequestError("PROJECTION_JSON_INVALID") from error


def _pair(value: object) -> tuple[str, str] | None:
    if value is None:
        return None
    values = _array(value)
    if len(values) != 2:
        _invalid()
    return (_string(values[0]), _string(values[1]))


def decode_snapshot_diagnostic_request(value: JsonObject) -> SnapshotDiagnosticRequest:
    """Decode the complete tagged Snapshot Diagnostic request object."""
    _exact_fields(
        value,
        frozenset(
            {
                "mode",
                "primary_snapshot_id",
                "scope",
                "path_prefix",
                "hierarchy_requested",
                "depth",
                "rank",
                "min_bytes",
                "duplicate_overlay",
                "relation_context_pair",
            }
        ),
    )
    if _string(value["mode"]) != ProjectionMode.SNAPSHOT_DIAGNOSTIC.value:
        raise ObservationProjectionRequestError("MODE_UNSUPPORTED")
    return SnapshotDiagnosticRequest(
        _string(value["primary_snapshot_id"]),
        _optional_string(value["scope"]),
        _optional_string(value["path_prefix"]),
        _boolean(value["hierarchy_requested"]),
        _optional_integer(value["depth"]),
        _optional_string(value["rank"]),
        _optional_integer(value["min_bytes"]),
        _state(value["duplicate_overlay"]),
        _pair(value["relation_context_pair"]),
    )


def decode_pair_tracking_request(value: JsonObject) -> PairTrackingRequest:
    """Decode the complete tagged Pair Tracking request object."""
    _exact_fields(
        value,
        frozenset(
            {
                "mode",
                "base_snapshot_id",
                "target_snapshot_id",
                "scope",
                "path_prefix",
                "growth",
                "diff",
                "relation",
            }
        ),
    )
    if _string(value["mode"]) != ProjectionMode.PAIR_TRACKING.value:
        raise ObservationProjectionRequestError("MODE_UNSUPPORTED")
    return PairTrackingRequest(
        _string(value["base_snapshot_id"]),
        _string(value["target_snapshot_id"]),
        _optional_string(value["scope"]),
        _optional_string(value["path_prefix"]),
        _state(value["growth"]),
        _state(value["diff"]),
        _state(value["relation"]),
    )


def _budget(value: object) -> ProjectionBudget:
    value = _json_object(value)
    _exact_fields(
        value,
        frozenset(
            {
                "explicit_entry_total",
                "hierarchy_node_total",
                "tracking_item_total",
                "relation_component_total",
                "duplicate_alias_component_total",
                "members_per_component",
                "scope_minimum_guarantee",
                "priority_quotas",
                "serialized_bytes_soft",
            }
        ),
    )
    raw_quotas = value["priority_quotas"]
    quotas: list[tuple[str, int]] = []
    for item in _array(raw_quotas):
        values = _array(item)
        if len(values) != 2:
            _invalid()
        quotas.append((_string(values[0]), _integer(values[1])))
    if len({name for name, _ in quotas}) != len(quotas):
        _invalid()
    return ProjectionBudget(
        _integer(value["explicit_entry_total"]),
        _integer(value["hierarchy_node_total"]),
        _integer(value["tracking_item_total"]),
        _integer(value["relation_component_total"]),
        _integer(value["duplicate_alias_component_total"]),
        _integer(value["members_per_component"]),
        _integer(value["scope_minimum_guarantee"]),
        tuple(quotas),
        _integer(value["serialized_bytes_soft"]),
    )


def decode_projection_policy(value: JsonObject) -> ProjectionPolicy:
    """Decode one fully resolved policy; calibration values are not accepted."""
    _exact_fields(
        value,
        frozenset(
            {
                "policy_schema_version",
                "ordering_reference",
                "budget",
                "duplicate_overlay",
                "relation_overlay",
            }
        ),
    )
    return ProjectionPolicy(
        _integer(value["policy_schema_version"]),
        _string(value["ordering_reference"]),
        _budget(value["budget"]),
        _boolean(value["duplicate_overlay"]),
        _boolean(value["relation_overlay"]),
    )
