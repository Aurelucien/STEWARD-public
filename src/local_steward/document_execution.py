"""Deterministic document execution planning and bounded process-memory reuse."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic
from typing import Generic, Protocol, TypeVar


DOCUMENT_EXECUTION_SCHEMA_NAME = "local_steward.document_execution"
DOCUMENT_EXECUTION_SCHEMA_VERSION = 3
DEFAULT_DOCUMENT_CACHE_MAX_ENTRIES = 8
DEFAULT_DOCUMENT_CACHE_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_DOCUMENT_CACHE_TTL_SECONDS = 600.0


class QualityItem(Protocol):
    @property
    def text_or_value(self) -> str | None: ...

    @property
    def role(self) -> str | None: ...

    @property
    def kind(self) -> str: ...

    @property
    def location(self) -> dict[str, int | str]: ...


@dataclass(frozen=True, slots=True)
class DocumentQualityAssessment:
    """Observable extraction signals; never an unsupported accuracy claim."""

    status: str
    reason_codes: tuple[str, ...]
    text_characters: int
    alphanumeric_characters: int
    text_item_count: int
    page_count: int
    empty_page_count: int
    structural_role_count: int

    def payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "text_characters": self.text_characters,
            "alphanumeric_characters": self.alphanumeric_characters,
            "text_item_count": self.text_item_count,
            "page_count": self.page_count,
            "empty_page_count": self.empty_page_count,
            "structural_role_count": self.structural_role_count,
        }


@dataclass(frozen=True, slots=True)
class DocumentExecutionAttempt:
    profile: str
    backend_name: str | None
    status: str
    cache_status: str
    quality: DocumentQualityAssessment
    failure_reason_code: str | None = None
    failure_exception_type: str | None = None

    def payload(self) -> dict[str, object]:
        value: dict[str, object] = {
            "profile": self.profile,
            "backend_name": self.backend_name,
            "status": self.status,
            "cache_status": self.cache_status,
            "quality": self.quality.payload(),
        }
        if self.failure_reason_code is not None:
            value["failure_reason_code"] = self.failure_reason_code
        if self.failure_exception_type is not None:
            value["failure_exception_type"] = self.failure_exception_type
        return value


@dataclass(frozen=True, slots=True)
class DocumentContainerQuality:
    """Quality of the query-relevant native container, not the whole file."""

    container_id: str
    container_kind: str
    native_label: str
    matched_item_count: int
    quality: DocumentQualityAssessment

    def payload(self) -> dict[str, object]:
        return {
            "container_id": self.container_id,
            "container_kind": self.container_kind,
            "native_label": self.native_label,
            "matched_item_count": self.matched_item_count,
            "quality": self.quality.payload(),
        }


@dataclass(frozen=True, slots=True)
class DocumentExecutionSelection:
    """Observed query map and bounded parser interval for evidence extraction."""

    strategy: str
    match_mode: str
    map_profile: str
    mapped_item_count: int
    matched_item_count: int
    matched_page_numbers: tuple[int, ...]
    selected_page_start: int | None
    selected_page_end: int | None
    omitted_matched_page_numbers: tuple[int, ...]
    matched_container_ids: tuple[str, ...] = ()
    selected_container_ids: tuple[str, ...] = ()
    omitted_matched_container_ids: tuple[str, ...] = ()
    container_qualities: tuple[DocumentContainerQuality, ...] = ()

    def payload(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "match_mode": self.match_mode,
            "map_profile": self.map_profile,
            "mapped_item_count": self.mapped_item_count,
            "matched_item_count": self.matched_item_count,
            "matched_page_numbers": list(self.matched_page_numbers),
            "selected_page_start": self.selected_page_start,
            "selected_page_end": self.selected_page_end,
            "omitted_matched_page_numbers": list(self.omitted_matched_page_numbers),
            "matched_container_ids": list(self.matched_container_ids),
            "selected_container_ids": list(self.selected_container_ids),
            "omitted_matched_container_ids": list(
                self.omitted_matched_container_ids
            ),
            "container_qualities": [
                quality.payload() for quality in self.container_qualities
            ],
        }


@dataclass(frozen=True, slots=True)
class DocumentExecutionTrace:
    requested_profile: str
    requested_intent: str
    requested_view: str
    initial_profile: str
    selected_profile: str | None
    escalation_reason: str | None
    attempts: tuple[DocumentExecutionAttempt, ...]
    selection: DocumentExecutionSelection | None = None

    def payload(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_name": DOCUMENT_EXECUTION_SCHEMA_NAME,
            "schema_version": DOCUMENT_EXECUTION_SCHEMA_VERSION,
            "requested_profile": self.requested_profile,
            "requested_intent": self.requested_intent,
            "requested_view": self.requested_view,
            "initial_profile": self.initial_profile,
            "selected_profile": self.selected_profile,
            "escalation_reason": self.escalation_reason,
            "attempts": [attempt.payload() for attempt in self.attempts],
            "reuse_scope": "PROCESS_MEMORY",
            "persistence_effect": "NONE",
        }
        if self.selection is not None:
            value["selection"] = self.selection.payload()
        return value


def initial_document_profile(
    source_format: str, requested_profile: str, view: str, intent: str = "READ"
) -> str:
    """Choose a deterministic first parser profile without probing document content."""

    if requested_profile == "FAST" and source_format in {"EPUB", "PNG", "JPEG", "TIFF"}:
        return "DEEP"
    if requested_profile != "AUTO":
        return requested_profile
    if view == "FORMULAS" and source_format == "XLSX":
        return "FORMULA_NATIVE"
    if view == "FORMULAS":
        return "ENRICHED"
    if source_format in {"EPUB", "PNG", "JPEG", "TIFF"}:
        return "DEEP"
    if intent in {"LOCATE", "EVIDENCE"}:
        return "FAST"
    if view == "STRUCTURE" and source_format == "PDF":
        return "STRUCTURE_NATIVE"
    if view in {"STRUCTURE", "TABLES"} and source_format in {"PDF", "DOCX"}:
        return "DEEP"
    return "FAST"


def assess_document_quality(
    items: Sequence[QualityItem],
    *,
    status: str,
    source_format: str,
    view: str,
) -> DocumentQualityAssessment:
    """Classify observable sufficiency for the requested view conservatively."""

    texts = [item.text_or_value for item in items if isinstance(item.text_or_value, str)]
    text_characters = sum(len(value.strip()) for value in texts)
    alphanumeric = sum(sum(char.isalnum() for char in value) for value in texts)
    page_text: dict[int, int] = {}
    roles: set[str] = set()
    for item in items:
        if item.role is not None:
            roles.add(item.role)
        page = item.location.get("page")
        if isinstance(page, int) and not isinstance(page, bool):
            page_text[page] = page_text.get(page, 0) + sum(
                char.isalnum() for char in (item.text_or_value or "")
            )
    reasons: list[str] = []
    if status != "COMPLETE":
        reasons.append("PARSER_NOT_COMPLETE")
    elif alphanumeric < 8:
        reasons.append("INSUFFICIENT_EXTRACTABLE_TEXT")
    if page_text:
        empty_pages = sum(value < 4 for value in page_text.values())
        if len(page_text) >= 2 and empty_pages * 2 > len(page_text):
            reasons.append("MAJORITY_EMPTY_PAGES")
    else:
        empty_pages = 0
    if status == "COMPLETE" and view == "STRUCTURE" and not roles.intersection(
        {"DOCUMENT", "SECTION", "HEADING", "SHEET", "SLIDE", "TABLE", "FIGURE"}
    ):
        reasons.append("REQUESTED_STRUCTURE_NOT_OBSERVED")
    if status == "COMPLETE" and view == "TABLES" and not roles.intersection(
        {"TABLE", "TABLE_CELL"}
    ):
        reasons.append("REQUESTED_TABLE_STRUCTURE_NOT_OBSERVED")
    if status == "COMPLETE" and view == "FORMULAS" and "FORMULA" not in roles:
        reasons.append("REQUESTED_FORMULA_NOT_OBSERVED")
    return DocumentQualityAssessment(
        "SUFFICIENT" if not reasons else "INSUFFICIENT",
        tuple(dict.fromkeys(reasons)),
        text_characters,
        alphanumeric,
        len(texts),
        len(page_text),
        empty_pages,
        len(roles),
    )


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _CacheEntry(Generic[T]):
    value: T
    size_bytes: int
    expires_at: float


class BoundedDocumentParseCache(Generic[T]):
    """TTL/LRU cache with per-key single-flight and no persistent artifacts."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_DOCUMENT_CACHE_MAX_ENTRIES,
        max_bytes: int = DEFAULT_DOCUMENT_CACHE_MAX_BYTES,
        ttl_seconds: float = DEFAULT_DOCUMENT_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_entries < 1 or max_bytes < 1 or ttl_seconds <= 0:
            raise ValueError("document cache bounds must be positive")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str, str], _CacheEntry[T]] = OrderedDict()
        self._inflight: dict[tuple[str, str, str], Event] = {}
        self._total_bytes = 0
        self._lock = Lock()

    def get_or_compute(
        self,
        key: tuple[str, str, str],
        compute: Callable[[], T],
        *,
        size_of: Callable[[T], int],
        cacheable: Callable[[T], bool],
    ) -> tuple[T, str]:
        while True:
            with self._lock:
                self._expire_locked()
                cached = self._entries.get(key)
                if cached is not None:
                    self._entries.move_to_end(key)
                    return cached.value, "HIT"
                event = self._inflight.get(key)
                if event is None:
                    event = Event()
                    self._inflight[key] = event
                    owner = True
                else:
                    owner = False
            if owner:
                break
            event.wait()
        try:
            value = compute()
            if cacheable(value):
                size_bytes = size_of(value)
                if 0 <= size_bytes <= self._max_bytes:
                    with self._lock:
                        self._insert_locked(key, value, size_bytes)
            return value, "MISS"
        finally:
            with self._lock:
                completed = self._inflight.pop(key, None)
                if completed is not None:
                    completed.set()

    def get_existing(self, key: tuple[str, str, str]) -> T | None:
        """Return one live cached value without computing or waiting for new work."""
        with self._lock:
            self._expire_locked()
            cached = self._entries.get(key)
            if cached is None:
                return None
            self._entries.move_to_end(key)
            return cached.value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total_bytes = 0

    def _expire_locked(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            entry = self._entries.pop(key)
            self._total_bytes -= entry.size_bytes

    def _insert_locked(self, key: tuple[str, str, str], value: T, size_bytes: int) -> None:
        replaced = self._entries.pop(key, None)
        if replaced is not None:
            self._total_bytes -= replaced.size_bytes
        self._entries[key] = _CacheEntry(value, size_bytes, self._clock() + self._ttl_seconds)
        self._total_bytes += size_bytes
        while len(self._entries) > self._max_entries or self._total_bytes > self._max_bytes:
            _old_key, old = self._entries.popitem(last=False)
            self._total_bytes -= old.size_bytes


__all__ = [
    "BoundedDocumentParseCache",
    "DocumentContainerQuality",
    "DocumentExecutionAttempt",
    "DocumentExecutionTrace",
    "DocumentQualityAssessment",
    "assess_document_quality",
    "initial_document_profile",
]
