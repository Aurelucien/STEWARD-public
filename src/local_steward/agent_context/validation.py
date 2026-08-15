"""Deterministic parity and integrity validation for Agent Context Pack v2."""

from pathlib import PurePosixPath
from uuid import UUID

from .canonical import agent_context_pack_digest, canonical_agent_context_pack
from .models import (
    AGENT_CONTEXT_PACK_SCHEMA_NAME,
    AGENT_CONTEXT_PACK_SCHEMA_VERSION,
    MAX_AGENT_CONTEXT_PACK_BYTES,
    AgentContextIncludedCounts,
    AgentContextOmittedCount,
    AgentContextPack,
    AgentContextPackKind,
    AgentContextPresentationStatus,
    AgentContextSourceProvenance,
    AgentContextValidationViolation,
)
from ..file_agent.models import SourceKind
from ..llm_context.models import LLMContextPacket, LLMTaskDomain
from ..llm_context.validation import validate_context_packet
from ..observation_projection.models import ProjectionMode, SnapshotSourceIdentity


def source_identities(packet: LLMContextPacket) -> tuple[SnapshotSourceIdentity, ...]:
    """Return ordered Snapshot source identities for the supported Pack domains."""
    source = packet.source_identity
    if (
        packet.projection_mode == ProjectionMode.SNAPSHOT_DIAGNOSTIC
        and packet.task_domain == LLMTaskDomain.STATIC_SNAPSHOT
        and source.primary_snapshot is not None
        and source.snapshot_pair is None
    ):
        return (source.primary_snapshot,)
    if (
        packet.projection_mode == ProjectionMode.PAIR_TRACKING
        and packet.task_domain == LLMTaskDomain.STATIC_PAIR_COMPARISON
        and source.primary_snapshot is None
        and source.snapshot_pair is not None
    ):
        return (source.snapshot_pair.base, source.snapshot_pair.target)
    return ()


def _canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _source_provenance_value_valid(value: AgentContextSourceProvenance) -> bool:
    digest = value.snapshot_digest
    relative = PurePosixPath(value.evidence_relative_path)
    return (
        _canonical_uuid(value.snapshot_id)
        and _canonical_uuid(value.persistent_run_id)
        and _canonical_uuid(value.evidence_id)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and not relative.is_absolute()
        and ".." not in relative.parts
        and len(relative.parts) == 3
        and relative.parts[0] == "runs"
        and relative.parts[1] == value.persistent_run_id
    )


def included_counts(packet: LLMContextPacket) -> AgentContextIncludedCounts:
    growth_count = (
        0 if packet.growth_hierarchy is None else len(packet.growth_hierarchy.hierarchy_items)
    )
    return AgentContextIncludedCounts(
        len(packet.explicit_entry_anchors),
        len(packet.tracking_items),
        len(packet.hierarchy_items),
        growth_count,
        len(packet.overlays),
        len(packet.expansion_descriptors),
        len(packet.evidence_references),
    )


def omitted_counts(packet: LLMContextPacket) -> tuple[AgentContextOmittedCount, ...]:
    return tuple(
        AgentContextOmittedCount(
            item.category.value,
            item.omitted_count,
            item.source_representation.value,
        )
        for item in sorted(packet.context_omissions, key=lambda value: value.category.value)
    )


def presentation_status(packet: LLMContextPacket) -> AgentContextPresentationStatus:
    if packet.context_omissions:
        return AgentContextPresentationStatus.SECOND_STAGE_OMISSIONS_PRESENT
    return AgentContextPresentationStatus.NO_SECOND_STAGE_OMISSIONS


def source_summary(packet: LLMContextPacket) -> tuple[tuple[str, ...], str | None]:
    identities = source_identities(packet)
    if not identities:
        return (), None
    return tuple(item.snapshot_id for item in identities), packet.normalized_request_scope.scope


def validate_agent_context_pack(
    pack: AgentContextPack,
) -> tuple[AgentContextValidationViolation, ...]:
    """Return stable violations without mutating or repairing the Pack."""
    if not isinstance(pack, AgentContextPack):
        return (AgentContextValidationViolation("PACK_TYPE_INVALID"),)
    codes: set[str] = set()

    def invalid(code: str) -> None:
        codes.add(code)

    if pack.schema_name != AGENT_CONTEXT_PACK_SCHEMA_NAME:
        invalid("PACK_SCHEMA_NAME_INVALID")
    if pack.schema_version != AGENT_CONTEXT_PACK_SCHEMA_VERSION:
        invalid("PACK_SCHEMA_VERSION_INVALID")
    if pack.pack_kind != AgentContextPackKind.HISTORICAL_PROJECTION_CONTEXT:
        invalid("PACK_KIND_UNSUPPORTED")
    if pack.source_kind != SourceKind.DERIVED_PROJECTION:
        invalid("PACK_SOURCE_KIND_INVALID")

    packet = pack.context_packet
    packet_violations = validate_context_packet(packet)
    if packet_violations:
        invalid("CONTEXT_PACKET_INVALID")
    if pack.context_protocol_version != packet.protocol_version:
        invalid("CONTEXT_PROTOCOL_VERSION_MISMATCH")
    if pack.context_packet_digest != packet.packet_digest:
        invalid("CONTEXT_PACKET_DIGEST_MISMATCH")
    if pack.projection_digest != packet.projection_digest:
        invalid("PROJECTION_DIGEST_MISMATCH")

    expected_ids, expected_scope = source_summary(packet)
    if not expected_ids:
        invalid("PACK_SOURCE_DOMAIN_UNSUPPORTED")
    if pack.snapshot_ids != expected_ids:
        invalid("PACK_SNAPSHOT_IDENTITY_MISMATCH")
    if pack.scope_id != expected_scope:
        invalid("PACK_SCOPE_IDENTITY_MISMATCH")
    expected_sources = source_identities(packet)
    if len(pack.source_provenance) != len(expected_sources):
        invalid("PACK_SOURCE_PROVENANCE_COUNT_MISMATCH")
    elif any(
        provenance.snapshot_id != identity.snapshot_id
        or provenance.snapshot_digest != identity.snapshot_digest
        or not _source_provenance_value_valid(provenance)
        for provenance, identity in zip(pack.source_provenance, expected_sources, strict=True)
    ):
        invalid("PACK_SOURCE_PROVENANCE_MISMATCH")
    if pack.presentation_status != presentation_status(packet):
        invalid("PACK_PRESENTATION_STATUS_MISMATCH")
    if pack.included_counts != included_counts(packet):
        invalid("PACK_INCLUDED_COUNT_MISMATCH")
    if pack.omitted_counts != omitted_counts(packet):
        invalid("PACK_OMISSION_COUNT_MISMATCH")
    try:
        if pack.pack_digest != agent_context_pack_digest(pack):
            invalid("PACK_DIGEST_INVALID")
        if len(canonical_agent_context_pack(pack, include_digest=True)) > MAX_AGENT_CONTEXT_PACK_BYTES:
            invalid("PACK_RESOURCE_LIMIT_EXCEEDED")
    except Exception:
        invalid("PACK_CANONICAL_INVALID")

    return tuple(AgentContextValidationViolation(code) for code in sorted(codes))
