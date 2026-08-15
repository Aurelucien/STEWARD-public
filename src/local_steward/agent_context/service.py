"""Provider-free construction of one bounded historical Agent Context Pack."""

from dataclasses import fields

from ..errors import StewardError
from ..evidence import canonical_json
from ..file_agent.models import SourceKind
from ..llm_context import ContextBudget, UserIntentContext, build_context_packet
from ..llm_context.canonical import machine_value as context_machine_value
from ..llm_context.errors import (
    LLMContextCanonicalError,
    LLMContextError,
    LLMContextInvariantError,
    LLMContextRequestError,
    LLMUnsupportedTaskDomainError,
)
from ..llm_context.validation import validate_context_packet
from ..llm_context.models import LLMContextPacket
from ..models import StewardConfig
from ..observation_projection import (
    PairTrackingRequest,
    ProjectionPolicy,
    SnapshotDiagnosticRequest,
    build_pair_tracking_projection,
    build_snapshot_diagnostic_projection,
)
from ..observation_projection.errors import (
    ObservationProjectionCanonicalError,
    ObservationProjectionInvariantError,
    ObservationProjectionRequestError,
)
from ..snapshots import _verified_snapshot_detail
from .canonical import canonical_agent_context_pack, finalize_agent_context_pack
from .errors import (
    AgentContextCanonicalError,
    AgentContextError,
    AgentContextInvariantError,
    AgentContextRequestError,
    AgentContextResourceError,
    AgentContextSourceError,
    AgentContextSourceUnavailableError,
    AgentContextSourceUnsupportedError,
    AgentContextUnavailableError,
)
from .models import (
    AGENT_CONTEXT_PACK_SCHEMA_NAME,
    AGENT_CONTEXT_PACK_SCHEMA_VERSION,
    MAX_AGENT_CONTEXT_INTENT_BYTES,
    MAX_AGENT_CONTEXT_PACK_BYTES,
    MAX_CONTEXT_EXPANSION_DESCRIPTORS,
    MAX_CONTEXT_EXPLICIT_FACTS,
    MAX_CONTEXT_HIERARCHY_ITEMS,
    MAX_CONTEXT_OVERLAYS,
    MAX_PROJECTION_DUPLICATE_ALIAS_COMPONENTS,
    MAX_PROJECTION_EXPLICIT_ENTRIES,
    MAX_PROJECTION_HIERARCHY_NODES,
    MAX_PROJECTION_MEMBERS_PER_COMPONENT,
    MAX_PROJECTION_PRIORITY_QUOTAS,
    MAX_PROJECTION_PRIORITY_QUOTA_VALUE,
    MAX_PROJECTION_RELATION_COMPONENTS,
    MAX_PROJECTION_SCOPE_MINIMUM,
    MAX_PROJECTION_SERIALIZED_BYTES_SOFT,
    MAX_PROJECTION_TRACKING_ITEMS,
    AgentContextPack,
    AgentContextPackKind,
    AgentContextPackRequest,
    AgentContextSourceProvenance,
)
from .validation import (
    included_counts,
    omitted_counts,
    presentation_status,
    source_summary,
    source_identities,
    validate_agent_context_pack,
)


_PROJECTION_LIMITS = {
    "explicit_entry_total": MAX_PROJECTION_EXPLICIT_ENTRIES,
    "hierarchy_node_total": MAX_PROJECTION_HIERARCHY_NODES,
    "tracking_item_total": MAX_PROJECTION_TRACKING_ITEMS,
    "relation_component_total": MAX_PROJECTION_RELATION_COMPONENTS,
    "duplicate_alias_component_total": MAX_PROJECTION_DUPLICATE_ALIAS_COMPONENTS,
    "members_per_component": MAX_PROJECTION_MEMBERS_PER_COMPONENT,
    "scope_minimum_guarantee": MAX_PROJECTION_SCOPE_MINIMUM,
    "serialized_bytes_soft": MAX_PROJECTION_SERIALIZED_BYTES_SOFT,
}

_CONTEXT_LIMITS = {
    "max_explicit_facts": MAX_CONTEXT_EXPLICIT_FACTS,
    "max_hierarchy_items": MAX_CONTEXT_HIERARCHY_ITEMS,
    "max_overlays": MAX_CONTEXT_OVERLAYS,
    "max_expansion_descriptors": MAX_CONTEXT_EXPANSION_DESCRIPTORS,
}


