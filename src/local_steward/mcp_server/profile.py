"""Strict wire decoding and immutable balanced-v1 profile resolution."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from uuid import UUID

from local_steward.agent_context import AgentContextPackRequest, MAX_AGENT_CONTEXT_INTENT_BYTES
from local_steward.llm_context import ContextBudget, UserIntentContext
from local_steward.observation_projection import (
    PairTrackingRequest,
    ProjectionBudget,
    ProjectionPolicy,
    SnapshotDiagnosticRequest,
)

from .protocol import PROFILE_NAME


class McpArgumentError(ValueError):
    """Private argument failure that never exposes raw input."""


BALANCED_V1_PROJECTION_POLICY = ProjectionPolicy(
    policy_schema_version=0,
    ordering_reference="raw-path",
    budget=ProjectionBudget(
        explicit_entry_total=12,
        hierarchy_node_total=12,
        tracking_item_total=12,
        relation_component_total=8,
        duplicate_alias_component_total=4,
        members_per_component=4,
        scope_minimum_guarantee=1,
        priority_quotas=(("TRACKING_FACT", 12),),
        serialized_bytes_soft=100_000,
    ),
    duplicate_overlay=False,
    relation_overlay=False,
)
BALANCED_V1_CONTEXT_BUDGET = ContextBudget(
    max_explicit_facts=12,
    max_hierarchy_items=12,
    max_overlays=8,
    max_expansion_descriptors=8,
)


def _utf8_length(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise McpArgumentError from error


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise McpArgumentError
    try:
        return str(UUID(value))
    except ValueError as error:
        raise McpArgumentError from error


def _relative_path(value: Any, *, optional: bool) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or _utf8_length(value) > 4096:
        raise McpArgumentError
    path = PurePosixPath(value)
    if path.is_absolute() or PureWindowsPath(value).is_absolute() or value.startswith("/"):
        raise McpArgumentError
    if value != "." and any(part in {"", ".", ".."} for part in value.split("/")):
        raise McpArgumentError
    return value


def build_pack_request(arguments: dict[str, Any]) -> AgentContextPackRequest:
    """Decode already schema-valid untouched JSON into one exact core request."""
    if arguments.get("profile") != PROFILE_NAME:
        raise McpArgumentError
    source = arguments.get("source")
    intent = arguments.get("user_intent")
    if not isinstance(source, dict) or not isinstance(intent, dict):
        raise McpArgumentError

    question = intent.get("question")
    scope_emphasis = intent.get("scope_emphasis")
    user_context = intent.get("user_provided_context")
    if (
        not isinstance(question, str)
        or not question
        or _utf8_length(question) > 8192
        or (scope_emphasis is not None and (
            not isinstance(scope_emphasis, str) or _utf8_length(scope_emphasis) > 2048
        ))
        or (user_context is not None and (
            not isinstance(user_context, str) or _utf8_length(user_context) > 4096
        ))
    ):
        raise McpArgumentError
    user_intent = UserIntentContext(question, scope_emphasis, user_context)
    canonical_intent = "\0".join(
        item for item in (question, scope_emphasis or "", user_context or "")
    ).encode("utf-8")
    if len(canonical_intent) > MAX_AGENT_CONTEXT_INTENT_BYTES:
        raise McpArgumentError

    scope_id = source.get("scope_id")
    if scope_id is not None and not isinstance(scope_id, str):
        raise McpArgumentError
    path_prefix = _relative_path(source.get("path_prefix"), optional=True)
    projection_request: SnapshotDiagnosticRequest | PairTrackingRequest
    if source.get("kind") == "SNAPSHOT_DIAGNOSTIC":
        projection_request = SnapshotDiagnosticRequest(
            primary_snapshot_id=_uuid(source.get("snapshot_id")),
            scope=scope_id,
            path_prefix=path_prefix,
        )
    elif source.get("kind") == "PAIR_TRACKING":
        base = _uuid(source.get("base_snapshot_id"))
        target = _uuid(source.get("target_snapshot_id"))
        if base == target:
            raise McpArgumentError
        projection_request = PairTrackingRequest(
            base_snapshot_id=base,
            target_snapshot_id=target,
            scope=scope_id,
            path_prefix=path_prefix,
        )
    else:
        raise McpArgumentError
    return AgentContextPackRequest(
        projection_request=projection_request,
        projection_policy=BALANCED_V1_PROJECTION_POLICY,
        user_intent=user_intent,
        context_budget=BALANCED_V1_CONTEXT_BUDGET,
    )


def decode_entry_reference(arguments: dict[str, Any]) -> tuple[str, str, str]:
    """Decode one explicit historical Entry reference."""
    scope_id = arguments.get("scope_id")
    if not isinstance(scope_id, str):
        raise McpArgumentError
    relative_path = _relative_path(arguments.get("relative_path"), optional=False)
    assert relative_path is not None
    return _uuid(arguments.get("snapshot_id")), scope_id, relative_path
