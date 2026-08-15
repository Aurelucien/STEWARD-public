"""Deterministic safe machine conversion for Agent facade results."""

from __future__ import annotations

from typing import Any

from ..evidence import canonical_json
from ..output import to_jsonable


_FORBIDDEN_FIELDS = frozenset({"evidence_relative_path"})


def machine_result(value: Any) -> Any:
    """Convert existing models once and omit repository-internal evidence paths."""
    return _redact(to_jsonable(value))


def machine_bytes(value: Any) -> bytes:
    return canonical_json(machine_result(value))


def serialize_envelope(value: Any) -> bytes:
    """Return deterministic UTF-8 Agent-facing envelope bytes."""
    return canonical_json(machine_result(value))


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items() if key not in _FORBIDDEN_FIELDS}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
