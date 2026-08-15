"""Immutable public models for one bounded historical Agent Context Pack."""

from dataclasses import dataclass
from enum import Enum

from ..file_agent.models import SourceKind
from ..llm_context.models import ContextBudget, LLMContextPacket, UserIntentContext
from ..observation_projection.models import (
    PairTrackingRequest,
    ProjectionPolicy,
    SnapshotDiagnosticRequest,
)


AGENT_CONTEXT_PACK_SCHEMA_NAME = "local_steward.agent_context_pack"
AGENT_CONTEXT_PACK_SCHEMA_VERSION = 2
AGENT_CONTEXT_PACK_DIGEST_DOMAIN = "local_steward.agent_context_pack.v2"

MAX_AGENT_CONTEXT_PACK_BYTES = 262_144
MAX_AGENT_CONTEXT_INTENT_BYTES = 16_384

MAX_PROJECTION_EXPLICIT_ENTRIES = 64
MAX_PROJECTION_HIERARCHY_NODES = 64
MAX_PROJECTION_TRACKING_ITEMS = 64
MAX_PROJECTION_RELATION_COMPONENTS = 32
MAX_PROJECTION_DUPLICATE_ALIAS_COMPONENTS = 32
MAX_PROJECTION_MEMBERS_PER_COMPONENT = 16
MAX_PROJECTION_SCOPE_MINIMUM = 8
MAX_PROJECTION_PRIORITY_QUOTAS = 16
MAX_PROJECTION_PRIORITY_QUOTA_VALUE = 64
MAX_PROJECTION_SERIALIZED_BYTES_SOFT = 200_000

MAX_CONTEXT_EXPLICIT_FACTS = 32
MAX_CONTEXT_HIERARCHY_ITEMS = 32
MAX_CONTEXT_OVERLAYS = 16
MAX_CONTEXT_EXPANSION_DESCRIPTORS = 16


class AgentContextPackKind(str, Enum):
    HISTORICAL_PROJECTION_CONTEXT = "HISTORICAL_PROJECTION_CONTEXT"


class AgentContextPresentationStatus(str, Enum):
    NO_SECOND_STAGE_OMISSIONS = "NO_SECOND_STAGE_OMISSIONS"
    SECOND_STAGE_OMISSIONS_PRESENT = "SECOND_STAGE_OMISSIONS_PRESENT"


@dataclass(frozen=True, slots=True)
class AgentContextPackRequest:
    projection_request: SnapshotDiagnosticRequest | PairTrackingRequest
    projection_policy: ProjectionPolicy
    user_intent: UserIntentContext
    context_budget: ContextBudget


@dataclass(frozen=True, slots=True)
class AgentContextIncludedCounts:
    explicit_entry_anchor_count: int
    tracking_item_count: int
    hierarchy_item_count: int
    growth_hierarchy_item_count: int
    overlay_count: int
    expansion_descriptor_count: int
    evidence_reference_count: int


@dataclass(frozen=True, slots=True)
class AgentContextOmittedCount:
    category: str
    omitted_count: int
    source_representation: str


@dataclass(frozen=True, slots=True)
class AgentContextSourceProvenance:
    """Verified persistent authority behind one Pack Snapshot source."""

    snapshot_id: str
    snapshot_digest: str
    persistent_run_id: str
    evidence_id: str
    evidence_relative_path: str


@dataclass(frozen=True, slots=True)
class AgentContextPack:
    schema_name: str
    schema_version: int
    pack_kind: AgentContextPackKind
    presentation_status: AgentContextPresentationStatus
    source_kind: SourceKind
    snapshot_ids: tuple[str, ...]
    scope_id: str | None
    source_provenance: tuple[AgentContextSourceProvenance, ...]
    projection_digest: str
    context_protocol_version: int
    context_packet_digest: str
    context_packet: LLMContextPacket
    included_counts: AgentContextIncludedCounts
    omitted_counts: tuple[AgentContextOmittedCount, ...]
    pack_digest: str


@dataclass(frozen=True, slots=True)
class AgentContextValidationViolation:
    code: str