def _admitted_integer(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise AgentContextRequestError(f"Agent Context {label} is invalid")
    return value


def _validate_request(request: AgentContextPackRequest) -> None:
    if not isinstance(request, AgentContextPackRequest):
        raise AgentContextRequestError("Agent Context request is invalid")
    if not isinstance(request.projection_request, (SnapshotDiagnosticRequest, PairTrackingRequest)):
        raise AgentContextSourceUnsupportedError("Agent Context source mode is unsupported")
    if not isinstance(request.projection_policy, ProjectionPolicy):
        raise AgentContextRequestError("Agent Context Projection policy is invalid")
    if not isinstance(request.user_intent, UserIntentContext):
        raise AgentContextRequestError("Agent Context user intent is invalid")
    if not isinstance(request.context_budget, ContextBudget):
        raise AgentContextRequestError("Agent Context budget is invalid")
    policy = request.projection_policy
    budget = getattr(policy, "budget", None)
    if budget is None:
        raise AgentContextRequestError("Agent Context Projection policy is invalid")
    for name, maximum in _PROJECTION_LIMITS.items():
        value = _admitted_integer(
            getattr(budget, name, None), label="Projection budget"
        )
        if value > maximum:
            raise AgentContextResourceError("Agent Context Projection budget exceeds the product limit")
    quotas = getattr(budget, "priority_quotas", None)
    if not isinstance(quotas, tuple):
        raise AgentContextRequestError("Agent Context Projection quota is invalid")
    if len(quotas) > MAX_PROJECTION_PRIORITY_QUOTAS:
        raise AgentContextResourceError("Agent Context Projection quota exceeds the product limit")
    for item in quotas:
        if not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str):
            raise AgentContextRequestError("Agent Context Projection quota is invalid")
        value = _admitted_integer(item[1], label="Projection quota")
        if value > MAX_PROJECTION_PRIORITY_QUOTA_VALUE:
            raise AgentContextResourceError("Agent Context Projection quota exceeds the product limit")
    context_budget = request.context_budget
    for field in fields(context_budget):
        maximum = _CONTEXT_LIMITS[field.name]
        value = _admitted_integer(
            getattr(context_budget, field.name), label="budget"
        )
        if value > maximum:
            raise AgentContextResourceError("Agent Context budget exceeds the product limit")
    try:
        intent_bytes = canonical_json(context_machine_value(request.user_intent))
    except Exception as error:
        raise AgentContextRequestError("Agent Context user intent is invalid") from error
    if len(intent_bytes) > MAX_AGENT_CONTEXT_INTENT_BYTES:
        raise AgentContextResourceError("Agent Context user intent exceeds the product limit")


def _projection(config: StewardConfig, request: AgentContextPackRequest):  # type: ignore[no-untyped-def]
    try:
        if isinstance(request.projection_request, SnapshotDiagnosticRequest):
            return build_snapshot_diagnostic_projection(
                config, request.projection_request, request.projection_policy
            )
        if isinstance(request.projection_request, PairTrackingRequest):
            return build_pair_tracking_projection(
                config, request.projection_request, request.projection_policy
            )
        raise AgentContextSourceUnsupportedError("Agent Context source mode is unsupported")
    except AgentContextError:
        raise
    except ObservationProjectionRequestError as error:
        raise AgentContextSourceError(
            "Agent Context historical source request is invalid", cause_code=error.code
        ) from error
    except ObservationProjectionInvariantError as error:
        raise AgentContextInvariantError(
            "Agent Context Projection invariant failed", cause_code=error.code
        ) from error
    except ObservationProjectionCanonicalError as error:
        raise AgentContextCanonicalError(
            "Agent Context Projection canonicalization failed", cause_code=error.code
        ) from error
    except StewardError as error:
        raise AgentContextSourceUnavailableError(
            "Agent Context historical source is unavailable", cause_code=error.code
        ) from error
    except Exception as error:
        raise AgentContextUnavailableError("Agent Context source preparation failed") from error


