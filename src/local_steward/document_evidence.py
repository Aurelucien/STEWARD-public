"""Deterministic hierarchy-aware evidence slices over one document graph.

This module selects already observed document items. It never invents facts,
performs semantic ranking, or persists a derived index. Source admission and
parser execution remain owned by :mod:`local_steward.document_observation`.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from .evidence import canonical_json
from .document_query import document_match_mode, match_document_text
from .file_agent.runtime.structured_documents import NormalizedDocumentItem


DOCUMENT_EVIDENCE_SELECTION_SCHEMA_NAME = "local_steward.document_evidence_selection"
DOCUMENT_EVIDENCE_SELECTION_SCHEMA_VERSION = 2
DOCUMENT_EVIDENCE_SELECTION_DIGEST_DOMAIN = "local_steward.document_evidence_selection.v2"
DEFAULT_DOCUMENT_EVIDENCE_CONTEXT_ITEMS = 2
DEFAULT_DOCUMENT_EVIDENCE_MAX_CHARS = 12_000
MAX_DOCUMENT_EVIDENCE_CONTEXT_ITEMS = 8
MAX_DOCUMENT_EVIDENCE_MAX_CHARS = 32_768
MAX_DOCUMENT_EVIDENCE_ITEM_CHARS = 2_048
MAX_DOCUMENT_EVIDENCE_SLICE_CHARS = 4_096
EVIDENCE_SELECTION_MODES = frozenset({"AUTO", "MATCH", "WINDOW", "SECTION"})


@dataclass(frozen=True, slots=True)
class DocumentEvidenceItem:
    """One source item retained inside a bounded evidence slice."""

    item_index: int
    node_id: str | None
    role: str
    kind: str
    relation: str
    location: dict[str, int | str]
    native_location: dict[str, object]
    parent: str | None
    text: str | None
    text_truncated: bool
    match_mode: str | None = None

    def payload(self) -> dict[str, object]:
        value: dict[str, object] = {
            "item_index": self.item_index,
            "role": self.role,
            "kind": self.kind,
            "relation": self.relation,
            "location": self.location,
            "native_location": self.native_location,
            "text_truncated": self.text_truncated,
        }
        if self.node_id is not None:
            value["node_id"] = self.node_id
        if self.parent is not None:
            value["parent"] = self.parent
        if self.text is not None:
            value["text"] = self.text
        if self.match_mode is not None:
            value["match_mode"] = self.match_mode
        return value


@dataclass(frozen=True, slots=True)
class DocumentEvidenceSlice:
    """One query anchor plus its deterministic structural neighborhood."""

    slice_id: str
    anchor_item_index: int
    anchor_node_id: str | None
    selection_mode: str
    heading_trail: tuple[int, ...]
    page_numbers: tuple[int, ...]
    items: tuple[DocumentEvidenceItem, ...]
    selected_character_count: int
    omitted_item_count: int
    truncated: bool

    def payload(self) -> dict[str, object]:
        value: dict[str, object] = {
            "slice_id": self.slice_id,
            "anchor_item_index": self.anchor_item_index,
            "selection_mode": self.selection_mode,
            "heading_trail": list(self.heading_trail),
            "page_numbers": list(self.page_numbers),
            "items": [item.payload() for item in self.items],
            "selected_character_count": self.selected_character_count,
            "omitted_item_count": self.omitted_item_count,
            "truncated": self.truncated,
        }
        if self.anchor_node_id is not None:
            value["anchor_node_id"] = self.anchor_node_id
        return value


@dataclass(frozen=True, slots=True)
class DocumentEvidenceSelection:
    """Bounded deterministic query projection over a complete observation."""

    status: str
    query: str
    match_mode: str
    requested_mode: str
    processing_strategy: str
    parsed_item_count: int
    matched_item_count: int
    matched_occurrence_count: int
    returned_slice_count: int
    selected_item_count: int
    selected_character_count: int
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None
    context_items: int
    max_characters: int
    slices: tuple[DocumentEvidenceSlice, ...]
    selection_digest: str

    def payload(self) -> dict[str, object]:
        return {
            "schema_name": DOCUMENT_EVIDENCE_SELECTION_SCHEMA_NAME,
            "schema_version": DOCUMENT_EVIDENCE_SELECTION_SCHEMA_VERSION,
            "status": self.status,
            "query": self.query,
            "match_mode": self.match_mode,
            "requested_mode": self.requested_mode,
            "processing_strategy": self.processing_strategy,
            "parsed_item_count": self.parsed_item_count,
            "matched_item_count": self.matched_item_count,
            "matched_occurrence_count": self.matched_occurrence_count,
            "returned_slice_count": self.returned_slice_count,
            "selected_item_count": self.selected_item_count,
            "selected_character_count": self.selected_character_count,
            "limit": self.limit,
            "offset": self.offset,
            "has_more": self.has_more,
            "next_offset": self.next_offset,
            "context_items": self.context_items,
            "max_characters": self.max_characters,
            "slices": [item.payload() for item in self.slices],
            "selection_digest": self.selection_digest,
        }


def _digest(value: object) -> str:
    return sha256(
        DOCUMENT_EVIDENCE_SELECTION_DIGEST_DOMAIN.encode("utf-8") + b"\0" + canonical_json(value)
    ).hexdigest()


def _role(item: NormalizedDocumentItem) -> str:
    if item.role is not None:
        return item.role
    kind = item.kind.lower()
    if "heading" in kind or kind.endswith("_title"):
        return "HEADING"
    if "table_cell" in kind or kind == "xlsx_cell":
        return "TABLE_CELL"
    if "table" in kind:
        return "TABLE"
    return "PARAGRAPH"


def _heading_level(item: NormalizedDocumentItem) -> int:
    extension = item.extension or {}
    level = extension.get("level")
    if isinstance(level, int) and not isinstance(level, bool) and level >= 0:
        return level
    depth = item.location.get("depth")
    if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0:
        return depth
    return 1


def _heading_trail(items: tuple[NormalizedDocumentItem, ...], anchor_index: int) -> tuple[int, ...]:
    node_index = {
        item.node_id: index for index, item in enumerate(items) if item.node_id is not None
    }
    trail: set[int] = set()
    parent = items[anchor_index].parent
    seen: set[str] = set()
    while parent is not None and parent not in seen:
        seen.add(parent)
        parent_index = node_index.get(parent)
        if parent_index is None:
            break
        parent_item = items[parent_index]
        if _role(parent_item) == "HEADING":
            trail.add(parent_index)
        parent = parent_item.parent

    threshold = 1 << 30
    for index in range(anchor_index - 1, -1, -1):
        candidate = items[index]
        if _role(candidate) != "HEADING":
            continue
        level = _heading_level(candidate)
        if level < threshold:
            trail.add(index)
            threshold = level
            if level <= 1:
                break
    return tuple(sorted(trail))


def _logical_container(item: NormalizedDocumentItem) -> tuple[str, int | str] | None:
    for key in ("page", "sheet_index", "sheet", "slide"):
        value = item.location.get(key)
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            return key, value
    return None


def _native_location(
    item: NormalizedDocumentItem,
    *,
    source_format: str | None,
    section_path: tuple[str, ...],
) -> dict[str, object]:
    """Publish one user-readable, format-native locator plus the raw graph location."""

    location = item.location
    parts: list[str] = []
    locator_parts: list[str] = []
    kind = "DOCUMENT_ITEM"
    if source_format in {"PDF", "PNG", "JPEG", "TIFF"}:
        page = location.get("page", 1 if source_format in {"PNG", "JPEG", "TIFF"} else None)
        if isinstance(page, int) and not isinstance(page, bool):
            parts.append(f"page {page}")
            locator_parts.append(f"page:{page}")
            kind = "IMAGE_REGION" if source_format in {"PNG", "JPEG", "TIFF"} else "PDF_PAGE"
        block = location.get("block")
        if isinstance(block, int) and not isinstance(block, bool):
            parts.append(f"block {block}")
            locator_parts.append(f"block:{block}")
        for key in ("outline", "annotation", "form_field"):
            value = location.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                parts.append(f"{key.replace('_', ' ')} {value}")
                locator_parts.append(f"{key}:{value}")
        if "annotation" in location:
            kind = "PDF_ANNOTATION"
        elif "form_field" in location:
            kind = "PDF_FORM_FIELD"
        elif "outline" in location:
            kind = "PDF_OUTLINE"
    elif source_format == "XLSX":
        sheet = location.get("sheet")
        if isinstance(sheet, str):
            parts.append(f'sheet "{sheet}"')
            locator_parts.append(f"sheet:{sheet}")
        cell = location.get("cell")
        table = location.get("table")
        chart = location.get("chart")
        if isinstance(cell, str):
            parts.append(f"cell {cell}")
            locator_parts.append(f"cell:{cell}")
            kind = "WORKBOOK_CELL"
        elif isinstance(table, str):
            parts.append(f'table "{table}"')
            locator_parts.append(f"table:{table}")
            kind = "WORKBOOK_TABLE"
        elif isinstance(chart, int) and not isinstance(chart, bool):
            parts.append(f"chart {chart}")
            locator_parts.append(f"chart:{chart}")
            kind = "WORKBOOK_CHART"
        else:
            kind = "WORKBOOK_SHEET"
        comment = location.get("comment")
        if isinstance(comment, int) and not isinstance(comment, bool):
            parts.append(f"comment {comment}")
            locator_parts.append(f"comment:{comment}")
            kind = "WORKBOOK_COMMENT"
    elif source_format == "PPTX":
        slide = location.get("slide")
        if isinstance(slide, int) and not isinstance(slide, bool):
            parts.append(f"slide {slide}")
            locator_parts.append(f"slide:{slide}")
        for key in (
            "shape",
            "table",
            "row",
            "column",
            "chart",
            "notes",
            "accessibility",
        ):
            value = location.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                parts.append(f"{key} {value}")
                locator_parts.append(f"{key}:{value}")
        if "notes" in location:
            kind = "PRESENTATION_SPEAKER_NOTES"
        elif "chart" in location:
            kind = "PRESENTATION_CHART"
        elif "accessibility" in location:
            kind = "PRESENTATION_ACCESSIBILITY"
        elif "row" in location:
            kind = "PRESENTATION_TABLE_CELL"
        else:
            kind = "PRESENTATION_SLIDE_ITEM"
    elif source_format == "DOCX":
        section = location.get("section")
        if isinstance(section, int) and not isinstance(section, bool):
            parts.append(f"section {section}")
            locator_parts.append(f"section:{section}")
        block = location.get("block")
        if isinstance(block, int) and not isinstance(block, bool):
            parts.append(f"block {block}")
            locator_parts.append(f"block:{block}")
        for key in (
            "table",
            "row",
            "column",
            "comment",
            "footnote",
            "endnote",
            "revision",
        ):
            value = location.get(key)
            if isinstance(value, (int, str)) and not isinstance(value, bool):
                parts.append(f"{key} {value}")
                locator_parts.append(f"{key}:{value}")
        if "comment" in location:
            kind = "WORD_COMMENT"
        elif "footnote" in location:
            kind = "WORD_FOOTNOTE"
        elif "endnote" in location:
            kind = "WORD_ENDNOTE"
        elif "revision" in location:
            kind = "WORD_REVISION"
        else:
            kind = "WORD_TABLE_CELL" if "row" in location else "WORD_BLOCK"
    elif source_format == "EPUB":
        section = location.get("section")
        title = location.get("section_title")
        if isinstance(section, int) and not isinstance(section, bool):
            parts.append(f"section {section}")
            locator_parts.append(f"section:{section}")
            if isinstance(title, str) and title:
                parts.append(f'heading "{title}"')
        ordinal = location.get("ordinal")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            parts.append(f"item {ordinal}")
            locator_parts.append(f"item:{ordinal}")
        kind = "EPUB_SECTION_ITEM"
    elif source_format in {"WAV", "FLAC", "MP3", "M4A", "AAC", "OGG", "OPUS"}:
        start_ms = location.get("start_ms")
        end_ms = location.get("end_ms")
        if isinstance(start_ms, int) and not isinstance(start_ms, bool):
            parts.append(f"{start_ms / 1000:.3f}s")
            locator_parts.append(f"start_ms:{start_ms}")
        if isinstance(end_ms, int) and not isinstance(end_ms, bool):
            parts.append(f"to {end_ms / 1000:.3f}s")
            locator_parts.append(f"end_ms:{end_ms}")
        kind = "AUDIO_TIME_RANGE"
    elif source_format in {"MP4", "MOV", "MKV", "WEBM"}:
        start_ms = location.get("start_ms", location.get("timestamp_ms"))
        end_ms = location.get("end_ms", start_ms)
        stream_index = location.get("stream_index")
        if isinstance(start_ms, int) and not isinstance(start_ms, bool):
            parts.append(f"{start_ms / 1000:.3f}s")
            locator_parts.append(f"start_ms:{start_ms}")
        if isinstance(end_ms, int) and not isinstance(end_ms, bool):
            parts.append(f"to {end_ms / 1000:.3f}s")
            locator_parts.append(f"end_ms:{end_ms}")
        if isinstance(stream_index, int) and not isinstance(stream_index, bool):
            parts.append(f"stream {stream_index}")
            locator_parts.append(f"stream:{stream_index}")
        kind = "VIDEO_TIME_RANGE"
    else:
        ordinal = location.get("ordinal")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            parts.append(f"item {ordinal}")
            locator_parts.append(f"item:{ordinal}")

    if not parts:
        parts.append(item.node_id or item.kind)
        locator_parts.append(item.node_id or item.kind)
    result: dict[str, object] = {
        "kind": kind,
        "label": ", ".join(parts),
        "locator": "/".join(locator_parts),
    }
    if section_path:
        result["section_path"] = list(section_path)
    extension = item.extension or {}
    visual_region = extension.get("visual_region")
    if isinstance(visual_region, dict):
        result["visual_region"] = visual_region
    formula = extension.get("formula")
    if isinstance(formula, str):
        result["formula"] = formula
    return result


def _window_indexes(
    items: tuple[NormalizedDocumentItem, ...], anchor_index: int, context_items: int
) -> set[int]:
    container = _logical_container(items[anchor_index])
    selected = {anchor_index}
    for direction in (-1, 1):
        index = anchor_index + direction
        retained = 0
        while 0 <= index < len(items) and retained < context_items:
            candidate = items[index]
            candidate_container = _logical_container(candidate)
            if container is not None and candidate_container is not None:
                if candidate_container != container:
                    break
            if candidate.text_or_value is not None or _role(candidate) in {
                "HEADING",
                "TABLE",
                "FIGURE",
            }:
                selected.add(index)
                retained += 1
            index += direction
    return selected


def _section_indexes(
    items: tuple[NormalizedDocumentItem, ...],
    anchor_index: int,
    heading_trail: tuple[int, ...],
    context_items: int,
) -> set[int]:
    anchor = items[anchor_index]
    role = _role(anchor)
    if role in {"TABLE", "TABLE_CELL"} and anchor.parent is not None:
        same_parent = {index for index, item in enumerate(items) if item.parent == anchor.parent}
        if same_parent:
            return same_parent | {anchor_index}
    heading_index = (
        anchor_index if role == "HEADING" else (heading_trail[-1] if heading_trail else None)
    )
    if heading_index is None:
        return _window_indexes(items, anchor_index, context_items)
    level = _heading_level(items[heading_index])
    end = len(items)
    for index in range(heading_index + 1, len(items)):
        if _role(items[index]) == "HEADING" and _heading_level(items[index]) <= level:
            end = index
            break
    return set(range(heading_index, end)) | {anchor_index}


def _bounded_item(
    item: NormalizedDocumentItem,
    *,
    item_index: int,
    relation: str,
    remaining: int,
    source_format: str | None,
    section_path: tuple[str, ...],
    match_mode: str | None,
) -> DocumentEvidenceItem:
    text = item.text_or_value
    truncated = False
    if text is not None:
        item_limit = min(MAX_DOCUMENT_EVIDENCE_ITEM_CHARS, max(0, remaining))
        if len(text) > item_limit:
            text = text[:item_limit]
            truncated = True
    return DocumentEvidenceItem(
        item_index,
        item.node_id,
        _role(item),
        item.kind,
        relation,
        dict(item.location),
        _native_location(item, source_format=source_format, section_path=section_path),
        item.parent,
        text,
        truncated,
        match_mode,
    )


def _build_slice(
    items: tuple[NormalizedDocumentItem, ...],
    *,
    source_sha256: str,
    query: str,
    anchor_index: int,
    requested_mode: str,
    context_items: int,
    character_budget: int,
    source_format: str | None,
    anchor_match_mode: str,
) -> DocumentEvidenceSlice:
    trail = _heading_trail(items, anchor_index)
    anchor_role = _role(items[anchor_index])
    mode = requested_mode
    if mode == "AUTO":
        mode = "SECTION" if trail or anchor_role in {"HEADING", "TABLE", "TABLE_CELL"} else "WINDOW"
    selected = {anchor_index}
    if mode == "WINDOW":
        selected |= _window_indexes(items, anchor_index, context_items)
    elif mode == "SECTION":
        selected |= _section_indexes(items, anchor_index, trail, context_items)
    if mode != "MATCH":
        selected.update(trail)

    relations: dict[int, str] = {index: "CONTEXT" for index in selected}
    for index in trail:
        relations[index] = "HEADING_TRAIL"
    relations[anchor_index] = "ANCHOR"
    priority = sorted(
        selected,
        key=lambda index: (
            0 if index == anchor_index else 1 if index in trail else 2,
            abs(index - anchor_index),
            index,
        ),
    )
    accepted: dict[int, DocumentEvidenceItem] = {}
    used = 0
    slice_budget = min(character_budget, MAX_DOCUMENT_EVIDENCE_SLICE_CHARS)
    section_path = tuple(
        text for index in trail if isinstance((text := items[index].text_or_value), str) and text
    )
    for index in priority:
        raw_text = items[index].text_or_value or ""
        if used >= slice_budget and index != anchor_index:
            continue
        bounded = _bounded_item(
            items[index],
            item_index=index,
            relation=relations[index],
            remaining=max(0, slice_budget - used),
            source_format=source_format,
            section_path=section_path,
            match_mode=anchor_match_mode if index == anchor_index else None,
        )
        text_length = len(bounded.text or "")
        if text_length == 0 and raw_text and index != anchor_index:
            continue
        accepted[index] = bounded
        used += text_length
    projected = tuple(accepted[index] for index in sorted(accepted))
    pages = tuple(
        sorted(
            {
                page
                for projected_item in projected
                if isinstance((page := projected_item.location.get("page")), int)
                and not isinstance(page, bool)
            }
        )
    )
    identity = {
        "source_sha256": source_sha256,
        "query": query,
        "anchor_item_index": anchor_index,
        "anchor_node_id": items[anchor_index].node_id,
        "selection_mode": mode,
        "items": [item.payload() for item in projected],
    }
    omitted = len(selected) - len(projected)
    return DocumentEvidenceSlice(
        f"slice:{_digest(identity)[:32]}",
        anchor_index,
        items[anchor_index].node_id,
        mode,
        trail,
        pages,
        projected,
        used,
        omitted,
        omitted > 0 or any(item.text_truncated for item in projected),
    )


def build_document_evidence_selection(
    items: tuple[NormalizedDocumentItem, ...],
    *,
    source_sha256: str,
    query: str,
    mode: str,
    context_items: int,
    max_characters: int,
    limit: int,
    offset: int,
    searchable: bool,
    source_format: str | None = None,
) -> DocumentEvidenceSelection:
    """Select source-pinned evidence slices without heuristic or model ranking."""

    matches = tuple(match_document_text(item.text_or_value, query) for item in items)
    counts = tuple(match.count if match is not None else 0 for match in matches)
    anchors = tuple(index for index, count in enumerate(counts) if count)
    occurrence_count = sum(counts)
    slices: list[DocumentEvidenceSlice] = []
    used = 0
    if searchable:
        for anchor_index in anchors[offset : offset + limit]:
            if used >= max_characters:
                break
            anchor_match = matches[anchor_index]
            if anchor_match is None:  # pragma: no cover - guaranteed by anchors
                continue
            evidence_slice = _build_slice(
                items,
                source_sha256=source_sha256,
                query=query,
                anchor_index=anchor_index,
                requested_mode=mode,
                context_items=context_items,
                character_budget=max_characters - used,
                source_format=source_format,
                anchor_match_mode=anchor_match.mode,
            )
            slices.append(evidence_slice)
            used += evidence_slice.selected_character_count
    consumed_anchors = len(slices)
    next_offset = offset + consumed_anchors
    has_more = next_offset < len(anchors)
    status = "NOT_SEARCHABLE" if not searchable else "COMPLETE" if anchors else "NO_MATCH"
    payload_without_digest = {
        "status": status,
        "query": query,
        "requested_mode": mode,
        "source_sha256": source_sha256,
        "source_format": source_format,
        "parsed_item_count": len(items),
        "matched_item_count": len(anchors),
        "matched_occurrence_count": occurrence_count,
        "limit": limit,
        "offset": offset,
        "context_items": context_items,
        "max_characters": max_characters,
        "slices": [item.payload() for item in slices],
    }
    return DocumentEvidenceSelection(
        status,
        query,
        document_match_mode({match.mode for match in matches if match is not None}),
        mode,
        "QUERY_MAP_THEN_BOUNDED_GRAPH_SELECTION",
        len(items),
        len(anchors),
        occurrence_count,
        len(slices),
        sum(len(item.items) for item in slices),
        used,
        limit,
        offset,
        has_more,
        next_offset if has_more else None,
        context_items,
        max_characters,
        tuple(slices),
        _digest(payload_without_digest),
    )


__all__ = [
    "DEFAULT_DOCUMENT_EVIDENCE_CONTEXT_ITEMS",
    "DEFAULT_DOCUMENT_EVIDENCE_MAX_CHARS",
    "DOCUMENT_EVIDENCE_SELECTION_SCHEMA_NAME",
    "DOCUMENT_EVIDENCE_SELECTION_SCHEMA_VERSION",
    "EVIDENCE_SELECTION_MODES",
    "MAX_DOCUMENT_EVIDENCE_CONTEXT_ITEMS",
    "MAX_DOCUMENT_EVIDENCE_MAX_CHARS",
    "DocumentEvidenceItem",
    "DocumentEvidenceSelection",
    "DocumentEvidenceSlice",
    "build_document_evidence_selection",
]
