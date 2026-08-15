"""Bounded native MCP dispatcher over one unified session and Codex host policy."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
import threading
from typing import Any, Callable

import anyio
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp.types import CallToolResult, ImageContent, TextContent

from ..agent_authority import RiskClass
from ..agent_context import (
    ContextProjectionRequest,
    agent_context_pack_machine_object,
    build_context_projection,
    prepare_agent_context,
)
from ..agent_context.routing import AUTO_CONTEXT_PROFILE, select_context_profile
from ..code_execution import build_code_execution_packet
from ..agent_session import (
    ResolvedSnapshot,
    ScopeSelectionRequest,
    SelectionPolicy,
    SnapshotSelectionRequest,
    StewardSession,
    resolve_scope,
    resolve_scoped_path,
    resolve_snapshot,
    resolve_user_absolute_path,
    resolve_user_absolute_scope,
    safe_session_identity_payload,
)
from ..agent_session.errors import StewardSelectionNotFoundError
from ..errors import StewardError
from ..document_discovery import search_current_documents
from ..document_collection import (
    CurrentDocumentCandidate,
    CurrentDocumentPlan,
    SnapshotDocumentCandidate,
    SnapshotDocumentPlan,
    document_collection_machine_object,
    document_collection_request_digest,
    plan_current_documents,
    plan_snapshot_documents,
    revalidate_current_document,
    revalidate_snapshot_document,
)
from ..grounded_evidence import (
    build_document_evidence_packet,
    build_historical_evidence_packet,
)
from ..file_agent import SharedToolBudget, ToolBudgetLimits, ToolExecutionContext
from ..file_agent.runtime.structured_documents import MAX_DOCUMENT_OPERATION_ELAPSED_SECONDS
from ..file_agent.facade import steward_inspect_snapshot, steward_list_snapshots
from ..file_agent.serialization import machine_result
from ..mcp_server.adapter import canonical_json, model_safe_json
from ..mcp_server.profile import build_pack_request
from ..models import ScanBudget, StewardConfig
from ..runtime_capabilities import inspect_runtime_capabilities
from ..snapshot_refresh import (
    SnapshotChangeReviewRequest,
    review_snapshot_changes,
)
from ..storage import storage_status
from .protocol import (
    ADAPTER_SCHEMA_NAME,
    ADAPTER_SCHEMA_VERSION,
    CODE_TOOL,
    DOCUMENT_TOOL,
    EXACT_INTEGER_ENCODING_SCHEME,
    HISTORY_TOOL,
    INPUT_SCHEMAS,
    MAX_STRUCTURED_RESULT_BYTES,
    MAX_TEXT_BYTES,
    OPERATION_TIMEOUT_SECONDS,
    OUTPUT_SCHEMAS,
    RECOVERY_TOOL,
    TOOL_NAMES,
    UPDATE_TOOL,
)
from .host_policy import (
    CodexHostPolicy,
    create_codex_host_policy,
    host_authority_machine_object,
)
from .host_paths import (
    HOST_FILE_SELECTION_POLICY,
    admit_host_absolute_file,
)
from .product_bridge import HostApprovedProductBridge
from .thread_attribution import thread_attribution_machine_object


JsonObject = dict[str, Any]
OperationResult = tuple[JsonObject, list[JsonObject], tuple[ImageContent, ...]]


def _success_text(tool_name: str, result: JsonObject) -> str:
    """Give the model exact compact read facts before the full governed payload."""

    if tool_name != DOCUMENT_TOOL:
        return "OK: STEWARD returned a governed structured result with exact selection facts."
    document = result.get("document")
    packet = result.get("evidence_packet")
    if not isinstance(document, dict) or document.get("view") not in {"STRUCTURE", "LOCATE"}:
        return "OK: STEWARD returned a governed structured result with exact selection facts."
    summary: JsonObject = {
        "view": document.get("view"),
        "source_format": document.get("source_format"),
        "returned_count": document.get("returned_count"),
        "has_more": document.get("has_more"),
    }
    media = document.get("media")
    if isinstance(media, dict):
        summary["duration_ms"] = media.get("duration_ms")
        summary["decoded_frame_count"] = media.get("decoded_frame_count")
        summary["decoded_audio_bytes"] = media.get("decoded_audio_bytes")
    if isinstance(packet, dict):
        facts = packet.get("facts")
        if isinstance(facts, list):
            kinds: list[str] = []
            suffixes = {
                "video_video_stream": "video",
                "video_audio_stream": "audio",
                "video_subtitle_stream": "subtitle",
                "audio_stream": "audio",
            }
            for fact in facts:
                if isinstance(fact, dict):
                    raw_kind = fact.get("kind")
                    kind = suffixes.get(raw_kind) if isinstance(raw_kind, str) else None
                    if kind is not None and kind not in kinds:
                        kinds.append(kind)
            if kinds:
                summary["track_kinds"] = kinds
        verification = packet.get("verification")
        if isinstance(verification, dict):
            summary["verification"] = verification.get("status")
    encoded = canonical_json(summary).decode("utf-8")
    return f"OK exact read facts: {encoded}. Citations remain in structuredContent."


Operation = Callable[[], OperationResult]


def _safe_message(code: str) -> str:
    return {
        "STEWARD_NATIVE_TOOL_NOT_FOUND": "The requested STEWARD tool is unavailable.",
        "STEWARD_NATIVE_ARGUMENT_INVALID": "The STEWARD tool arguments are invalid.",
        "STEWARD_NATIVE_SELECTION_INVALID": "STEWARD could not resolve one safe exact selection.",
        "STEWARD_NATIVE_SOURCE_UNAVAILABLE": "The configured STEWARD source is unavailable.",
        "STEWARD_NATIVE_BUSY": "STEWARD is busy or changed during the operation.",
        "STEWARD_NATIVE_OPERATION_REJECTED": "STEWARD rejected the requested operation safely.",
        "STEWARD_NATIVE_TIMEOUT": "The STEWARD operation exceeded its bounded time.",
        "STEWARD_NATIVE_RESOURCE_LIMIT": "The STEWARD result exceeds its bounded size.",
        "STEWARD_NATIVE_UNAVAILABLE": "The STEWARD operation failed safely.",
    }.get(code, "The STEWARD operation failed safely.")


def _map_steward_error(error: StewardError) -> str:
    code = error.code
    if code.startswith("STEWARD_SELECTION_") or code in {
        "STEWARD_SCOPE_RESOLUTION_INVALID",
        "STEWARD_PATH_RESOLUTION_INVALID",
        "SNAPSHOT_NOT_FOUND",
        "SNAPSHOT_SCOPE_INVALID",
        "BASE_SELECTOR_REQUIRED",
    }:
        return "STEWARD_NATIVE_SELECTION_INVALID"
    if code in {
        "UNSUPPORTED_PROFILE",
        "CONTINUATION_MISMATCH",
        "CONTEXT_PROJECTION_INVALID",
        "CODE_EXECUTION_INVALID",
        "CODE_EXECUTION_BASELINE_INVALID",
    }:
        return "STEWARD_NATIVE_ARGUMENT_INVALID"
    if code == "CODE_EXECUTION_REPOSITORY_INVALID":
        return "STEWARD_NATIVE_SOURCE_UNAVAILABLE"
    if code == "CODE_EXECUTION_RESOURCE_LIMIT":
        return "STEWARD_NATIVE_RESOURCE_LIMIT"
    if code == "STORAGE_BUSY":
        return "STEWARD_NATIVE_BUSY"
    if code.startswith("CONFIG_") or code in {
        "STORAGE_NOT_INITIALIZED",
        "STORAGE_SCHEMA_INVALID",
        "STORAGE_SCHEMA_TOO_NEW",
        "STORAGE_MIGRATION_REQUIRED",
        "STEWARD_SESSION_UNAVAILABLE",
        "STEWARD_SESSION_CONFIGURATION_INVALID",
        "STEWARD_AUTHORITY_DOMAIN_MISMATCH",
    }:
        return "STEWARD_NATIVE_SOURCE_UNAVAILABLE"
    if "RESOURCE_LIMIT" in code or code.endswith("BUDGET_INVALID"):
        return "STEWARD_NATIVE_RESOURCE_LIMIT"
    return "STEWARD_NATIVE_OPERATION_REJECTED"


class NativeStewardDispatcher:
    """Own one coherent session behind an attested Codex tool-policy boundary."""

    def __init__(
        self,
        session: StewardSession,
        host_policy: CodexHostPolicy,
        *,
        max_result_bytes: int = MAX_STRUCTURED_RESULT_BYTES,
        timeout_seconds: float | None = None,
    ) -> None:
        self._session = session
        self._host_policy = host_policy
        if host_policy != create_codex_host_policy():
            raise ValueError("host policy and native tool surface differ")
        self._bridge = HostApprovedProductBridge(session)
        self._max_result_bytes = max_result_bytes
        self._timeout_override_seconds = timeout_seconds
        self._operation_lock = threading.Lock()

    async def dispatch(
        self,
        tool_name: str,
        arguments: object,
        *,
        request_meta: Mapping[str, Any] | None = None,
    ) -> CallToolResult:
        risk = self._tool_risk(tool_name)
        thread_attribution = thread_attribution_machine_object(request_meta)
        if tool_name not in TOOL_NAMES:
            return self._error(
                tool_name,
                risk,
                "STEWARD_NATIVE_TOOL_NOT_FOUND",
                thread_attribution=thread_attribution,
            )
        try:
            valid = isinstance(arguments, dict) and not any(
                Draft202012Validator(INPUT_SCHEMAS[tool_name]).iter_errors(arguments)
            )
        except Exception:
            valid = False
        if not valid:
            return self._error(
                tool_name,
                risk,
                "STEWARD_NATIVE_ARGUMENT_INVALID",
                thread_attribution=thread_attribution,
            )
        assert isinstance(arguments, dict)
        try:
            operation = self._operation(tool_name, arguments, thread_attribution)
            deadline_seconds = self._deadline_seconds(tool_name)
            with anyio.fail_after(deadline_seconds):
                result, selection, extra_content = await anyio.to_thread.run_sync(
                    self._locked(operation), abandon_on_cancel=True
                )
            return self._success(
                tool_name,
                risk,
                result,
                selection,
                extra_content=extra_content,
                thread_attribution=thread_attribution,
            )
        except TimeoutError:
            return self._error(
                tool_name,
                risk,
                "STEWARD_NATIVE_TIMEOUT",
                (
                    "DOCUMENT_OPERATION_DEADLINE_EXCEEDED"
                    if tool_name == DOCUMENT_TOOL
                    else "OPERATION_DEADLINE_EXCEEDED"
                ),
                thread_attribution=thread_attribution,
            )
        except StewardError as error:
            cause_code = getattr(error, "cause_code", None)
            return self._error(
                tool_name,
                risk,
                _map_steward_error(error),
                cause_code if isinstance(cause_code, str) else error.code,
                thread_attribution=thread_attribution,
            )
        except (KeyError, TypeError, ValueError):
            return self._error(
                tool_name,
                risk,
                "STEWARD_NATIVE_ARGUMENT_INVALID",
                thread_attribution=thread_attribution,
            )
        except Exception:
            return self._error(
                tool_name,
                risk,
                "STEWARD_NATIVE_UNAVAILABLE",
                thread_attribution=thread_attribution,
            )

    def _locked(self, operation: Operation) -> Callable[[], OperationResult]:
        def execute() -> OperationResult:
            with self._operation_lock:
                return operation()

        return execute

    @staticmethod
    def _tool_risk(tool_name: str) -> RiskClass:
        return {
            HISTORY_TOOL: RiskClass.HISTORICAL_READ,
            DOCUMENT_TOOL: RiskClass.CURRENT_CONTENT_READ,
            CODE_TOOL: RiskClass.CODE_WORKSPACE_READ,
            UPDATE_TOOL: RiskClass.DERIVED_STATE_APPEND,
            RECOVERY_TOOL: RiskClass.RECOVERY_OR_ADMIN,
        }.get(tool_name, RiskClass.HISTORICAL_READ)

    def _operation(
        self,
        tool_name: str,
        arguments: JsonObject,
        thread_attribution: JsonObject,
    ) -> Operation:
        if tool_name == HISTORY_TOOL:
            return self._plain_operation(lambda: self._history(arguments))
        if tool_name == DOCUMENT_TOOL:
            return lambda: self._document(arguments)
        if tool_name == CODE_TOOL:
            return self._plain_operation(
                lambda: self._code_execution(arguments, thread_attribution)
            )
        if tool_name == UPDATE_TOOL:
            return self._plain_operation(lambda: self._update(arguments))
        if tool_name == RECOVERY_TOOL:
            return self._plain_operation(lambda: self._recovery(arguments))
        raise ValueError

    @staticmethod
    def _plain_operation(
        operation: Callable[[], tuple[JsonObject, list[JsonObject]]],
    ) -> Operation:
        def execute() -> OperationResult:
            result, selection = operation()
            return result, selection, ()

        return execute

    def _selector(self, value: object, *, default_anchor: str | None = None) -> ResolvedSnapshot:
        if not isinstance(value, dict):
            raise ValueError
        policy = SelectionPolicy(value["policy"])
        anchor = value.get("anchor_snapshot_id", default_anchor)
        request = SnapshotSelectionRequest(
            policy,
            scope_id=value.get("scope_id"),
            exact_snapshot_id=value.get("snapshot_id"),
            anchor_snapshot_id=anchor,
        )
        return resolve_snapshot(self._session, request)

    @staticmethod
    def _selection(resolved: ResolvedSnapshot) -> JsonObject:
        return {
            "object_kind": "SNAPSHOT",
            "policy": resolved.policy.value,
            "snapshot_id": resolved.snapshot.snapshot_id,
            "run_id": resolved.snapshot.run_id,
            "verification_status": resolved.verification.status,
            "compatible_scope_id": resolved.compatible_scope_id,
        }

    def _history(self, arguments: JsonObject) -> tuple[JsonObject, list[JsonObject]]:
        action = arguments["action"]
        limit = arguments.get("limit", 100)
        offset = arguments.get("offset", 0)
        selections: list[JsonObject] = []
        if action != "ANALYZE_SNAPSHOT" and (
            "analysis_profile" in arguments or "continuation" in arguments
        ):
            raise ValueError
        if action in {"STATUS", "LIST_SNAPSHOTS"}:
            if action == "STATUS":
                return {"storage": machine_result(storage_status(self._session.config))}, selections
            value = steward_list_snapshots(self._facade_context(limit), limit=limit, offset=offset)
            return {"inventory": machine_result(value)}, selections
        target = self._selector(arguments.get("selector"))
        selections.append(self._selection(target))
        analysis_profile = arguments.get("analysis_profile")
        if analysis_profile is not None and action != "ANALYZE_SNAPSHOT":
            raise ValueError
        if action == "ANALYZE_SNAPSHOT" and analysis_profile is not None:
            question = arguments.get("question")
            if not isinstance(question, str):
                raise ValueError
            requested_profile = analysis_profile
            routing: JsonObject | None = None
            if analysis_profile == AUTO_CONTEXT_PROFILE:
                decision = select_context_profile(question)
                analysis_profile = decision.selected_profile
                routing = decision.payload()
            base_snapshot_id: str | None = None
            base_selector = arguments.get("base_selector")
            if analysis_profile == "CHANGE_TRIAGE":
                if base_selector is None and requested_profile == AUTO_CONTEXT_PROFILE:
                    base_selector = {
                        "policy": SelectionPolicy.PREVIOUS_VALID.value,
                        "anchor_snapshot_id": target.snapshot.snapshot_id,
                    }
                if base_selector is not None:
                    base = self._selector(base_selector)
                    selections.insert(0, self._selection(base))
                    base_snapshot_id = base.snapshot.snapshot_id
            continuation = arguments.get("continuation")
            if continuation is not None and not isinstance(continuation, dict):
                raise ValueError
            projection = build_context_projection(
                self._session.config,
                ContextProjectionRequest(
                    profile=analysis_profile,
                    snapshot_id=target.snapshot.snapshot_id,
                    scope_id=arguments.get("scope_id"),
                    path_prefix=arguments.get("path_prefix"),
                    limit=limit,
                    offset=offset,
                    question=question,
                    base_snapshot_id=base_snapshot_id,
                    continuation_digest=(
                        continuation.get("request_digest")
                        if isinstance(continuation, dict)
                        else None
                    ),
                    continuation_offset=(
                        continuation.get("offset") if isinstance(continuation, dict) else None
                    ),
                ),
            )
            result: JsonObject = {
                "context_projection": projection,
                "evidence_packet": build_historical_evidence_packet(
                    projection,
                    routing=routing,
                ),
            }
            if routing is not None:
                result["routing"] = routing
            return result, selections
        if action in {"REVIEW_CHANGES", "EXPLAIN_CHANGES"}:
            base_value = arguments.get("base_selector")
            if base_value is None:
                base_value = {
                    "policy": SelectionPolicy.PREVIOUS_VALID.value,
                    "scope_id": arguments.get("scope_id"),
                }
                base = self._selector(base_value, default_anchor=target.snapshot.snapshot_id)
            else:
                base = self._selector(base_value)
            selections.insert(0, self._selection(base))
            snapshot_ids: tuple[str, ...] = (
                base.snapshot.snapshot_id,
                target.snapshot.snapshot_id,
            )
        else:
            snapshot_ids = (target.snapshot.snapshot_id,)
        if action == "INSPECT_SNAPSHOT":
            value = steward_inspect_snapshot(
                self._facade_context(limit),
                target.snapshot.snapshot_id,
                scope_id=arguments.get("scope_id"),
                path_prefix=arguments.get("path_prefix"),
                limit=limit,
                offset=offset,
            )
            return {"inspection": machine_result(value)}, selections
        question = arguments.get("question")
        if action in {"ANALYZE_SNAPSHOT", "EXPLAIN_CHANGES"} and not isinstance(question, str):
            raise ValueError
        if action == "REVIEW_CHANGES":
            review = review_snapshot_changes(
                self._session.config,
                SnapshotChangeReviewRequest(snapshot_ids[0], snapshot_ids[1], limit, offset),
            )
            return {"review": machine_result(review)}, selections
        if action == "ANALYZE_SNAPSHOT":
            source: JsonObject = {
                "kind": "SNAPSHOT_DIAGNOSTIC",
                "snapshot_id": snapshot_ids[0],
                "scope_id": arguments.get("scope_id"),
                "path_prefix": arguments.get("path_prefix"),
            }
        elif action == "EXPLAIN_CHANGES":
            source = {
                "kind": "PAIR_TRACKING",
                "base_snapshot_id": snapshot_ids[0],
                "target_snapshot_id": snapshot_ids[1],
                "scope_id": arguments.get("scope_id"),
                "path_prefix": arguments.get("path_prefix"),
            }
        else:
            raise ValueError
        request = build_pack_request(
            {
                "profile": "balanced-v1",
                "source": source,
                "user_intent": {"question": question},
            }
        )
        bounded = replace(
            request,
            context_budget=replace(
                request.context_budget,
                max_explicit_facts=min(request.context_budget.max_explicit_facts, limit),
                max_hierarchy_items=min(request.context_budget.max_hierarchy_items, limit),
                max_overlays=min(request.context_budget.max_overlays, limit),
            ),
        )
        pack = prepare_agent_context(self._session.config, bounded)
        return {"context_pack": agent_context_pack_machine_object(pack)}, selections

    def _document(self, arguments: JsonObject) -> OperationResult:
        requested_action = arguments.get("action")
        action = requested_action or "READ"
        if action == "CAPABILITIES":
            if set(arguments) != {"action"}:
                raise ValueError
            return (
                {"runtime_capabilities": inspect_runtime_capabilities()},
                [{"object_kind": "RUNTIME_PROFILE", "policy": "OBSERVED_LOCAL_RUNTIME"}],
                (),
            )
        if action == "AUTO":
            action = "LOCATE" if arguments.get("content_query") is not None else "STRUCTURE"
        if action == "LOCATE" and arguments.get("content_query") is None:
            raise ValueError
        if action in {"EVIDENCE", "EVIDENCE_SET"} and arguments.get("content_query") is None:
            raise ValueError
        if action not in {"EVIDENCE", "EVIDENCE_SET"} and "diagnostic_detail" in arguments:
            raise ValueError
        evidence_fields = {
            "evidence_mode",
            "evidence_context_items",
            "evidence_max_characters",
            "evidence_page",
        }
        if action not in {"EVIDENCE", "EVIDENCE_SET"} and any(
            field in arguments for field in evidence_fields
        ):
            raise ValueError
        collection_fields = {
            "snapshot_selector",
            "max_documents",
            "batch_size",
            "per_document_timeout_seconds",
            "collection_continuation",
        }
        if action != "EVIDENCE_SET" and any(
            field in arguments for field in collection_fields
        ):
            raise ValueError
        if action == "EVIDENCE_SET":
            if any(
                field in arguments
                for field in {
                    "absolute_path",
                    "relative_path",
                    "limit",
                    "offset",
                    "expected_source_sha256",
                    "content_offset",
                    "evidence_page",
                    "page",
                    "node_id",
                    "visual_scale",
                }
            ):
                raise ValueError
            return self._document_evidence_set(arguments)
        has_query = "query" in arguments
        has_absolute_path = "absolute_path" in arguments
        has_relative_path = "relative_path" in arguments
        has_scope_id = "scope_id" in arguments
        if has_relative_path != (has_scope_id and not has_query):
            raise ValueError
        if sum((has_query, has_absolute_path, has_relative_path)) != 1:
            raise ValueError
        if action == "DISCOVER" and "query" not in arguments:
            raise ValueError
        visual_fields = {"page", "node_id", "visual_scale"}
        if action != "VIEW" and any(field in arguments for field in visual_fields):
            raise ValueError
        view = {
            "STRUCTURE": "STRUCTURE",
            "LOCATE": "STRUCTURE",
            "EVIDENCE": "READ",
            "READ": "READ",
            "EXTRACT_TABLE": "TABLES",
            "EXTRACT_FORMULA": "FORMULAS",
        }.get(action, "STRUCTURE")
        parser_profile = "AUTO" if requested_action else "FAST"
        if "query" in arguments:
            scope_id = arguments.get("scope_id")
            if scope_id is None:
                scope = resolve_scope(
                    self._session,
                    ScopeSelectionRequest(SelectionPolicy.ONLY_COMPATIBLE),
                )
            else:
                if not isinstance(scope_id, str):
                    raise ValueError
                scope = resolve_scope(
                    self._session,
                    ScopeSelectionRequest(SelectionPolicy.EXACT_ID, exact_scope_id=scope_id),
                )
            discovery = search_current_documents(
                self._session.config,
                scope.scope,
                query=arguments["query"],
                extensions=arguments.get("extensions"),
                limit=min(arguments.get("limit", 100), 50),
            )
            selection = {
                "object_kind": "DOCUMENT_SEARCH",
                "policy": scope.policy.value,
                "scope_id": scope.scope_id,
            }
            candidates = discovery["candidates"]
            if action == "DISCOVER" or len(candidates) != 1:
                if arguments.get("content_query") is not None:
                    discovery["content_search"] = {
                        "status": "NOT_RUN_AMBIGUOUS",
                        "reason_code": "DOCUMENT_SELECTION_NOT_UNIQUE",
                    }
                return {"document_search": discovery}, [selection], ()
            candidate = candidates[0]
            if requested_action == "AUTO" and candidate.get("source_format") in {
                "WAV",
                "FLAC",
                "MP3",
                "M4A",
                "AAC",
                "OGG",
                "OPUS",
                "MP4",
                "MOV",
                "MKV",
                "WEBM",
            }:
                action = "READ"
                view = "READ"
            resolved = resolve_scoped_path(
                self._session, candidate["scope_id"], candidate["relative_path"]
            )
            selection = {
                "object_kind": "CURRENT_DOCUMENT",
                "policy": "QUERY_UNIQUE",
                "input_kind": "DOCUMENT_QUERY",
                "scope_id": resolved.scope_id,
                "relative_path": resolved.relative_path,
            }
            search_result: JsonObject = {
                "query": discovery["query"],
                "matched_count": discovery["matched_count"],
                "selected": candidate,
            }
            return self._resolved_document_operation(
                resolved.scope_id,
                resolved.relative_path,
                action,
                arguments,
                parser_profile,
                view,
                selection,
                document_search=search_result,
            )
        operation_config = None
        operation_scoped = False
        if "absolute_path" in arguments:
            try:
                resolved = resolve_user_absolute_path(self._session, arguments["absolute_path"])
            except StewardSelectionNotFoundError:
                host_file = admit_host_absolute_file(self._session, arguments["absolute_path"])
                operation_config = host_file.config
                operation_scoped = True
                resolved_scope_id = host_file.scope_id
                resolved_relative_path = host_file.relative_path
        else:
            resolved = resolve_scoped_path(
                self._session, arguments["scope_id"], arguments["relative_path"]
            )
        if not operation_scoped:
            resolved_scope_id = resolved.scope_id
            resolved_relative_path = resolved.relative_path
        if requested_action == "AUTO" and Path(resolved_relative_path).suffix.casefold() in {
            ".wav",
            ".flac",
            ".mp3",
            ".m4a",
            ".aac",
            ".ogg",
            ".opus",
            ".mp4",
            ".m4v",
            ".mov",
            ".mkv",
            ".webm",
        }:
            action = "READ"
            view = "READ"
        selection = {
            "object_kind": "CURRENT_DOCUMENT",
            "policy": (
                HOST_FILE_SELECTION_POLICY if operation_scoped else resolved.policy.value
            ),
            "input_kind": (
                "USER_ABSOLUTE" if operation_scoped else resolved.input_kind.value
            ),
            "scope_id": resolved_scope_id,
            "relative_path": resolved_relative_path,
        }
        if operation_scoped:
            selection.update(
                {
                    "scope_lifetime": "OPERATION",
                    "persistence_effect": "NONE",
                    "authority_boundary": "CODEX_HOST_TOOL_POLICY",
                }
            )
        return self._resolved_document_operation(
            resolved_scope_id,
            resolved_relative_path,
            action,
            arguments,
            parser_profile,
            view,
            selection,
            operation_config=operation_config,
        )

    def _document_evidence_set(self, arguments: JsonObject) -> OperationResult:
        query = arguments.get("query")
        content_query = arguments.get("content_query")
        if not isinstance(query, str) or not isinstance(content_query, str):
            raise ValueError
        max_documents = arguments.get("max_documents", 4)
        batch_size = arguments.get("batch_size", 2)
        parser_timeout_seconds = arguments.get("per_document_timeout_seconds", 45)
        evidence_mode = arguments.get("evidence_mode", "AUTO")
        evidence_context_items = arguments.get("evidence_context_items", 2)
        evidence_max_characters = arguments.get("evidence_max_characters", 12_000)
        content_limit = arguments.get("content_limit", 20)
        diagnostic_detail = arguments.get("diagnostic_detail", "COMPACT")
        if (
            type(max_documents) is not int
            or not 1 <= max_documents <= 8
            or type(batch_size) is not int
            or not 1 <= batch_size <= 2
            or isinstance(parser_timeout_seconds, bool)
            or not isinstance(parser_timeout_seconds, (int, float))
            or not 5 <= parser_timeout_seconds <= 120
            or not isinstance(evidence_mode, str)
            or type(evidence_context_items) is not int
            or type(evidence_max_characters) is not int
            or type(content_limit) is not int
            or diagnostic_detail not in {"COMPACT", "FULL"}
        ):
            raise ValueError

        snapshot_selector = arguments.get("snapshot_selector")
        if snapshot_selector is not None and "scope_id" in arguments:
            raise ValueError
        selection: JsonObject
        plan: SnapshotDocumentPlan | CurrentDocumentPlan
        if snapshot_selector is not None:
            resolved_snapshot = self._selector(snapshot_selector)
            plan = plan_snapshot_documents(
                self._session,
                resolved_snapshot,
                query=query,
                extensions=arguments.get("extensions"),
                max_documents=max_documents,
            )
            selection = self._selection(resolved_snapshot)
            candidates: tuple[SnapshotDocumentCandidate | CurrentDocumentCandidate, ...] = (
                plan.candidates
            )
        else:
            scope_id = arguments.get("scope_id")
            if scope_id is None:
                scope = resolve_scope(
                    self._session,
                    ScopeSelectionRequest(SelectionPolicy.ONLY_COMPATIBLE),
                )
            elif isinstance(scope_id, str):
                scope = resolve_scope(
                    self._session,
                    ScopeSelectionRequest(SelectionPolicy.EXACT_ID, exact_scope_id=scope_id),
                )
            else:
                raise ValueError
            plan = plan_current_documents(
                self._session,
                scope,
                query=query,
                extensions=arguments.get("extensions"),
                max_documents=max_documents,
            )
            selection = {
                "object_kind": "SCOPE",
                "policy": scope.policy.value,
                "scope_id": scope.scope_id,
            }
            candidates = plan.candidates

        request_digest = document_collection_request_digest(
            source_kind=plan.source_kind,
            selection_policy=plan.selection_policy,
            query=query,
            content_query=content_query,
            extensions=plan.extensions,
            max_documents=max_documents,
            batch_size=batch_size,
            parser_timeout_seconds=float(parser_timeout_seconds),
            evidence_mode=evidence_mode,
            evidence_context_items=evidence_context_items,
            evidence_max_characters=evidence_max_characters,
            content_limit=content_limit,
            diagnostic_detail=diagnostic_detail,
        )
        continuation = arguments.get("collection_continuation")
        start = 0
        if continuation is not None:
            if not isinstance(continuation, dict):
                raise ValueError
            start = continuation.get("next_index")  # type: ignore[assignment]
            if (
                continuation.get("request_digest") != request_digest
                or continuation.get("candidate_set_digest") != plan.candidate_set_digest
                or type(start) is not int
                or not 0 <= start < len(candidates)
            ):
                raise ValueError

        stop = min(start + batch_size, len(candidates))
        allocated_characters = max(
            512,
            evidence_max_characters // max(1, stop - start),
        )
        items: list[JsonObject] = []
        document_selections: list[JsonObject] = [selection]
        for index, candidate in enumerate(candidates[start:stop], start=start):
            historical = (
                document_collection_machine_object(candidate.historical)
                if isinstance(candidate, SnapshotDocumentCandidate)
                else None
            )
            try:
                identity = (
                    revalidate_snapshot_document(self._session, candidate)
                    if isinstance(candidate, SnapshotDocumentCandidate)
                    else revalidate_current_document(self._session, candidate)
                )
                document_selections.append(
                    {
                        "object_kind": "CURRENT_DOCUMENT",
                        "policy": "COLLECTION_CURRENT_REVALIDATION",
                        "scope_id": identity.scope_id,
                        "relative_path": identity.relative_path,
                    }
                )
                page = self._bridge.inspect_document(
                    identity.scope_id,
                    identity.relative_path,
                    limit=1,
                    offset=0,
                    expected_source_sha256=None,
                    content_query=content_query,
                    content_limit=content_limit,
                    content_offset=0,
                    parser_profile="AUTO",
                    view="READ",
                    intent="EVIDENCE",
                    evidence_mode=evidence_mode,
                    evidence_context_items=evidence_context_items,
                    evidence_max_characters=allocated_characters,
                    parser_timeout_seconds=float(parser_timeout_seconds),
                )
                packet = build_document_evidence_packet(
                    page,
                    compact_execution=True,
                )
                payload_relation = (
                    "UNKNOWN"
                    if identity.historical_payload_sha256 is None or page.source_sha256 is None
                    else (
                        "PAYLOAD_MATCH"
                        if identity.historical_payload_sha256 == page.source_sha256
                        else "PAYLOAD_CHANGED"
                    )
                )
                item_status = (
                    "COMPLETE"
                    if page.status == "COMPLETE" and packet["packet_status"] == "READY"
                    else (
                        "NO_EVIDENCE"
                        if page.status == "COMPLETE"
                        else "FAILED"
                    )
                )
                item: JsonObject = {
                        "index": index,
                        "status": item_status,
                        "reason_code": None if item_status == "COMPLETE" else (
                            "CONTENT_QUERY_NOT_OBSERVED"
                            if item_status == "NO_EVIDENCE"
                            else page.status
                        ),
                        "candidate": document_collection_machine_object(candidate),
                        "historical": historical,
                        "current": {
                            **document_collection_machine_object(identity),
                            "source_sha256": page.source_sha256,
                            "historical_payload_relation": payload_relation,
                        },
                        "document_observation_digest": page.document_observation_digest,
                        "evidence_packet": packet,
                    }
                if diagnostic_detail == "FULL":
                    item["diagnostics"] = {
                        "detail": "FULL",
                        "execution": page.execution.payload()
                        if page.execution is not None
                        else None,
                        "resources": machine_result(page.resources),
                    }
                items.append(item)
            except StewardError as error:
                items.append(
                    {
                        "index": index,
                        "status": "FAILED",
                        "reason_code": error.code,
                        "candidate": document_collection_machine_object(candidate),
                        "historical": historical,
                        "current": None,
                        "document_observation_digest": None,
                        "evidence_packet": None,
                    }
                )
            except (OSError, ValueError):
                items.append(
                    {
                        "index": index,
                        "status": "FAILED",
                        "reason_code": "CURRENT_DOCUMENT_ADMISSION_FAILED",
                        "candidate": document_collection_machine_object(candidate),
                        "historical": historical,
                        "current": None,
                        "document_observation_digest": None,
                        "evidence_packet": None,
                    }
                )
            except Exception:
                items.append(
                    {
                        "index": index,
                        "status": "FAILED",
                        "reason_code": "DOCUMENT_COLLECTION_ITEM_FAILED",
                        "candidate": document_collection_machine_object(candidate),
                        "historical": historical,
                        "current": None,
                        "document_observation_digest": None,
                        "evidence_packet": None,
                    }
                )

        next_continuation: JsonObject | None = None
        if stop < len(candidates):
            next_continuation = {
                "request_digest": request_digest,
                "candidate_set_digest": plan.candidate_set_digest,
                "next_index": stop,
            }
        counts = {
            "planned": len(candidates),
            "processed": len(items),
            "complete": sum(item["status"] == "COMPLETE" for item in items),
            "no_evidence": sum(item["status"] == "NO_EVIDENCE" for item in items),
            "failed": sum(item["status"] == "FAILED" for item in items),
        }
        return (
            {
                "document_evidence_collection": {
                    "schema_name": "local_steward.document_evidence_collection",
                    "schema_version": 2,
                    "request_digest": request_digest,
                    "plan": document_collection_machine_object(plan),
                    "batch": {
                        "start_index": start,
                        "next_index": stop,
                        "batch_size": batch_size,
                        "per_document_timeout_seconds": parser_timeout_seconds,
                        "allocated_evidence_characters_per_document": allocated_characters,
                    },
                    "items": items,
                    "counts": counts,
                    "continuation": next_continuation,
                    "diagnostic_detail": diagnostic_detail,
                }
            },
            document_selections,
            (),
        )

    def _resolved_document_operation(
        self,
        scope_id: str,
        relative_path: str,
        action: object,
        arguments: JsonObject,
        parser_profile: str,
        view: str,
        selection: JsonObject,
        *,
        document_search: JsonObject | None = None,
        operation_config: StewardConfig | None = None,
    ) -> OperationResult:
        if action == "VIEW":
            visual = self._bridge.inspect_document_visual(
                scope_id,
                relative_path,
                page=arguments.get("page"),
                node_id=arguments.get("node_id"),
                content_query=arguments.get("content_query"),
                expected_source_sha256=arguments.get("expected_source_sha256"),
                scale=arguments.get("visual_scale", 2.0),
                video_timestamp_ms=arguments.get("video_timestamp_ms"),
                operation_config=operation_config,
            )
            result: JsonObject = {"visual": visual.payload()}
            if document_search is not None:
                result["document_search"] = document_search
            media: tuple[ImageContent, ...] = ()
            if visual.status == "COMPLETE" and visual.mime_type is not None:
                media = (
                    ImageContent(
                        type="image",
                        data=b64encode(visual.image_data).decode("ascii"),
                        mimeType=visual.mime_type,
                    ),
                )
            return result, [selection], media
        page = self._bridge.inspect_document(
            scope_id,
            relative_path,
            limit=arguments.get("limit", 100),
            offset=arguments.get("offset", 0),
            expected_source_sha256=arguments.get("expected_source_sha256"),
            content_query=arguments.get("content_query"),
            content_limit=arguments.get("content_limit", 20),
            content_offset=arguments.get("content_offset", 0),
            parser_profile=parser_profile,
            view=view,
            intent={
                "EXTRACT_TABLE": "TABLES",
                "EXTRACT_FORMULA": "FORMULAS",
            }.get(str(action), str(action)),
            evidence_mode=arguments.get("evidence_mode", "AUTO"),
            evidence_context_items=arguments.get("evidence_context_items", 2),
            evidence_max_characters=arguments.get("evidence_max_characters", 12_000),
            evidence_page=arguments.get("evidence_page"),
            audio_analysis=arguments.get("audio_analysis", "TRANSCRIPT"),
            audio_language=arguments.get("audio_language"),
            audio_continuation=arguments.get("audio_continuation"),
            video_analysis=arguments.get("video_analysis", "MULTIMODAL"),
            video_continuation=arguments.get("video_continuation"),
            operation_config=operation_config,
        )
        if action == "EVIDENCE":
            diagnostic_detail = arguments.get("diagnostic_detail", "COMPACT")
            evidence_packet = build_document_evidence_packet(
                page,
                compact_execution=True,
                execution_projection="EVIDENCE_SUMMARY",
            )
            result = {
                "document": {
                    "protocol_version": page.protocol_version,
                    "status": page.status,
                    "source_kind": page.source_kind,
                    "scope_id": page.scope_id,
                    "relative_path": page.relative_path,
                    "source_format": page.source_format,
                    "source_sha256": page.source_sha256,
                    "document_observation_digest": page.document_observation_digest,
                    "projection": "EVIDENCE_PACKET_ONLY",
                    "diagnostic_detail": diagnostic_detail,
                },
                "evidence_packet": evidence_packet,
            }
            if diagnostic_detail == "FULL":
                result["diagnostics"] = {
                    "execution": page.execution.payload()
                    if page.execution is not None
                    else None,
                    "resources": machine_result(page.resources),
                    "parser_failure": (
                        {
                            "reason_code": page.failure_reason_code,
                            "exception_type": page.failure_exception_type,
                        }
                        if page.failure_reason_code is not None
                        else None
                    ),
                }
        elif action in {"STRUCTURE", "LOCATE"}:
            result = {
                "document": {
                    "protocol_version": page.protocol_version,
                    "status": page.status,
                    "source_kind": page.source_kind,
                    "scope_id": page.scope_id,
                    "relative_path": page.relative_path,
                    "source_format": page.source_format,
                    "source_sha256": page.source_sha256,
                    "document_observation_digest": page.document_observation_digest,
                    "backend_name": page.backend_name,
                    "backend_version": page.backend_version,
                    "view": page.view,
                    "full_item_count": page.full_item_count,
                    "returned_count": page.returned_count,
                    "has_more": page.has_more,
                    "next_offset": page.next_offset,
                    "continuation": page.continuation,
                    "warnings": list(page.warnings),
                    "failure_reason_code": page.failure_reason_code,
                    "failure_exception_type": page.failure_exception_type,
                    "media": machine_result(page.resources.media),
                    "projection": "GROUNDED_EVIDENCE_ONLY",
                },
                "evidence_packet": build_document_evidence_packet(page),
            }
        else:
            result = {
                "document": machine_result(page),
                "evidence_packet": build_document_evidence_packet(page),
            }
        if document_search is not None:
            result["document_search"] = document_search
        return result, [selection], ()

    def _code_execution(
        self,
        arguments: JsonObject,
        thread_attribution: JsonObject,
    ) -> tuple[JsonObject, list[JsonObject]]:
        packet = build_code_execution_packet(
            self._session.config,
            phase=arguments["phase"],
            target_paths=arguments.get("target_paths"),
            baseline=arguments.get("baseline"),
            validation_claims=arguments.get("validation_claims"),
            thread_attribution=thread_attribution,
        )
        return (
            {"code_execution_packet": packet},
            [
                {
                    "object_kind": "CODE_WORKSPACE",
                    "policy": "CONFIGURED_PROJECT_ROOT",
                    "root_id": "PROJECT_ROOT",
                }
            ],
        )

    def _resolve_update_scope(self, value: object):  # type: ignore[no-untyped-def]
        if not isinstance(value, dict):
            raise ValueError
        if "absolute_path" in value:
            return resolve_user_absolute_scope(self._session, value["absolute_path"])
        policy = SelectionPolicy(value["policy"])
        return resolve_scope(
            self._session,
            ScopeSelectionRequest(policy, exact_scope_id=value.get("scope_id")),
        )

    def _update(self, arguments: JsonObject) -> tuple[JsonObject, list[JsonObject]]:
        action = arguments["action"]
        scope = self._resolve_update_scope(arguments["scope"])
        budget = ScanBudget(
            max_entries=arguments.get("max_entries", 100_000),
            max_duration_seconds=arguments.get("max_duration_seconds", 60.0),
        )
        selections: list[JsonObject] = [
            {"object_kind": "SCOPE", "policy": scope.policy.value, "scope_id": scope.scope_id}
        ]
        if action == "ACQUIRE":
            acquisition = self._bridge.acquire(scope.scope_id, budget)
            result: JsonObject = {"acquisition": machine_result(acquisition)}
            run_id = acquisition.run_id
            snapshot_id = acquisition.snapshot_id
        elif action == "REFRESH":
            base = self._selector(arguments.get("base_selector"))
            selections.append(self._selection(base))
            refresh = self._bridge.refresh(
                scope.scope_id,
                base.snapshot.snapshot_id,
                budget,
                change_limit=arguments.get("change_limit", 100),
                change_offset=arguments.get("change_offset", 0),
            )
            result = {"refresh": machine_result(refresh)}
            run_id = refresh.acquisition.run_id
            snapshot_id = refresh.acquisition.snapshot_id
        else:
            raise ValueError
        result["created_identity"] = {"run_id": run_id, "snapshot_id": snapshot_id}
        return result, selections

    def _recovery(self, arguments: JsonObject) -> tuple[JsonObject, list[JsonObject]]:
        run_id = arguments["run_id"]
        report = self._bridge.recover(run_id)
        return (
            {"recovery": machine_result(report)},
            [{"object_kind": "RUN", "policy": SelectionPolicy.EXACT_ID.value, "run_id": run_id}],
        )

    def _facade_context(self, max_items: int) -> ToolExecutionContext:
        timeout_seconds = self._timeout_override_seconds or OPERATION_TIMEOUT_SECONDS
        limits = ToolBudgetLimits(
            max_steward_calls_per_turn=1,
            max_items_per_call=max_items,
            max_items_per_turn=max_items,
            max_serialized_bytes_per_call=self._max_result_bytes,
            max_serialized_bytes_per_turn=self._max_result_bytes,
            max_elapsed_ms_per_call=int(timeout_seconds * 1000),
            max_elapsed_ms_per_turn=int(timeout_seconds * 1000),
        )
        return ToolExecutionContext(self._session.config, SharedToolBudget(limits))

    def _deadline_seconds(self, tool_name: str) -> float:
        if self._timeout_override_seconds is not None:
            return self._timeout_override_seconds
        if tool_name == DOCUMENT_TOOL:
            return MAX_DOCUMENT_OPERATION_ELAPSED_SECONDS
        return OPERATION_TIMEOUT_SECONDS

    def _success(
        self,
        tool_name: str,
        risk: RiskClass,
        result: JsonObject,
        selection: list[JsonObject],
        *,
        extra_content: tuple[ImageContent, ...] = (),
        thread_attribution: JsonObject,
    ) -> CallToolResult:
        safe, paths = model_safe_json(result)
        if not isinstance(safe, dict):
            return self._error(
                tool_name,
                risk,
                "STEWARD_NATIVE_UNAVAILABLE",
                thread_attribution=thread_attribution,
            )
        envelope: JsonObject = self._base(
            tool_name,
            risk,
            selection,
            thread_attribution=thread_attribution,
        )
        envelope.update(
            {
                "status": "OK",
                "exact_integer_encoding": {
                    "scheme": EXACT_INTEGER_ENCODING_SCHEME,
                    "decimal_string_paths": list(paths),
                },
                "result": safe,
                "error": None,
            }
        )
        if len(canonical_json(envelope)) > self._max_result_bytes:
            return self._error(
                tool_name,
                risk,
                "STEWARD_NATIVE_RESOURCE_LIMIT",
                thread_attribution=thread_attribution,
            )
        if any(Draft202012Validator(OUTPUT_SCHEMAS[tool_name]).iter_errors(envelope)):
            return self._error(
                tool_name,
                risk,
                "STEWARD_NATIVE_UNAVAILABLE",
                thread_attribution=thread_attribution,
            )
        text = _success_text(tool_name, safe)
        return CallToolResult(
            content=[TextContent(type="text", text=text), *extra_content],
            structuredContent=envelope,
            isError=False,
        )

    def _base(
        self,
        tool_name: str,
        risk: RiskClass,
        selection: list[JsonObject],
        *,
        thread_attribution: JsonObject,
    ) -> JsonObject:
        return {
            "schema_name": ADAPTER_SCHEMA_NAME,
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "tool_name": tool_name,
            "risk_class": risk.value,
            "session": safe_session_identity_payload(self._session),
            "authority": host_authority_machine_object(self._host_policy, tool_name),
            "thread_attribution": thread_attribution,
            "selection": selection,
        }

    def _error(
        self,
        tool_name: str,
        risk: RiskClass,
        code: str,
        cause_code: str | None = None,
        *,
        thread_attribution: JsonObject | None = None,
    ) -> CallToolResult:
        envelope = self._base(
            tool_name,
            risk,
            [],
            thread_attribution=(
                thread_attribution_machine_object(None)
                if thread_attribution is None
                else thread_attribution
            ),
        )
        envelope.update(
            {
                "status": "ERROR",
                "exact_integer_encoding": {
                    "scheme": EXACT_INTEGER_ENCODING_SCHEME,
                    "decimal_string_paths": [],
                },
                "result": None,
                "error": {
                    "code": code,
                    "cause_code": cause_code,
                    "message": _safe_message(code),
                },
            }
        )
        text = f"ERROR [{code}]: {envelope['error']['message']}"
        assert len(text.encode("utf-8")) <= MAX_TEXT_BYTES
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=envelope,
            isError=True,
        )
