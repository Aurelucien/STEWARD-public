"""Non-authoritative host-thread correlation for native MCP results."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from typing import Any


THREAD_ATTRIBUTION_SCHEMA_NAME = "local_steward.thread_attribution"
THREAD_ATTRIBUTION_SCHEMA_VERSION = 1
HOST_SESSION_META_KEY = "openai/session"
MAX_HOST_SESSION_CHARS = 256
_SIGNATURE_DOMAIN = b"local_steward.thread_attribution.v1"


def _admissible_reference(value: object) -> str | None:
    if not isinstance(value, str) or not value or value.strip() != value:
        return None
    if len(value) > MAX_HOST_SESSION_CHARS:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    return value


def _signature(reference: str) -> str:
    return hashlib.sha256(_SIGNATURE_DOMAIN + b"\0" + reference.encode("utf-8")).hexdigest()


def thread_attribution_machine_object(
    request_meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Publish optional host correlation without turning it into authority.

    The value is read only from MCP request metadata. Tool arguments never
    participate in this object, and no field affects admission or approval.
    """

    raw = request_meta.get(HOST_SESSION_META_KEY) if request_meta is not None else None
    reference = _admissible_reference(raw)
    if reference is None:
        reason = (
            "MCP_CLIENT_SESSION_META_ABSENT"
            if raw is None
            else "MCP_CLIENT_SESSION_META_INVALID"
        )
        status = "HOST_UNAVAILABLE"
        source = "NONE"
        signature = None
    else:
        reason = None
        status = "HOST_BOUND"
        source = "MCP_REQUEST_META_OPENAI_SESSION"
        signature = _signature(reference)
    return {
        "schema_name": THREAD_ATTRIBUTION_SCHEMA_NAME,
        "schema_version": THREAD_ATTRIBUTION_SCHEMA_VERSION,
        "status": status,
        "host_kind": "CODEX",
        "thread_reference": reference,
        "thread_signature": signature,
        "reference_semantics": (
            "ANONYMIZED_CONVERSATION_ID" if reference is not None else "UNAVAILABLE"
        ),
        "source": source,
        "source_field": HOST_SESSION_META_KEY,
        "verification": "HOST_REPORTED_NOT_ATTESTED",
        "correlation_only": True,
        "authorization_effect": "NONE",
        "model_supplied": False,
        "reason_code": reason,
    }
