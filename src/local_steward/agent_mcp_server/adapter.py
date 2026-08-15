"""Grant-gated single-call bridge from typed routing to historical Context."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import anyio
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp.types import CallToolResult, TextContent

from local_steward.agent_context import (
    AgentContextError,
    AgentContextPack,
    AgentContextPackRequest,
    agent_context_pack_machine_object,
    prepare_agent_context,
)
from local_steward.agent_routing import (
    AgentRouteGrantError,
    AuthorityBoundary,
    OperationKind,
    PublicationAccounting,
    PublicationEnvelope,
    PublicationExactInteger,
    PublicationFact,
    PublicationSourceProvenance,
    PublicationStatus,
    PublicationTypedError,
    RouteBounds,
    RouteDecision,
    RouteGrantGuard,
    StewardRouteOutcome,
    StewardRouteRequest,
    build_publication_envelope,
    publication_envelope_machine_object,
    route_outcome_machine_object,
    route_steward_operation,
)
from local_steward.config import load_config
from local_steward.errors import ConfigurationError
from local_steward.mcp_server.adapter import canonical_json, model_safe_json
from local_steward.mcp_server.profile import build_pack_request

from .protocol import (
    ADAPTER_SCHEMA_NAME,
    ADAPTER_SCHEMA_VERSION,
    EXACT_INTEGER_ENCODING_SCHEME,
    INPUT_SCHEMA,
    MAX_STRUCTURED_RESULT_BYTES,
    MAX_TEXT_BYTES,
    OPERATION_TIMEOUT_SECONDS,
    OUTPUT_SCHEMA,
    TOOL_NAME,
)


JsonObject = dict[str, Any]


def _safe_message(code: str) -> str:
    return {
        "STEWARD_AGENT_MCP_ARGUMENT_INVALID": "The STEWARD route arguments are invalid.",
        "STEWARD_ROUTE_CORE_REQUIRED": "This operation must use the supported STEWARD Core path.",
        "STEWARD_ROUTE_CLARIFICATION_REQUIRED": "Exact route identities or bounds are missing.",
        "STEWARD_ROUTE_UNSUPPORTED": "The requested operation is outside the closed STEWARD surface.",
        "STEWARD_ROUTE_GRANT_INVALID": "The Context route grant was rejected.",
        "STEWARD_AGENT_MCP_RESOURCE_LIMIT": "The STEWARD route result exceeds the governed limit.",
        "STEWARD_AGENT_MCP_TIMEOUT": "The STEWARD Context operation exceeded its time limit.",
        "STEWARD_AGENT_MCP_SOURCE_UNAVAILABLE": "The configured STEWARD source is unavailable.",
        "STEWARD_AGENT_MCP_UNAVAILABLE": "The STEWARD Context operation is unavailable.",
    }.get(code, "The STEWARD operation failed safely.")


def _route_request(arguments: JsonObject) -> StewardRouteRequest:
    snapshot_ids = arguments.get("ordered_snapshot_ids", [])
    bounds_value = arguments.get("bounds")
    bounds = None
    if isinstance(bounds_value, dict):
        bounds = RouteBounds(bounds_value["limit"], bounds_value.get("offset", 0))
    return StewardRouteRequest(
        operation_kind=arguments["operation_kind"],
        ordered_snapshot_ids=tuple(snapshot_ids),
        scope_id=arguments.get("scope_id"),
        path_or_prefix=arguments.get("path_or_prefix"),
        bounds=bounds,
    )


def _pack_request(request: StewardRouteRequest, question: str) -> AgentContextPackRequest:
    if request.bounds is None or request.scope_id is None:
        raise AgentRouteGrantError("Context route request is incomplete")
    if request.operation_kind == OperationKind.BOUNDED_STRUCTURAL_DIAGNOSTIC.value:
        source: JsonObject = {
            "kind": "SNAPSHOT_DIAGNOSTIC",
            "snapshot_id": request.ordered_snapshot_ids[0],
            "scope_id": request.scope_id,
            "path_prefix": request.path_or_prefix,
        }
    elif request.operation_kind == OperationKind.ORDERED_HISTORICAL_CHANGE_EXPLANATION.value:
        source = {
            "kind": "PAIR_TRACKING",
            "base_snapshot_id": request.ordered_snapshot_ids[0],
            "target_snapshot_id": request.ordered_snapshot_ids[1],
            "scope_id": request.scope_id,
            "path_prefix": request.path_or_prefix,
        }
    else:
        raise AgentRouteGrantError("Context route operation is invalid")
    pack_request = build_pack_request(
        {
            "profile": "balanced-v1",
            "source": source,
            "user_intent": {"question": question},
        }
    )
    limit = request.bounds.limit
    context_budget = replace(
        pack_request.context_budget,
        max_explicit_facts=min(pack_request.context_budget.max_explicit_facts, limit),
        max_hierarchy_items=min(pack_request.context_budget.max_hierarchy_items, limit),
        max_overlays=min(pack_request.context_budget.max_overlays, limit),
        max_expansion_descriptors=min(
            pack_request.context_budget.max_expansion_descriptors, limit
        ),
    )
    projection_budget = pack_request.projection_policy.budget
    bounded_projection = replace(
        projection_budget,
        explicit_entry_total=min(int(projection_budget.explicit_entry_total), limit),
        hierarchy_node_total=min(int(projection_budget.hierarchy_node_total), limit),
        tracking_item_total=min(int(projection_budget.tracking_item_total), limit),
        relation_component_total=min(int(projection_budget.relation_component_total), limit),
        duplicate_alias_component_total=min(
            int(projection_budget.duplicate_alias_component_total), limit
        ),
        members_per_component=min(int(projection_budget.members_per_component), limit),
        priority_quotas=tuple(
            (name, min(int(value), limit)) for name, value in projection_budget.priority_quotas
        ),
    )
    return replace(
        pack_request,
        projection_policy=replace(pack_request.projection_policy, budget=bounded_projection),
        context_budget=context_budget,
    )


def _value_at_pointer(value: JsonObject, pointer: str) -> str:
    current: Any = value
    for raw in pointer.split("/")[2:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    if not isinstance(current, str):
        raise TypeError
    return current


def _publication(
    route: StewardRouteOutcome,
    pack: AgentContextPack,
    safe_pack: JsonObject,
    exact_paths: tuple[str, ...],
) -> PublicationEnvelope:
    facts = (
        PublicationFact("context_pack_digest", pack.pack_digest),
        PublicationFact("context_packet_digest", pack.context_packet_digest),
        PublicationFact("pack_schema_version", str(pack.schema_version)),
        PublicationFact("presentation_status", pack.presentation_status.value),
        PublicationFact("projection_digest", pack.projection_digest),
        PublicationFact("source_kind", pack.source_kind.value),
        PublicationFact("task_domain", pack.context_packet.task_domain.value),
    )
    provenance = tuple(
        PublicationSourceProvenance(
            item.snapshot_id,
            item.snapshot_digest,
            item.persistent_run_id,
            item.evidence_id,
        )
        for item in pack.source_provenance
    )
    included = tuple(
        PublicationAccounting(field.name, getattr(pack.included_counts, field.name))
        for field in fields(pack.included_counts)
    )
    omitted = tuple(
        PublicationAccounting(item.category.lower(), item.omitted_count)
        for item in pack.omitted_counts
    )
    exact = tuple(
        PublicationExactInteger(path, _value_at_pointer(safe_pack, path))
        for path in exact_paths
    )
    return build_publication_envelope(
        route,
        status=PublicationStatus.OK,
        deterministic_facts=facts,
        source_provenance=provenance,
        exact_integer_encoding=exact,
        inclusion_accounting=included,
        omission_accounting=omitted,
        authority_boundary=(
            AuthorityBoundary.BOUNDED_RESULT,
            AuthorityBoundary.HISTORICAL_NOT_CURRENT,
            AuthorityBoundary.NO_CURRENT_FILESYSTEM_AUTHORITY,
            AuthorityBoundary.NO_LIFECYCLE_AUTHORITY,
            AuthorityBoundary.NO_WRITE_AUTHORITY,
        ),
    )


def _failure_block(route: StewardRouteOutcome, code: str, message: str) -> str:
    missing = ", ".join(route.missing_fields) if route.missing_fields else "None"
    return "\n".join(
        (
            "## STEWARD Deterministic Route Block",
            "",
            f"- Route decision: `{route.decision.value}`",
            f"- Operation kind: `{route.operation_kind}`",
            f"- Operation identity: `{route.request_digest}`",
            f"- Error code: `{code}`",
            f"- Safe message: {json.dumps(message, ensure_ascii=False)}",
            f"- Missing fields: `{missing}`",
            "- Business result: `NONE`",
            "",
        )
    )


class AgentContextRouteDispatcher:
    """Expose one closed Context operation and consume its grant before product access."""

    def __init__(
        self,
        config_path: Path,
        *,
        max_result_bytes: int = MAX_STRUCTURED_RESULT_BYTES,
        timeout_seconds: float = OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        self._config_path = config_path
        self._max_result_bytes = max_result_bytes
        self._timeout_seconds = timeout_seconds
        self._grant_guard = RouteGrantGuard()

    async def dispatch(self, tool_name: str, arguments: object) -> CallToolResult:
        if tool_name != TOOL_NAME:
            return self._argument_failure("STEWARD_AGENT_MCP_ARGUMENT_INVALID")
        if not isinstance(arguments, dict) or any(
            Draft202012Validator(INPUT_SCHEMA).iter_errors(arguments)
        ):
            return self._argument_failure("STEWARD_AGENT_MCP_ARGUMENT_INVALID")
        try:
            request = _route_request(arguments)
            route = route_steward_operation(request)
        except Exception:
            return self._argument_failure("STEWARD_AGENT_MCP_ARGUMENT_INVALID")
        if route.decision == RouteDecision.CORE:
            return self._routed_failure(route, "STEWARD_ROUTE_CORE_REQUIRED")
        if route.decision == RouteDecision.CLARIFY:
            return self._routed_failure(
                route,
                "STEWARD_ROUTE_CLARIFICATION_REQUIRED",
                missing_fields=route.missing_fields,
            )
        if route.decision == RouteDecision.UNSUPPORTED:
            return self._routed_failure(route, "STEWARD_ROUTE_UNSUPPORTED")
        grant = route.grant
        if grant is None:
            return self._routed_failure(route, "STEWARD_ROUTE_GRANT_INVALID")
        try:
            self._grant_guard.consume(request, grant)
            pack_request = _pack_request(request, arguments["question"])
        except AgentRouteGrantError:
            return self._routed_failure(route, "STEWARD_ROUTE_GRANT_INVALID")
        try:
            with anyio.fail_after(self._timeout_seconds):
                pack = await anyio.to_thread.run_sync(
                    lambda: prepare_agent_context(load_config(self._config_path), pack_request),
                    abandon_on_cancel=True,
                )
        except TimeoutError:
            return self._routed_failure(route, "STEWARD_AGENT_MCP_TIMEOUT")
        except AgentContextError as error:
            return self._routed_failure(route, error.code, cause_code=error.cause_code)
        except ConfigurationError as error:
            return self._routed_failure(
                route,
                "STEWARD_AGENT_MCP_SOURCE_UNAVAILABLE",
                cause_code=error.code,
            )
        except Exception:
            return self._routed_failure(route, "STEWARD_AGENT_MCP_UNAVAILABLE")
        try:
            raw_pack = agent_context_pack_machine_object(pack)
            safe_pack_value, exact_paths = model_safe_json(raw_pack, pointer="/context_pack")
            if not isinstance(safe_pack_value, dict):
                raise TypeError
            publication = _publication(route, pack, safe_pack_value, exact_paths)
            envelope: JsonObject = {
                "schema_name": ADAPTER_SCHEMA_NAME,
                "schema_version": ADAPTER_SCHEMA_VERSION,
                "tool_name": TOOL_NAME,
                "status": "OK",
                "route": route_outcome_machine_object(route),
                "publication": publication_envelope_machine_object(publication),
                "context_pack": safe_pack_value,
                "exact_integer_encoding": {
                    "scheme": EXACT_INTEGER_ENCODING_SCHEME,
                    "decimal_string_paths": list(exact_paths),
                },
                "fact_block_markdown": publication.fact_block_markdown,
                "fact_block_sha256": publication.fact_block_sha256,
                "error": None,
            }
            return self._publish(envelope, publication.fact_block_markdown, is_error=False)
        except Exception:
            return self._routed_failure(route, "STEWARD_AGENT_MCP_UNAVAILABLE")

    def _argument_failure(self, code: str) -> CallToolResult:
        route = StewardRouteOutcome(
            "local_steward.agent_route_outcome",
            1,
            RouteDecision.UNSUPPORTED,
            "INVALID",
            "0" * 64,
            (),
            None,
        )
        return self._routed_failure(route, code)

    def _routed_failure(
        self,
        route: StewardRouteOutcome,
        code: str,
        *,
        cause_code: str | None = None,
        missing_fields: tuple[str, ...] = (),
    ) -> CallToolResult:
        message = _safe_message(code)
        publication = None
        if route.decision in (RouteDecision.CORE, RouteDecision.CONTEXT):
            try:
                publication = build_publication_envelope(
                    route,
                    status=PublicationStatus.ERROR,
                    typed_error=PublicationTypedError(code, message),
                    authority_boundary=(AuthorityBoundary.NO_BUSINESS_RESULT,),
                )
            except Exception:
                publication = None
        block = (
            publication.fact_block_markdown
            if publication is not None
            else _failure_block(route, code, message)
        )
        error: JsonObject = {"code": code, "message": message}
        if cause_code:
            error["cause_code"] = cause_code
        if missing_fields:
            error["missing_fields"] = list(missing_fields)
        envelope: JsonObject = {
            "schema_name": ADAPTER_SCHEMA_NAME,
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "tool_name": TOOL_NAME,
            "status": "ERROR",
            "route": route_outcome_machine_object(route),
            "publication": (
                publication_envelope_machine_object(publication)
                if publication is not None
                else None
            ),
            "context_pack": None,
            "exact_integer_encoding": {
                "scheme": EXACT_INTEGER_ENCODING_SCHEME,
                "decimal_string_paths": [],
            },
            "fact_block_markdown": block,
            "fact_block_sha256": sha256(block.encode("utf-8")).hexdigest(),
            "error": error,
        }
        return self._publish(envelope, block, is_error=True)

    def _publish(self, envelope: JsonObject, text: str, *, is_error: bool) -> CallToolResult:
        oversized = len(canonical_json(envelope)) > self._max_result_bytes
        invalid = any(Draft202012Validator(OUTPUT_SCHEMA).iter_errors(envelope))
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES or invalid or (
            oversized and envelope["error"] is None
        ):
            if envelope["error"] is None:
                route_value = envelope["route"]
                route = StewardRouteOutcome(
                    route_value["schema_name"],
                    route_value["schema_version"],
                    RouteDecision(route_value["decision"]),
                    route_value["operation_kind"],
                    route_value["request_digest"],
                    tuple(route_value["missing_fields"]),
                    None,
                )
                return self._routed_failure(route, "STEWARD_AGENT_MCP_RESOURCE_LIMIT")
            raise RuntimeError("Invalid STEWARD failure envelope")
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=envelope,
            isError=is_error,
        )
