"""Explicit runtime registrations over only the public Steward facade."""

from dataclasses import dataclass
import json
from typing import Any, Callable

from ...models import (
    FilesystemObjectType,
    FilesystemObservationStatus,
    GrowthRank,
    RelationKind,
    StructureRank,
)
from ...observation_projection import (
    PairTrackingRequest,
    ProjectionPolicy,
    SnapshotDiagnosticRequest,
)
from .. import (
    AgentToolError,
    AgentToolEnvelope,
    ToolExecutionContext,
    serialize_envelope,
    steward_compare_snapshots,
    steward_inspect_duplicates,
    steward_inspect_growth,
    steward_inspect_relations,
    steward_inspect_snapshot,
    steward_inspect_structure,
    steward_list_snapshots,
    steward_project_snapshot,
    steward_resolve_entry_reference,
)
from .failures import RuntimeFailure
from .runtime import RuntimeTool, RuntimeToolResult, SourceFamily, ToolRegistry


@dataclass(frozen=True, slots=True)
class StewardRuntimeDependencies:
    context: ToolExecutionContext
    projection_policy: ProjectionPolicy


def _object_schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        value["required"] = list(required)
    return value


_STRING = {"type": "string", "minLength": 1}
_HISTORICAL_ENTRY_RELATIVE_PATH = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Complete Snapshot-scoped relative path for one historical Entry. It must exactly match an Entry in "
        "snapshot_id and scope_id. A basename is not an Entry reference unless it is the complete relative path. "
        "Do not pass a current filesystem path or promote one to a historical reference; first inspect or search "
        "the historical Snapshot when the complete path is unknown. No basename, fuzzy, or scope-changing fallback "
        "is performed. This historical reference is not a current filesystem handle."
    ),
}
_POSITIVE = {"type": "integer", "minimum": 1, "maximum": 1000}
_OFFSET = {"type": "integer", "minimum": 0}
_OPTIONAL_STRING = {"type": ["string", "null"]}
_OPTIONAL_INT = {"type": ["integer", "null"], "minimum": 0}


def _enum_or_none(enum_type: type[Any], value: object) -> Any | None:
    return None if value is None else enum_type(value)


def _envelope_result(envelope: AgentToolEnvelope) -> RuntimeToolResult:
    payload = json.loads(serialize_envelope(envelope))
    return RuntimeToolResult(
        SourceFamily.STEWARD_HISTORICAL,
        {"envelope": payload},
        envelope.result_digest,
        envelope.entries_returned or 0,
        envelope.serialized_bytes,
        envelope.elapsed_ms,
        envelope.status.value,
    )


def _invoke(action: Callable[[], AgentToolEnvelope]) -> RuntimeToolResult:
    try:
        return _envelope_result(action())
    except AgentToolError as error:
        if error.code in {"INVALID_ARGUMENT", "QUERY_TOO_BROAD"}:
            raise RuntimeFailure("TOOL_ARGUMENT_INVALID", "Steward tool arguments are invalid") from error
        if error.code == "BUDGET_EXHAUSTED":
            raise RuntimeFailure("BUDGET_EXHAUSTED", "Steward shared budget is exhausted") from error
        raise RuntimeFailure("STEWARD_TOOL_FAILED", "Steward historical query failed") from error


