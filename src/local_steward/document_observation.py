"""Public provider-free inspection of one configured current document."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import stat

from .errors import (
    DocumentInspectionConfirmationError,
    DocumentInspectionInputError,
    DocumentInspectionScopeError,
    DocumentInspectionSourceChangedError,
    DocumentInspectionUnavailableError,
)
from .evidence import canonical_json
from .document_evidence import (
    DEFAULT_DOCUMENT_EVIDENCE_CONTEXT_ITEMS,
    DEFAULT_DOCUMENT_EVIDENCE_MAX_CHARS,
    EVIDENCE_SELECTION_MODES,
    MAX_DOCUMENT_EVIDENCE_CONTEXT_ITEMS,
    MAX_DOCUMENT_EVIDENCE_MAX_CHARS,
    DocumentEvidenceSelection,
    build_document_evidence_selection,
)
from .document_execution import BoundedDocumentParseCache, DocumentExecutionTrace
from .document_query import (
    EXACT_MATCH_MODE,
    document_match_mode,
    match_document_text,
)
from .file_agent.runtime.failures import RuntimeFailure
from .file_agent.runtime.audio_documents import (
    AUDIO_CONTINUATION_SCHEMA_NAME,
    AUDIO_RESULT_PAGE_CONTINUATION_SCHEMA_VERSION,
    AUDIO_SOURCE_FORMATS,
    AUDIO_TIME_CONTINUATION_SCHEMA_VERSION,
)
from .file_agent.runtime.video_documents import (
    VIDEO_CONTINUATION_SCHEMA_NAME,
    VIDEO_RESULT_MODALITY_QUOTAS,
    VIDEO_RESULT_PAGE_CONTINUATION_SCHEMA_VERSION,
    VIDEO_SOURCE_FORMATS,
    VIDEO_TIME_CONTINUATION_SCHEMA_VERSION,
)
from .file_agent.runtime.scope_binding import ScopeBinding, ScopeBindings
from .file_agent.runtime.structured_documents import (
    DocumentResourceUsage,
    NormalizedDocumentItem,
    NormalizedDocumentObservation,
    ProjectOwnedBoundedDocumentIngress,
    StructuredDocumentParserAdapter,
    _WorkerExecution,
)
from .models import ScopeConfig, ScopeRole, StewardConfig
from .paths import contains


DOCUMENT_INSPECTION_PROTOCOL_VERSION = 4
MAX_DOCUMENT_INSPECTION_PAGE_ITEMS = 1_000
MAX_DOCUMENT_CONTENT_QUERY_BYTES = 512
MAX_DOCUMENT_CONTENT_MATCHES = 50
MAX_DOCUMENT_CONTENT_EXCERPT_CHARS = 512
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ACTIONABLE_ROLES = frozenset({ScopeRole.MANAGED_ROOT, ScopeRole.REFERENCE_ROOT})
_MEDIA_SUFFIXES = frozenset(
    {
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
    }
)


@dataclass(frozen=True, slots=True)
class DocumentInspectionRequest:
    """One explicitly confirmed bounded current-document read."""

    scope_id: str
    relative_path: str
    confirmed: bool
    limit: int = 100
    offset: int = 0
    expected_source_sha256: str | None = None
    content_query: str | None = None
    content_limit: int = 20
    content_offset: int = 0
    parser_profile: str = "FAST"
    view: str = "READ"
    intent: str = "READ"
    evidence_mode: str = "AUTO"
    evidence_context_items: int = DEFAULT_DOCUMENT_EVIDENCE_CONTEXT_ITEMS
    evidence_max_characters: int = DEFAULT_DOCUMENT_EVIDENCE_MAX_CHARS
    evidence_page: int | None = None
    parser_timeout_seconds: float | None = None
    audio_analysis: str = "TRANSCRIPT"
    audio_language: str | None = None
    audio_continuation: dict[str, object] | None = None
    video_analysis: str = "MULTIMODAL"
    video_continuation: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class DocumentContentMatch:
    """One bounded logical item containing a deterministic text match."""

    item_index: int
    kind: str
    location: dict[str, int | str]
    parent: str | None
    excerpt: str
    excerpt_truncated: bool
    match_count: int
    match_mode: str
    node_id: str | None = None
    role: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentContentSearch:
    """A bounded, case-folded substring search over normalized document items."""

    query: str
    match_mode: str
    status: str
    matched_item_count: int
    matched_occurrence_count: int
    returned_count: int
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None
    matches: tuple[DocumentContentMatch, ...]


@dataclass(frozen=True, slots=True)
class DocumentInspectionPage:
    """One bounded page projected from a complete normalized observation."""

    protocol_version: int
    status: str
    source_format: str | None
    backend_name: str | None
    backend_version: str | None
    source_kind: str
    scope_id: str
    relative_path: str
    source_sha256: str | None
    identification_reason: str | None
    warnings: tuple[str, ...]
    resources: DocumentResourceUsage
    items: tuple[NormalizedDocumentItem, ...]
    full_item_count: int
    returned_count: int
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None
    document_observation_digest: str | None
    execution: DocumentExecutionTrace | None
    content_search: DocumentContentSearch | None = None
    view: str = "READ"
    evidence_selection: DocumentEvidenceSelection | None = None
    continuation: dict[str, object] | None = None
    failure_reason_code: str | None = None
    failure_exception_type: str | None = None


def _valid_audio_time_continuation(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        set(value)
        == {
            "schema_name",
            "schema_version",
            "request_digest",
            "source_sha256",
            "next_start_ms",
        }
        and value.get("schema_name") == AUDIO_CONTINUATION_SCHEMA_NAME
        and value.get("schema_version") == AUDIO_TIME_CONTINUATION_SCHEMA_VERSION
        and isinstance(value.get("request_digest"), str)
        and _SHA256.fullmatch(value["request_digest"]) is not None
        and isinstance(value.get("source_sha256"), str)
        and _SHA256.fullmatch(value["source_sha256"]) is not None
        and type(value.get("next_start_ms")) is int
        and value["next_start_ms"] >= 0
    )


def _valid_audio_result_page_continuation(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    next_window = value.get("next_window")
    return (
        set(value)
        == {
            "schema_name",
            "schema_version",
            "kind",
            "request_digest",
            "source_sha256",
            "window_start_ms",
            "next_offset",
            "limit",
            "next_window",
        }
        and value.get("schema_name") == AUDIO_CONTINUATION_SCHEMA_NAME
        and value.get("schema_version") == AUDIO_RESULT_PAGE_CONTINUATION_SCHEMA_VERSION
        and value.get("kind") == "RESULT_PAGE"
        and isinstance(value.get("request_digest"), str)
        and _SHA256.fullmatch(value["request_digest"]) is not None
        and isinstance(value.get("source_sha256"), str)
        and _SHA256.fullmatch(value["source_sha256"]) is not None
        and type(value.get("window_start_ms")) is int
        and value["window_start_ms"] >= 0
        and type(value.get("next_offset")) is int
        and value["next_offset"] > 0
        and type(value.get("limit")) is int
        and 1 <= value["limit"] <= MAX_DOCUMENT_INSPECTION_PAGE_ITEMS
        and (next_window is None or _valid_audio_time_continuation(next_window))
    )


def _valid_video_time_continuation(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        set(value)
        == {
            "schema_name",
            "schema_version",
            "request_digest",
            "source_sha256",
            "next_start_ms",
        }
        and value.get("schema_name") == VIDEO_CONTINUATION_SCHEMA_NAME
        and value.get("schema_version") == VIDEO_TIME_CONTINUATION_SCHEMA_VERSION
        and isinstance(value.get("request_digest"), str)
        and _SHA256.fullmatch(value["request_digest"]) is not None
        and isinstance(value.get("source_sha256"), str)
        and _SHA256.fullmatch(value["source_sha256"]) is not None
        and type(value.get("next_start_ms")) is int
        and value["next_start_ms"] >= 0
    )


def _valid_video_result_page_continuation(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    next_window = value.get("next_window")
    return (
        set(value)
        == {
            "schema_name",
            "schema_version",
            "kind",
            "request_digest",
            "source_sha256",
            "window_start_ms",
            "next_offset",
            "limit",
            "next_window",
        }
        and value.get("schema_name") == VIDEO_CONTINUATION_SCHEMA_NAME
        and value.get("schema_version") == VIDEO_RESULT_PAGE_CONTINUATION_SCHEMA_VERSION
        and value.get("kind") == "RESULT_PAGE"
        and isinstance(value.get("request_digest"), str)
        and _SHA256.fullmatch(value["request_digest"]) is not None
        and isinstance(value.get("source_sha256"), str)
        and _SHA256.fullmatch(value["source_sha256"]) is not None
        and type(value.get("window_start_ms")) is int
        and value["window_start_ms"] >= 0
        and type(value.get("next_offset")) is int
        and value["next_offset"] > 0
        and type(value.get("limit")) is int
        and 1 <= value["limit"] <= MAX_DOCUMENT_INSPECTION_PAGE_ITEMS
        and (next_window is None or _valid_video_time_continuation(next_window))
    )


def _validate_request(request: DocumentInspectionRequest) -> None:
    if request.confirmed is not True:
        raise DocumentInspectionConfirmationError(
            "document content inspection requires explicit confirmation"
        )
    if not isinstance(request.scope_id, str) or not request.scope_id:
        raise DocumentInspectionScopeError("scope ID must be a non-empty string")
    if not isinstance(request.relative_path, str) or not request.relative_path:
        raise DocumentInspectionInputError("relative path must be a non-empty string")
    if (
        isinstance(request.limit, bool)
        or not isinstance(request.limit, int)
        or not 1 <= request.limit <= MAX_DOCUMENT_INSPECTION_PAGE_ITEMS
    ):
        raise DocumentInspectionInputError("limit must be an integer from 1 through 1000")
    if (
        isinstance(request.offset, bool)
        or not isinstance(request.offset, int)
        or request.offset < 0
    ):
        raise DocumentInspectionInputError("offset must be a nonnegative integer")
    expected = request.expected_source_sha256
    if expected is not None and (not isinstance(expected, str) or not _SHA256.fullmatch(expected)):
        raise DocumentInspectionInputError(
            "expected source SHA-256 must be 64 lowercase hexadecimal characters"
        )
    if request.offset > 0 and expected is None:
        raise DocumentInspectionInputError("a later page requires the first page source SHA-256")
    query = request.content_query
    if query is not None:
        if not isinstance(query, str) or not query or not query.strip():
            raise DocumentInspectionInputError("content query must be a non-empty string")
        if len(query.encode("utf-8")) > MAX_DOCUMENT_CONTENT_QUERY_BYTES:
            raise DocumentInspectionInputError("content query exceeds the 512-byte limit")
        if "\x00" in query or any(ord(char) < 32 and char not in "\n\r\t" for char in query):
            raise DocumentInspectionInputError("content query contains an unsupported control")
    elif request.content_offset != 0:
        raise DocumentInspectionInputError("content offset requires a content query")
    if (
        isinstance(request.content_limit, bool)
        or not isinstance(request.content_limit, int)
        or not 1 <= request.content_limit <= MAX_DOCUMENT_CONTENT_MATCHES
    ):
        raise DocumentInspectionInputError("content limit must be an integer from 1 through 50")
    if (
        isinstance(request.content_offset, bool)
        or not isinstance(request.content_offset, int)
        or request.content_offset < 0
    ):
        raise DocumentInspectionInputError("content offset must be a nonnegative integer")
    if request.content_offset > 0 and expected is None:
        raise DocumentInspectionInputError(
            "a later content page requires the first page source SHA-256"
        )
    if request.parser_profile not in {"FAST", "AUTO", "DEEP", "ENRICHED"}:
        raise DocumentInspectionInputError("parser profile must be FAST, AUTO, DEEP, or ENRICHED")
    if request.view not in {"READ", "STRUCTURE", "TABLES", "FORMULAS"}:
        raise DocumentInspectionInputError(
            "document view must be READ, STRUCTURE, TABLES, or FORMULAS"
        )
    if request.intent not in {
        "READ",
        "STRUCTURE",
        "LOCATE",
        "EVIDENCE",
        "TABLES",
        "FORMULAS",
    }:
        raise DocumentInspectionInputError("document execution intent is invalid")
    if request.intent == "EVIDENCE" and request.content_query is None:
        raise DocumentInspectionInputError("document evidence extraction requires a content query")
    if request.evidence_mode not in EVIDENCE_SELECTION_MODES:
        raise DocumentInspectionInputError(
            "document evidence mode must be AUTO, MATCH, WINDOW, or SECTION"
        )
    if (
        isinstance(request.evidence_context_items, bool)
        or not isinstance(request.evidence_context_items, int)
        or not 0 <= request.evidence_context_items <= MAX_DOCUMENT_EVIDENCE_CONTEXT_ITEMS
    ):
        raise DocumentInspectionInputError("evidence context items must be an integer from 0 to 8")
    if (
        isinstance(request.evidence_max_characters, bool)
        or not isinstance(request.evidence_max_characters, int)
        or not 512 <= request.evidence_max_characters <= MAX_DOCUMENT_EVIDENCE_MAX_CHARS
    ):
        raise DocumentInspectionInputError(
            "evidence character budget must be an integer from 512 through 32768"
        )
    if request.evidence_page is not None and (
        isinstance(request.evidence_page, bool)
        or not isinstance(request.evidence_page, int)
        or not 1 <= request.evidence_page <= 10_000
    ):
        raise DocumentInspectionInputError("evidence page must be an integer from 1 through 10000")
    if request.parser_timeout_seconds is not None and (
        isinstance(request.parser_timeout_seconds, bool)
        or not isinstance(request.parser_timeout_seconds, (int, float))
        or not 1 <= request.parser_timeout_seconds <= 600
    ):
        raise DocumentInspectionInputError(
            "parser timeout must be a number from 1 through 600 seconds"
        )
    if request.audio_language is not None and (
        not isinstance(request.audio_language, str)
        or not request.audio_language
        or len(request.audio_language) > 16
        or not request.audio_language.replace("-", "").isalpha()
    ):
        raise DocumentInspectionInputError("audio language hint is invalid")
    if request.audio_analysis not in {
        "TRANSCRIPT",
        "ALIGNED_WORDS",
        "SPEAKER_TURNS",
        "ALIGNED_WORDS_AND_SPEAKERS",
    }:
        raise DocumentInspectionInputError("audio analysis mode is invalid")
    if request.audio_continuation is not None:
        continuation = request.audio_continuation
        if not (
            _valid_audio_time_continuation(continuation)
            or _valid_audio_result_page_continuation(continuation)
        ):
            raise DocumentInspectionInputError("audio continuation is invalid")
    if request.video_analysis not in {
        "SCENES",
        "SCENES_AND_OCR",
        "MULTIMODAL",
        "MULTIMODAL_AND_OCR",
    }:
        raise DocumentInspectionInputError("video analysis mode is invalid")
    if request.video_continuation is not None:
        continuation = request.video_continuation
        if not (
            _valid_video_time_continuation(continuation)
            or _valid_video_result_page_continuation(continuation)
        ):
            raise DocumentInspectionInputError("video continuation is invalid")


def _content_match(value: str, query: str) -> tuple[int, str, str] | None:
    match = match_document_text(value, query)
    if match is None:
        return None
    excerpt_start = max(0, match.source_start - 160)
    excerpt_end = min(len(value), max(match.source_end, match.source_start) + 160)
    excerpt = value[excerpt_start:excerpt_end]
    if len(excerpt) > MAX_DOCUMENT_CONTENT_EXCERPT_CHARS:
        excerpt = excerpt[:MAX_DOCUMENT_CONTENT_EXCERPT_CHARS]
    return match.count, excerpt, match.mode


def _content_search(
    observation: NormalizedDocumentObservation,
    request: DocumentInspectionRequest,
) -> DocumentContentSearch | None:
    if request.content_query is None:
        return None
    if observation.status != "COMPLETE":
        return DocumentContentSearch(
            request.content_query,
            EXACT_MATCH_MODE,
            "NOT_SEARCHABLE",
            0,
            0,
            0,
            request.content_limit,
            request.content_offset,
            False,
            None,
            (),
        )
    matched: list[DocumentContentMatch] = []
    occurrence_count = 0
    match_modes: set[str] = set()
    for item_index, item in enumerate(observation.items):
        if item.text_or_value is None:
            continue
        result = _content_match(item.text_or_value, request.content_query)
        if result is None:
            continue
        match_count, excerpt, match_mode = result
        occurrence_count += match_count
        match_modes.add(match_mode)
        matched.append(
            DocumentContentMatch(
                item_index,
                item.kind,
                item.location,
                item.parent,
                excerpt,
                len(excerpt) < len(item.text_or_value),
                match_count,
                match_mode,
                item.node_id,
                item.role,
            )
        )
    page = matched[request.content_offset : request.content_offset + request.content_limit]
    next_offset = request.content_offset + len(page)
    has_more = next_offset < len(matched)
    return DocumentContentSearch(
        request.content_query,
        document_match_mode(match_modes),
        "COMPLETE",
        len(matched),
        occurrence_count,
        len(page),
        request.content_limit,
        request.content_offset,
        has_more,
        next_offset if has_more else None,
        tuple(page),
    )


def selected_document_scope(config: StewardConfig, scope_id: str) -> ScopeConfig:
    scope = next((item for item in config.scopes if item.scope_id == scope_id), None)
    if scope is None:
        raise DocumentInspectionScopeError("configured scope is unavailable")
    if not scope.enabled or scope.role not in _ACTIONABLE_ROLES:
        raise DocumentInspectionScopeError(
            "configured scope is not enabled for document inspection"
        )
    if scope.follow_directory_symlinks or scope.allow_cross_mount:
        raise DocumentInspectionScopeError(
            "configured scope policies are incompatible with document inspection"
        )
    configured_root = Path(scope.raw_path).expanduser()
    try:
        configured_state = configured_root.lstat()
        root_state = scope.normalized_path.lstat()
    except OSError as error:
        raise DocumentInspectionScopeError("configured scope root is unavailable") from error
    if (
        stat.S_ISLNK(configured_state.st_mode)
        or not stat.S_ISDIR(root_state.st_mode)
        or stat.S_ISLNK(root_state.st_mode)
        or not os.access(scope.normalized_path, os.R_OK)
    ):
        raise DocumentInspectionScopeError("configured scope root is not a readable directory")
    return scope


def validate_document_scoped_path(
    config: StewardConfig, scope: ScopeConfig, relative_path: str
) -> None:
    binding = ScopeBinding(scope.scope_id, scope.normalized_path)
    try:
        binding.resolve_relative_path(relative_path)
    except RuntimeFailure as error:
        raise DocumentInspectionInputError("scoped relative path is invalid") from error
    candidate = scope.normalized_path / Path(relative_path)
    if any(
        item.role == ScopeRole.EXCLUDED_ROOT and contains(item.normalized_path, candidate)
        for item in config.scopes
    ):
        raise DocumentInspectionScopeError("document path is inside a configured exclusion")


def _semantic_digest(observation: NormalizedDocumentObservation) -> str:
    """Hash complete semantic output while excluding variable resource measurements."""
    payload: dict[str, object] = {
        "protocol_version": DOCUMENT_INSPECTION_PROTOCOL_VERSION,
        "status": observation.status,
        "source_format": observation.source_format,
        "backend_name": observation.backend_name,
        "backend_version": observation.backend_version,
        "source_provenance": observation.provenance.payload(),
        "warnings": list(observation.warnings),
        "items": [item.payload() for item in observation.items],
    }
    if observation.identification_reason is not None:
        payload["identification_reason"] = observation.identification_reason
    return sha256(canonical_json(payload)).hexdigest()


def _item_role(item: NormalizedDocumentItem) -> str:
    if item.role is not None:
        return item.role
    kind = item.kind.lower()
    if "heading" in kind or kind.endswith("_title"):
        return "HEADING"
    if "table_cell" in kind or kind == "xlsx_cell":
        return "TABLE_CELL"
    if "table" in kind:
        return "TABLE"
    if "chart" in kind or "image" in kind or "picture" in kind:
        return "FIGURE"
    if "sheet" in kind:
        return "SHEET"
    if "slide" in kind:
        return "SLIDE"
    if kind.endswith(("_document", "_workbook", "_presentation")):
        return "DOCUMENT"
    return "PARAGRAPH"


def _view_items(
    observation: NormalizedDocumentObservation,
    view: str,
) -> tuple[NormalizedDocumentItem, ...]:
    if view == "READ":
        return observation.items
    if view == "TABLES":
        accepted = {"TABLE", "TABLE_CELL"}
    elif view == "FORMULAS":
        accepted = {"FORMULA"}
    else:
        accepted = {
            "DOCUMENT",
            "SECTION",
            "HEADING",
            "TABLE",
            "FIGURE",
            "SHEET",
            "SLIDE",
            "METADATA",
        }
    return tuple(item for item in observation.items if _item_role(item) in accepted)


def _balanced_video_read_items(
    items: tuple[NormalizedDocumentItem, ...],
) -> tuple[NormalizedDocumentItem, ...]:
    """Project video items in deterministic modality-fair presentation order."""
    header_kinds = {
        "video_document",
        "video_analysis_summary",
        "video_video_stream",
        "video_audio_stream",
        "video_subtitle_stream",
        "video_chapter",
        "video_query_window",
    }
    headers: list[NormalizedDocumentItem] = []
    buckets: dict[str, list[NormalizedDocumentItem]] = {
        "SCENE_VISUAL": [],
        "FRAME_OCR": [],
        "EMBEDDED_SUBTITLE": [],
        "AUDIO_ASR": [],
        "VISUAL_SEMANTIC": [],
        "OTHER": [],
    }
    for item in items:
        if item.kind in header_kinds:
            headers.append(item)
        elif item.kind in {"video_scene", "video_representative_frame"}:
            buckets["SCENE_VISUAL"].append(item)
        elif item.kind in {"video_frame_ocr_text", "video_text_track"}:
            buckets["FRAME_OCR"].append(item)
        elif item.kind == "video_embedded_subtitle_cue":
            buckets["EMBEDDED_SUBTITLE"].append(item)
        elif item.kind.startswith("audio_"):
            buckets["AUDIO_ASR"].append(item)
        elif item.kind == "video_visual_semantic_anchor":
            buckets["VISUAL_SEMANTIC"].append(item)
        else:
            buckets["OTHER"].append(item)

    ordered = list(headers)
    positions = {name: 0 for name in buckets}
    while any(positions[name] < len(bucket) for name, bucket in buckets.items()):
        for name, bucket in buckets.items():
            start = positions[name]
            stop = min(len(bucket), start + VIDEO_RESULT_MODALITY_QUOTAS[name])
            ordered.extend(bucket[start:stop])
            positions[name] = stop
    return tuple(ordered)


def _adapter_for(
    scope: ScopeConfig,
    parse_cache: BoundedDocumentParseCache[_WorkerExecution] | None = None,
) -> StructuredDocumentParserAdapter:
    root = scope.normalized_path
    bindings = ScopeBindings(
        (ScopeBinding(scope.scope_id, root),),
        (str(root),),
        (scope.scope_id,),
    )
    return StructuredDocumentParserAdapter(
        ProjectOwnedBoundedDocumentIngress(bindings, require_same_device=True),
        parse_cache=parse_cache,
    )


def _media_source_state(scope: ScopeConfig, relative_path: str) -> tuple[int, ...] | None:
    if Path(relative_path).suffix.casefold() not in _MEDIA_SUFFIXES:
        return None
    try:
        state = (scope.normalized_path / relative_path).lstat()
    except OSError as error:
        raise DocumentInspectionUnavailableError("media source is unavailable") from error
    if not stat.S_ISREG(state.st_mode) or stat.S_ISLNK(state.st_mode):
        raise DocumentInspectionUnavailableError("media source is unavailable")
    return (
        state.st_dev,
        state.st_ino,
        state.st_size,
        state.st_mtime_ns,
        state.st_ctime_ns,
    )


def _result_page_bounds(
    request: DocumentInspectionRequest, source_format: str | None
) -> tuple[int, int]:
    continuation = request.audio_continuation
    if source_format in AUDIO_SOURCE_FORMATS and _valid_audio_result_page_continuation(
        continuation
    ):
        assert isinstance(continuation, dict)
        limit = continuation["limit"]
        next_offset = continuation["next_offset"]
        assert isinstance(limit, int) and not isinstance(limit, bool)
        assert isinstance(next_offset, int) and not isinstance(next_offset, bool)
        return limit, next_offset
    continuation = request.video_continuation
    if source_format in VIDEO_SOURCE_FORMATS and _valid_video_result_page_continuation(
        continuation
    ):
        assert isinstance(continuation, dict)
        limit = continuation["limit"]
        next_offset = continuation["next_offset"]
        assert isinstance(limit, int) and not isinstance(limit, bool)
        assert isinstance(next_offset, int) and not isinstance(next_offset, bool)
        return limit, next_offset
    return request.limit, request.offset


def _audio_result_page_continuation(
    observation: NormalizedDocumentObservation,
    *,
    source_sha256: str | None,
    limit: int,
    next_offset: int,
) -> dict[str, object] | None:
    if observation.source_format not in AUDIO_SOURCE_FORMATS or source_sha256 is None:
        return None
    media = observation.resources.media
    if not isinstance(media, dict):
        return None
    request_digest = media.get("audio_request_digest")
    window_start_ms = media.get("window_start_ms")
    if (
        not isinstance(request_digest, str)
        or _SHA256.fullmatch(request_digest) is None
        or type(window_start_ms) is not int
        or window_start_ms < 0
        or observation.execution is None
        or not observation.execution.attempts
        or observation.execution.attempts[0].cache_status == "DISABLED"
    ):
        return None
    next_window = (
        observation.continuation
        if _valid_audio_time_continuation(observation.continuation)
        else None
    )
    return {
        "schema_name": AUDIO_CONTINUATION_SCHEMA_NAME,
        "schema_version": AUDIO_RESULT_PAGE_CONTINUATION_SCHEMA_VERSION,
        "kind": "RESULT_PAGE",
        "request_digest": request_digest,
        "source_sha256": source_sha256,
        "window_start_ms": window_start_ms,
        "next_offset": next_offset,
        "limit": limit,
        "next_window": next_window,
    }


def _video_result_page_continuation(
    observation: NormalizedDocumentObservation,
    *,
    source_sha256: str | None,
    limit: int,
    next_offset: int,
) -> dict[str, object] | None:
    if observation.source_format not in VIDEO_SOURCE_FORMATS or source_sha256 is None:
        return None
    media = observation.resources.media
    if not isinstance(media, dict):
        return None
    request_digest = media.get("video_request_digest")
    window_start_ms = media.get("window_start_ms")
    if (
        not isinstance(request_digest, str)
        or _SHA256.fullmatch(request_digest) is None
        or type(window_start_ms) is not int
        or window_start_ms < 0
        or observation.execution is None
        or not observation.execution.attempts
        or observation.execution.attempts[0].cache_status == "DISABLED"
    ):
        return None
    next_window = (
        observation.continuation
        if _valid_video_time_continuation(observation.continuation)
        else None
    )
    return {
        "schema_name": VIDEO_CONTINUATION_SCHEMA_NAME,
        "schema_version": VIDEO_RESULT_PAGE_CONTINUATION_SCHEMA_VERSION,
        "kind": "RESULT_PAGE",
        "request_digest": request_digest,
        "source_sha256": source_sha256,
        "window_start_ms": window_start_ms,
        "next_offset": next_offset,
        "limit": limit,
        "next_window": next_window,
    }


def inspect_document(
    config: StewardConfig,
    request: DocumentInspectionRequest,
    *,
    parse_cache: BoundedDocumentParseCache[_WorkerExecution] | None = None,
) -> DocumentInspectionPage:
    """Inspect one current document without persistence, providers, or filesystem writes."""
    _validate_request(request)
    scope = selected_document_scope(config, request.scope_id)
    validate_document_scoped_path(config, scope, request.relative_path)
    media_state_before = _media_source_state(scope, request.relative_path)
    try:
        observation = _adapter_for(scope, parse_cache).observe(
            {
                "scope_id": request.scope_id,
                "relative_path": request.relative_path,
                "parser_profile": request.parser_profile,
                "view": request.view,
                "intent": request.intent,
                "content_query": request.content_query,
                "evidence_page": request.evidence_page,
                "parser_timeout_seconds": request.parser_timeout_seconds,
                "audio_analysis": request.audio_analysis,
                "audio_language": request.audio_language,
                "audio_continuation": request.audio_continuation,
                "video_analysis": request.video_analysis,
                "video_continuation": request.video_continuation,
            }
        )
    except RuntimeFailure as error:
        if error.code == "SCOPE_BINDING_FAILED":
            raise DocumentInspectionInputError("scoped relative path is invalid") from error
        raise DocumentInspectionUnavailableError("document inspection failed safely") from error
    except Exception as error:
        raise DocumentInspectionUnavailableError("document inspection failed safely") from error
    if (
        media_state_before is not None
        and _media_source_state(scope, request.relative_path) != media_state_before
    ):
        raise DocumentInspectionSourceChangedError(
            "media source changed before the bounded result could be published"
        )
    expected = request.expected_source_sha256
    source_sha256 = observation.provenance.source_sha256
    if expected is not None and source_sha256 is not None and source_sha256 != expected:
        raise DocumentInspectionSourceChangedError(
            "document source changed after the caller selected its page sequence"
        )

    if observation.status != "COMPLETE":
        content_search = _content_search(observation, request)
        evidence_selection = (
            build_document_evidence_selection(
                (),
                source_sha256=source_sha256 or "",
                query=request.content_query,
                mode=request.evidence_mode,
                context_items=request.evidence_context_items,
                max_characters=request.evidence_max_characters,
                limit=request.content_limit,
                offset=request.content_offset,
                searchable=False,
                source_format=observation.source_format,
            )
            if request.intent == "EVIDENCE" and request.content_query is not None
            else None
        )
        return DocumentInspectionPage(
            DOCUMENT_INSPECTION_PROTOCOL_VERSION,
            observation.status,
            observation.source_format,
            observation.backend_name,
            observation.backend_version,
            observation.provenance.source_kind,
            observation.provenance.scope_id,
            observation.provenance.relative_path,
            source_sha256,
            observation.identification_reason,
            observation.warnings,
            observation.resources,
            (),
            0,
            0,
            request.limit,
            request.offset,
            False,
            None,
            None,
            observation.execution,
            content_search,
            request.view,
            evidence_selection,
            observation.continuation,
            observation.failure_reason_code,
            observation.failure_exception_type,
        )

    projected_items = _view_items(observation, request.view)
    if (
        request.intent == "READ"
        and request.view == "READ"
        and observation.source_format in VIDEO_SOURCE_FORMATS
    ):
        projected_items = _balanced_video_read_items(projected_items)
    full_count = len(projected_items)
    page_limit, page_offset = _result_page_bounds(request, observation.source_format)
    items = projected_items[page_offset : page_offset + page_limit]
    next_offset = page_offset + len(items)
    has_more = next_offset < full_count
    page_continuation: dict[str, object] | None = None
    if has_more and request.intent == "READ" and request.view == "READ":
        page_continuation = _audio_result_page_continuation(
            observation,
            source_sha256=source_sha256,
            limit=page_limit,
            next_offset=next_offset,
        ) or _video_result_page_continuation(
            observation,
            source_sha256=source_sha256,
            limit=page_limit,
            next_offset=next_offset,
        )
    continuation = page_continuation or observation.continuation
    content_search = _content_search(observation, request)
    evidence_selection = (
        build_document_evidence_selection(
            observation.items,
            source_sha256=source_sha256 or "",
            query=request.content_query,
            mode=request.evidence_mode,
            context_items=request.evidence_context_items,
            max_characters=request.evidence_max_characters,
            limit=request.content_limit,
            offset=request.content_offset,
            searchable=True,
            source_format=observation.source_format,
        )
        if request.intent == "EVIDENCE" and request.content_query is not None
        else None
    )
    return DocumentInspectionPage(
        DOCUMENT_INSPECTION_PROTOCOL_VERSION,
        observation.status,
        observation.source_format,
        observation.backend_name,
        observation.backend_version,
        observation.provenance.source_kind,
        observation.provenance.scope_id,
        observation.provenance.relative_path,
        source_sha256,
        observation.identification_reason,
        observation.warnings,
        observation.resources,
        items,
        full_count,
        len(items),
        page_limit,
        page_offset,
        has_more,
        next_offset if has_more else None,
        _semantic_digest(observation),
        observation.execution,
        content_search,
        request.view,
        evidence_selection,
        continuation,
        observation.failure_reason_code,
        observation.failure_exception_type,
    )


__all__ = [
    "DOCUMENT_INSPECTION_PROTOCOL_VERSION",
    "MAX_DOCUMENT_CONTENT_EXCERPT_CHARS",
    "MAX_DOCUMENT_CONTENT_MATCHES",
    "MAX_DOCUMENT_CONTENT_QUERY_BYTES",
    "MAX_DOCUMENT_INSPECTION_PAGE_ITEMS",
    "DocumentContentMatch",
    "DocumentContentSearch",
    "DocumentInspectionPage",
    "DocumentInspectionRequest",
    "inspect_document",
    "selected_document_scope",
    "validate_document_scoped_path",
]
