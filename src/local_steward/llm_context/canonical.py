"""Pure canonical conversion and packet digesting for LLM Context packets."""

from dataclasses import fields, is_dataclass, replace
from enum import Enum
from hashlib import sha256
from typing import TypeAlias

from ..evidence import canonical_json
from .errors import LLMContextCanonicalError
from .models import (
    CONTEXT_DIGEST_DOMAIN,
    REQUEST_CONSTRAINTS_DIGEST_DOMAIN,
    LLMContextPacket,
    RequestConstraints,
)


JsonValue: TypeAlias = str | int | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


def machine_value(value: object) -> JsonValue:
    """Convert only immutable packet/result facts into ordinary JSON values."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Enum):
        if isinstance(value.value, (str, int, bool)):
            return value.value
        raise LLMContextCanonicalError("LLM_CONTEXT_CANONICAL_VALUE_INVALID")
    if isinstance(value, tuple):
        return [machine_value(item) for item in value]
    if is_dataclass(value):
        return {
            field.name: machine_value(item)
            for field in fields(value)
            if (item := getattr(value, field.name)) is not None
        }
    raise LLMContextCanonicalError("LLM_CONTEXT_CANONICAL_VALUE_INVALID")


def packet_machine_object(packet: LLMContextPacket, *, include_digest: bool = True) -> dict[str, JsonValue]:
    value = machine_value(packet)
    if not isinstance(value, dict):
        raise LLMContextCanonicalError("LLM_CONTEXT_PACKET_INVALID")
    if not include_digest:
        value.pop("packet_digest", None)
    return value


def canonical_packet(packet: LLMContextPacket, *, include_digest: bool = False) -> bytes:
    return canonical_json(packet_machine_object(packet, include_digest=include_digest))


def packet_digest(packet: LLMContextPacket) -> str:
    return sha256(CONTEXT_DIGEST_DOMAIN.encode("utf-8") + b"\0" + canonical_packet(packet)).hexdigest()


def finalize_packet(packet: LLMContextPacket) -> LLMContextPacket:
    return replace(packet, packet_digest=packet_digest(packet))


def request_constraints_machine_object(value: RequestConstraints) -> dict[str, JsonValue]:
    """Render only the safe dynamic registry identity for one request."""
    return {
        "capability_classes": [item.value for item in value.capability_classes],
        "empty_registry_rule": value.empty_registry_rule,
        "evidence_array_unique": value.evidence_array_unique,
        "evidence_token_wire_type": value.evidence_token_wire_type,
        "evidence_tokens": list(value.evidence_tokens),
        "excluded_capability_classes": [item.value for item in value.excluded_capability_classes],
        "expansion_target_tokens": list(value.expansion_target_tokens),
        "schema_version": value.schema_version,
        "task_domain": value.task_domain.value,
        "token_order_rule": value.token_order_rule,
        "top_level_reference_rule": value.top_level_reference_rule,
    }


def canonical_request_constraints(value: RequestConstraints) -> bytes:
    return canonical_json(request_constraints_machine_object(value))


def request_constraints_digest(value: RequestConstraints) -> str:
    return sha256(
        REQUEST_CONSTRAINTS_DIGEST_DOMAIN.encode("utf-8") + b"\0" + canonical_request_constraints(value)
    ).hexdigest()
