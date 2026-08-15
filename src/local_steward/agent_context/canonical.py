"""Strict canonical conversion and digesting for Agent Context Pack v2."""

from dataclasses import fields, is_dataclass, replace
from enum import Enum
from hashlib import sha256
from typing import TypeAlias

from ..evidence import canonical_json
from .errors import AgentContextCanonicalError
from .models import AGENT_CONTEXT_PACK_DIGEST_DOMAIN, AgentContextPack


JsonValue: TypeAlias = str | int | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


def machine_value(value: object) -> JsonValue:
    """Convert only strict immutable Pack facts to ordinary JSON values."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Enum):
        if isinstance(value.value, (str, int, bool)):
            return value.value
        raise AgentContextCanonicalError("Agent Context enum value is invalid")
    if isinstance(value, tuple):
        return [machine_value(item) for item in value]
    if is_dataclass(value):
        return {
            field.name: machine_value(item)
            for field in fields(value)
            if (item := getattr(value, field.name)) is not None
        }
    raise AgentContextCanonicalError("Agent Context canonical value is invalid")


def agent_context_pack_machine_object(
    pack: AgentContextPack, *, include_digest: bool = True
) -> dict[str, JsonValue]:
    value = machine_value(pack)
    if not isinstance(value, dict):
        raise AgentContextCanonicalError("Agent Context Pack is invalid")
    if not include_digest:
        value.pop("pack_digest", None)
    return value


def canonical_agent_context_pack(
    pack: AgentContextPack, *, include_digest: bool = False
) -> bytes:
    return canonical_json(agent_context_pack_machine_object(pack, include_digest=include_digest))


def agent_context_pack_digest(pack: AgentContextPack) -> str:
    return sha256(
        AGENT_CONTEXT_PACK_DIGEST_DOMAIN.encode("utf-8")
        + b"\0"
        + canonical_agent_context_pack(pack)
    ).hexdigest()


def finalize_agent_context_pack(pack: AgentContextPack) -> AgentContextPack:
    return replace(pack, pack_digest=agent_context_pack_digest(pack))