def register_steward_tools(registry: ToolRegistry, dependencies: StewardRuntimeDependencies) -> None:
    """Register exactly the nine stable facade callables with explicit schemas."""

    context = dependencies.context
    policy = dependencies.projection_policy

    registry.register(
        RuntimeTool(
            "steward_list_snapshots",
            "List historical Snapshot inventory; results are not current filesystem state.",
            _object_schema({"limit": _POSITIVE, "offset": _OFFSET}),
            SourceFamily.STEWARD_HISTORICAL,
            lambda value: _invoke(lambda: steward_list_snapshots(context, **value)),
        )
    )
    registry.register(
        RuntimeTool(
            "steward_inspect_snapshot",
            "Inspect a flat historical Snapshot Entry page. It has no depth or hierarchy semantics.",
            _object_schema(
                {
                    "snapshot_id": _STRING,
                    "scope_id": _OPTIONAL_STRING,
                    "object_type": _OPTIONAL_STRING,
                    "observation_status": _OPTIONAL_STRING,
                    "path_prefix": _OPTIONAL_STRING,
                    "limit": _POSITIVE,
                    "offset": _OFFSET,
                },
                ("snapshot_id",),
            ),
            SourceFamily.STEWARD_HISTORICAL,
            lambda value: _invoke(
                lambda: steward_inspect_snapshot(
                    context,
                    value["snapshot_id"],
                    scope_id=value.get("scope_id"),
                    object_type=_enum_or_none(FilesystemObjectType, value.get("object_type")),
                    observation_status=_enum_or_none(
                        FilesystemObservationStatus, value.get("observation_status")
                    ),
                    path_prefix=value.get("path_prefix"),
                    limit=value.get("limit", 100),
                    offset=value.get("offset", 0),
                )
            ),
        )
    )
    registry.register(
        RuntimeTool(
            "steward_inspect_structure",
            "Inspect historical derived directory hierarchy and logical bytes; depth is effective-root-relative.",
            _object_schema(
                {
                    "snapshot_id": _STRING,
                    "scope_id": _OPTIONAL_STRING,
                    "path_prefix": _OPTIONAL_STRING,
                    "depth": _OPTIONAL_INT,
                    "rank": _OPTIONAL_STRING,
                    "min_bytes": _OPTIONAL_INT,
                    "limit": _POSITIVE,
                    "offset": _OFFSET,
                },
                ("snapshot_id",),
            ),
            SourceFamily.STEWARD_HISTORICAL,
            lambda value: _invoke(
                lambda: steward_inspect_structure(
                    context,
                    value["snapshot_id"],
                    scope_id=value.get("scope_id"),
                    path_prefix=value.get("path_prefix"),
                    depth=value.get("depth"),
                    rank=_enum_or_none(StructureRank, value.get("rank")),
                    min_bytes=value.get("min_bytes"),
                    limit=value.get("limit", 100),
                    offset=value.get("offset", 0),
                )
            ),
        )
    )
    registry.register(
        RuntimeTool(
            "steward_compare_snapshots",
            "Compare two historical Snapshots; it does not establish a long-term trend.",
            _object_schema({"left_snapshot_id": _STRING, "right_snapshot_id": _STRING}, ("left_snapshot_id", "right_snapshot_id")),
            SourceFamily.STEWARD_HISTORICAL,
            lambda value: _invoke(
                lambda: steward_compare_snapshots(context, value["left_snapshot_id"], value["right_snapshot_id"])
            ),
        )
    )
    registry.register(
        RuntimeTool(
            "steward_inspect_growth",
            "Inspect directional historical logical growth, not physical allocation or long-term trend.",
            _object_schema(
                {
                    "base_snapshot_id": _STRING,
                    "target_snapshot_id": _STRING,
                    "scope_id": _OPTIONAL_STRING,
                    "path_prefix": _OPTIONAL_STRING,
                    "depth": _OPTIONAL_INT,
                    "rank": _OPTIONAL_STRING,
                    "min_bytes": _OPTIONAL_INT,
                    "limit": _POSITIVE,
                    "offset": _OFFSET,
                },
                ("base_snapshot_id", "target_snapshot_id"),
            ),
            SourceFamily.STEWARD_HISTORICAL,
            lambda value: _invoke(
                lambda: steward_inspect_growth(
                    context,
                    value["base_snapshot_id"],
                    value["target_snapshot_id"],
                    scope_id=value.get("scope_id"),
                    path_prefix=value.get("path_prefix"),
                    depth=value.get("depth"),
                    rank=_enum_or_none(GrowthRank, value.get("rank")),
                    min_bytes=value.get("min_bytes"),
                    limit=value.get("limit", 100),
                    offset=value.get("offset", 0),
                )
            ),
        )
    )
    registry.register(
        RuntimeTool(
            "steward_inspect_duplicates",
            "Inspect historical equal-payload groups; duplicate equality does not prove deletion or reclaimable space.",
            _object_schema({"snapshot_id": _STRING, "only_exact": {"type": "boolean"}, "limit": _POSITIVE, "offset": _OFFSET}, ("snapshot_id",)),
            SourceFamily.STEWARD_HISTORICAL,
            lambda value: _invoke(
                lambda: steward_inspect_duplicates(
                    context,
                    value["snapshot_id"],
                    only_exact=value.get("only_exact", False),
                    limit=value.get("limit", 100),
                    offset=value.get("offset", 0),
                )
            ),
        )
    )
    registry.register(
        RuntimeTool(
            "steward_inspect_relations",
            "Inspect historical relations; relation candidates do not confirm rename or move.",
            _object_schema(
                {
                    "base_snapshot_id": _STRING,
                    "target_snapshot_id": _STRING,
                    "kind": _OPTIONAL_STRING,
                    "limit": _POSITIVE,
                    "offset": _OFFSET,
                },
                ("base_snapshot_id", "target_snapshot_id"),
            ),
            SourceFamily.STEWARD_HISTORICAL,
            lambda value: _invoke(
                lambda: steward_inspect_relations(
                    context,
                    value["base_snapshot_id"],
                    value["target_snapshot_id"],
                    kind=_enum_or_none(RelationKind, value.get("kind")),
                    limit=value.get("limit", 100),
                    offset=value.get("offset", 0),
                )
            ),
        )
    )
    registry.register(
        RuntimeTool(
            "steward_project_snapshot",
            "Build a composite historical Projection; it is a read-only view, not Agent planning.",
            _object_schema(
                {
                    "mode": {"type": "string", "enum": ["SNAPSHOT_DIAGNOSTIC", "PAIR_TRACKING"]},
                    "snapshot_id": _OPTIONAL_STRING,
                    "base_snapshot_id": _OPTIONAL_STRING,
                    "target_snapshot_id": _OPTIONAL_STRING,
                    "scope": _OPTIONAL_STRING,
                    "path_prefix": _OPTIONAL_STRING,
                    "depth": _OPTIONAL_INT,
                },
                ("mode",),
            ),
            SourceFamily.STEWARD_HISTORICAL,
            lambda value: _invoke(lambda: _project(context, policy, value)),
        )
    )
    registry.register(
        RuntimeTool(
            "steward_resolve_entry_reference",
            "Resolve one exact historical Entry reference. relative_path must be the complete Snapshot-scoped "
            "relative path returned by historical inspection and exactly match an Entry in snapshot_id and scope_id. "
            "A basename is not resolved unless it is already the complete path; current filesystem paths are never "
            "converted or promoted. Inspect or search the historical Snapshot first if the complete path is unknown. "
            "No basename, fuzzy, or scope-changing fallback occurs. The result is historical only, not a current "
            "filesystem handle.",
            _object_schema(
                {"snapshot_id": _STRING, "scope_id": _STRING, "relative_path": _HISTORICAL_ENTRY_RELATIVE_PATH},
                ("snapshot_id", "scope_id", "relative_path"),
            ),
            SourceFamily.STEWARD_HISTORICAL,
            lambda value: _invoke(
                lambda: steward_resolve_entry_reference(
                    context, value["snapshot_id"], value["scope_id"], value["relative_path"]
                )
            ),
        )
    )


def _project(
    context: ToolExecutionContext, policy: ProjectionPolicy, value: dict[str, Any]
) -> AgentToolEnvelope:
    request: SnapshotDiagnosticRequest | PairTrackingRequest
    if value["mode"] == "SNAPSHOT_DIAGNOSTIC":
        snapshot_id = value.get("snapshot_id")
        if not isinstance(snapshot_id, str):
            raise RuntimeFailure("TOOL_ARGUMENT_INVALID", "snapshot diagnostic requires snapshot_id")
        request = SnapshotDiagnosticRequest(
            snapshot_id,
            scope=value.get("scope"),
            path_prefix=value.get("path_prefix"),
            depth=value.get("depth"),
        )
    else:
        base = value.get("base_snapshot_id")
        target = value.get("target_snapshot_id")
        if not isinstance(base, str) or not isinstance(target, str):
            raise RuntimeFailure("TOOL_ARGUMENT_INVALID", "pair tracking requires both Snapshot IDs")
        request = PairTrackingRequest(
            base,
            target,
            scope=value.get("scope"),
            path_prefix=value.get("path_prefix"),
        )
    return steward_project_snapshot(context, request, policy)