def _packet(projection, request: AgentContextPackRequest):  # type: ignore[no-untyped-def]
    try:
        packet = build_context_packet(
            projection, request.user_intent, request.context_budget
        )
        violations = validate_context_packet(packet)
        if violations:
            raise AgentContextInvariantError(
                "Agent Context Packet validation failed", cause_code=violations[0].code
            )
        return packet
    except AgentContextError:
        raise
    except LLMUnsupportedTaskDomainError as error:
        raise AgentContextSourceUnsupportedError(
            "Agent Context task domain is unsupported", cause_code=error.code
        ) from error
    except LLMContextRequestError as error:
        raise AgentContextRequestError(
            "Agent Context Packet request is invalid", cause_code=error.code
        ) from error
    except LLMContextInvariantError as error:
        raise AgentContextInvariantError(
            "Agent Context Packet invariant failed", cause_code=error.code
        ) from error
    except LLMContextCanonicalError as error:
        raise AgentContextCanonicalError(
            "Agent Context Packet canonicalization failed", cause_code=error.code
        ) from error
    except LLMContextError as error:
        raise AgentContextInvariantError(
            "Agent Context Packet construction failed", cause_code=error.code
        ) from error
    except Exception as error:
        raise AgentContextUnavailableError("Agent Context Packet construction failed") from error


def _source_provenance(
    config: StewardConfig, packet: LLMContextPacket
) -> tuple[AgentContextSourceProvenance, ...]:
    """Load verified persistent identities without publishing unverified index facts."""
    try:
        identities = source_identities(packet)
        values: list[AgentContextSourceProvenance] = []
        for identity in identities:
            verification, snapshot = _verified_snapshot_detail(config, identity.snapshot_id)
            if verification.status != "VALID":
                raise AgentContextSourceError(
                    "Agent Context historical source is invalid",
                    cause_code="SNAPSHOT_REPOSITORY_INVALID",
                )
            if (
                snapshot.snapshot_id != identity.snapshot_id
                or snapshot.snapshot_digest != identity.snapshot_digest
                or verification.persistent_run_id != snapshot.run_id
                or verification.evidence_id != snapshot.evidence_id
                or not snapshot.run_id
                or not snapshot.evidence_id
                or not snapshot.evidence_relative_path
            ):
                raise AgentContextInvariantError(
                    "Agent Context source provenance is inconsistent",
                    cause_code="SOURCE_PROVENANCE_MISMATCH",
                )
            values.append(
                AgentContextSourceProvenance(
                    snapshot.snapshot_id,
                    snapshot.snapshot_digest,
                    snapshot.run_id,
                    snapshot.evidence_id,
                    snapshot.evidence_relative_path,
                )
            )
        return tuple(values)
    except AgentContextError:
        raise
    except StewardError as error:
        raise AgentContextSourceUnavailableError(
            "Agent Context historical source is unavailable", cause_code=error.code
        ) from error
    except Exception as error:
        raise AgentContextUnavailableError("Agent Context source provenance failed") from error


def prepare_agent_context(
    config: StewardConfig,
    request: AgentContextPackRequest,
) -> AgentContextPack:
    """Build one immutable bounded Pack without providers, persistence, or live Scope access."""
    _validate_request(request)
    projection = _projection(config, request)
    packet = _packet(projection, request)
    snapshot_ids, scope_id = source_summary(packet)
    if not snapshot_ids:
        raise AgentContextSourceUnsupportedError("Agent Context task domain is unsupported")
    provenance = _source_provenance(config, packet)
    try:
        pack = finalize_agent_context_pack(
            AgentContextPack(
                AGENT_CONTEXT_PACK_SCHEMA_NAME,
                AGENT_CONTEXT_PACK_SCHEMA_VERSION,
                AgentContextPackKind.HISTORICAL_PROJECTION_CONTEXT,
                presentation_status(packet),
                SourceKind.DERIVED_PROJECTION,
                snapshot_ids,
                scope_id,
                provenance,
                packet.projection_digest,
                packet.protocol_version,
                packet.packet_digest,
                packet,
                included_counts(packet),
                omitted_counts(packet),
                "",
            )
        )
        violations = validate_agent_context_pack(pack)
        non_resource = tuple(
            item for item in violations if item.code != "PACK_RESOURCE_LIMIT_EXCEEDED"
        )
        if non_resource:
            raise AgentContextInvariantError(
                "Agent Context Pack validation failed", cause_code=non_resource[0].code
            )
        if violations:
            raise AgentContextResourceError("Agent Context Pack exceeds the product limit")
        if len(canonical_agent_context_pack(pack, include_digest=True)) > MAX_AGENT_CONTEXT_PACK_BYTES:
            raise AgentContextResourceError("Agent Context Pack exceeds the product limit")
        return pack
    except AgentContextError:
        raise
    except Exception as error:
        raise AgentContextCanonicalError("Agent Context Pack canonicalization failed") from error
