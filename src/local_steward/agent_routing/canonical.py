"""Canonical conversion and identities for Agent routing and publication."""

from dataclasses import fields, is_dataclass
from enum import Enum
from hashlib import sha256
from typing import TypeAlias

from ..evidence import canonical_json
from .errors import AgentRoutingCanonicalError
from .models import (
    ROUTE_GRANT_DIGEST_DOMAIN,
    ROUTE_REQUEST_DIGEST_DOMAIN,
    PublicationEnvelope,
    StewardRouteGrant,
    StewardRouteOutcome,
    StewardRouteRequest,
)


JsonValue: TypeAlias = str | int | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


def machine_value(value: object) -> JsonValue:
    """Convert only strict immutable routing/publication values to JSON values."""
    if value is None:
        return None
    if isinstance(value, Enum):
        if isinstance(value.value, (str, int, bool)):
            return value.value
        raise AgentRoutingCanonicalError("Agent routing enum value is invalid")
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, tuple):
        return [machine_value(item) for item in value]
    if is_dataclass(value):
        return {field.name: machine_value(getattr(value, field.name)) for field in fields(value)}
    raise AgentRoutingCanonicalError("Agent routing canonical value is invalid")


def route_request_machine_object(request: StewardRouteRequest) -> dict[str, JsonValue]:
    value = machine_value(request)
    if not isinstance(value, dict):
        raise AgentRoutingCanonicalError("Agent route request is invalid")
    return value


def canonical_route_request(request: StewardRouteRequest) -> bytes:
    return canonical_json(route_request_machine_object(request))


def route_request_digest(request: StewardRouteRequest) -> str:
    return sha256(
        ROUTE_REQUEST_DIGEST_DOMAIN.encode("utf-8") + b"\0" + canonical_route_request(request)
    ).hexdigest()


def route_outcome_machine_object(outcome: StewardRouteOutcome) -> dict[str, JsonValue]:
    value = machine_value(outcome)
    if not isinstance(value, dict):
        raise AgentRoutingCanonicalError("Agent route outcome is invalid")
    return value


def route_grant_machine_object(
    grant: StewardRouteGrant, *, include_grant_id: bool = True
) -> dict[str, JsonValue]:
    value = machine_value(grant)
    if not isinstance(value, dict):
        raise AgentRoutingCanonicalError("Agent route grant is invalid")
    if not include_grant_id:
        value.pop("grant_id", None)
    return value


def route_grant_digest(grant: StewardRouteGrant) -> str:
    payload = canonical_json(route_grant_machine_object(grant, include_grant_id=False))
    return sha256(ROUTE_GRANT_DIGEST_DOMAIN.encode("utf-8") + b"\0" + payload).hexdigest()


def publication_envelope_machine_object(
    envelope: PublicationEnvelope,
) -> dict[str, JsonValue]:
    value = machine_value(envelope)
    if not isinstance(value, dict):
        raise AgentRoutingCanonicalError("Agent publication envelope is invalid")
    return value


def canonical_publication_envelope(envelope: PublicationEnvelope) -> bytes:
    return canonical_json(publication_envelope_machine_object(envelope))


def fact_block_digest(markdown: str) -> str:
    return sha256(markdown.encode("utf-8")).hexdigest()
