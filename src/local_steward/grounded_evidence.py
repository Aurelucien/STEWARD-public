"""Deterministic, bounded evidence packets for the native Agent surface.

The packet is a presentation wrapper only.  It never adds facts to a historical
Context Projection or to a current structured-document observation; it carries
their authority, locations, bounds, omissions and explicit uncertainty into one
response-ready shape.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from .document_observation import DocumentInspectionPage
from .evidence import canonical_json


GROUNDED_EVIDENCE_PACKET_SCHEMA_NAME = "local_steward.grounded_evidence_packet"
GROUNDED_EVIDENCE_PACKET_SCHEMA_VERSION = 1
GROUNDED_EVIDENCE_PACKET_DIGEST_DOMAIN = "local_steward.grounded_evidence_packet.v1"
DOCUMENT_EVIDENCE_PACKET_SCHEMA_NAME = "local_steward.document_evidence_packet"
DOCUMENT_EVIDENCE_PACKET_SCHEMA_VERSION = 3
DOCUMENT_EVIDENCE_PACKET_DIGEST_DOMAIN = "local_steward.document_evidence_packet.v3"
MAX_GROUNDED_EVIDENCE_FACTS = 64
MAX_GROUNDED_EVIDENCE_TEXT_CHARS = 1_024


def _digest(value: object) -> str:
    return sha256(
        GROUNDED_EVIDENCE_PACKET_DIGEST_DOMAIN.encode("utf-8") + b"\0" + canonical_json(value)
    ).hexdigest()


def _document_packet_digest(value: object) -> str:
    return sha256(
        DOCUMENT_EVIDENCE_PACKET_DIGEST_DOMAIN.encode("utf-8") + b"\0" + canonical_json(value)
    ).hexdigest()


def _citation_id(kind: str, identity: dict[str, object]) -> str:
    return f"citation:{kind.lower()}:{_digest(identity)[:32]}"


def _bounded_text(value: object) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    text = str(value)
    if len(text) <= MAX_GROUNDED_EVIDENCE_TEXT_CHARS:
        return text, False
    return text[:MAX_GROUNDED_EVIDENCE_TEXT_CHARS], True


def _delivery_contract(*, has_facts: bool, model_derived: bool = False) -> dict[str, object]:
    contract: dict[str, object] = {
        "response_contract": "GROUNDED_EVIDENCE_V1",
        "citation_required": has_facts,
        "required_answer_fields": [
            "source",
            "verification",
            "citation_id",
            "unknowns",
            "omissions",
        ],
        "historical_current_distinction_required": True,
        "interpretation_is_not_evidence": True,
        "unknowns_and_omissions_must_be_preserved": True,
    }
    if model_derived:
        contract["model_output_must_not_be_described_as_verbatim"] = True
    return contract


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _finalize(
    packet_kind: str,
    source: dict[str, Any],
    verification: dict[str, Any],
    facts: list[dict[str, Any]],
    interpretations: list[dict[str, Any]],
    unknowns: list[dict[str, Any]],
    omissions: list[dict[str, Any]],
    bounds: dict[str, Any],
    *,
    routing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_derived = any(fact.get("model_derived") is True for fact in facts)
    if model_derived and verification.get("status") == "OBSERVATION_COMPLETE":
        verification = {**verification, "status": "MODEL_OBSERVATION_COMPLETE"}
    packet: dict[str, Any] = {
        "schema_name": GROUNDED_EVIDENCE_PACKET_SCHEMA_NAME,
        "schema_version": GROUNDED_EVIDENCE_PACKET_SCHEMA_VERSION,
        "packet_kind": packet_kind,
        "packet_status": "READY" if facts else "NO_EVIDENCE",
        "source": source,
        "verification": verification,
        "facts": facts,
        "interpretations": interpretations,
        "unknowns": unknowns,
        "omissions": omissions,
        "bounds": bounds,
        "delivery": _delivery_contract(
            has_facts=bool(facts),
            model_derived=model_derived,
        ),
    }
    if routing is not None:
        packet["routing"] = routing
    packet["packet_digest"] = _digest(packet)
    return packet


def _document_item_fact(
    page: DocumentInspectionPage,
    item_index: int,
    item: Any,
    *,
    query: str | None = None,
    excerpt: str | None = None,
    match_count: int | None = None,
    excerpt_truncated: bool = False,
) -> dict[str, Any]:
    value = excerpt if excerpt is not None else item.text_or_value
    bounded, was_truncated = _bounded_text(value)
    audio_model = page.resources.media if page.source_kind == "CURRENT_FILESYSTEM_AUDIO" else None
    ocr_model = page.resources.media if str(item.kind).startswith("pdf_ocr_") else None
    fact: dict[str, Any] = {
        "citation_id": _citation_id(
            "document",
            {
                "source_sha256": page.source_sha256,
                "observation_digest": page.document_observation_digest,
                "item_index": item_index,
                "kind": item.kind,
                "location": item.location,
                "query": query,
            },
        ),
        "fact_kind": "CONTENT_MATCH" if query is not None else "DOCUMENT_ITEM",
        "item_index": item_index,
        "kind": item.kind,
        "location": dict(item.location),
        "parent": item.parent,
        "value": bounded,
        "value_truncated": bool(excerpt_truncated or was_truncated),
        "source_scope_id": page.scope_id,
        "source_relative_path": page.relative_path,
        **({"query": query} if query is not None else {}),
        **({"match_count": match_count} if match_count is not None else {}),
        **(
            {
                "authority": "MODEL_DERIVED",
                "model_derived": True,
                "timestamp_accuracy": "MODEL_APPROXIMATE",
                "model": {
                    key: audio_model.get(key)
                    for key in (
                        "asr_backend",
                        "asr_version",
                        "asr_model_id",
                        "asr_model_revision",
                        "asr_model_identity_sha256",
                        "vad_backend",
                        "vad_version",
                    )
                }
                if isinstance(audio_model, dict)
                else None,
            }
            if page.source_kind == "CURRENT_FILESYSTEM_AUDIO"
            and str(item.kind).startswith(("audio_transcript", "audio_speech"))
            else {}
        ),
    }
    if str(item.kind).startswith("pdf_ocr_"):
        fact.update(
            {
                "authority": "MODEL_DERIVED",
                "model_derived": True,
                "text_accuracy": "OCR_MODEL_APPROXIMATE",
                "model": {
                    "backend_name": (
                        ocr_model.get("ocr_backend")
                        if isinstance(ocr_model, dict)
                        else page.backend_name
                    ),
                    "backend_version": (
                        ocr_model.get("ocr_version")
                        if isinstance(ocr_model, dict)
                        else page.backend_version
                    ),
                },
            }
        )
    return fact


def build_document_evidence_packet(
    page: DocumentInspectionPage,
    *,
    compact_execution: bool = False,
    execution_projection: str = "COLLECTION_SUMMARY",
) -> dict[str, Any]:
    """Wrap one current-document page/search in a bounded grounded packet."""
    if page.evidence_selection is not None:
        return _build_selected_document_evidence_packet(
            page,
            compact_execution=compact_execution,
            execution_projection=execution_projection,
        )
    source: dict[str, Any] = {
        "source_kind": page.source_kind,
        "scope_id": page.scope_id,
        "relative_path": page.relative_path,
        "source_sha256": page.source_sha256,
        "document_observation_digest": page.document_observation_digest,
        "source_format": page.source_format,
        "backend_name": page.backend_name,
        "backend_version": page.backend_version,
    }
    if page.source_kind == "CURRENT_FILESYSTEM_AUDIO" and page.resources.media is not None:
        source["model"] = {
            key: page.resources.media.get(key)
            for key in (
                "asr_backend",
                "asr_version",
                "asr_model_id",
                "asr_model_revision",
                "asr_model_identity_sha256",
                "vad_backend",
                "vad_version",
            )
        }
    verification: dict[str, Any] = {
        "status": (
            "MODEL_OBSERVATION_COMPLETE"
            if page.status == "COMPLETE" and page.source_kind == "CURRENT_FILESYSTEM_AUDIO"
            else "OBSERVATION_COMPLETE"
            if page.status == "COMPLETE"
            else page.status
        ),
        "source_sha256": page.source_sha256,
        "document_observation_digest": page.document_observation_digest,
    }
    facts: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    content_search = page.content_search
    if content_search is not None and content_search.status == "COMPLETE":
        for match in content_search.matches[:MAX_GROUNDED_EVIDENCE_FACTS]:
            item: Any = next(
                (
                    candidate
                    for index, candidate in enumerate(page.items, start=page.offset)
                    if index == match.item_index
                ),
                None,
            )
            if item is None:
                # Content matches are computed over the complete observation;
                # a paged document item page may not contain that item.  Keep
                # the parser-native location and excerpt without inventing a
                # missing normalized item.
                class _MatchItem:
                    kind = match.kind
                    location = match.location
                    parent = match.parent
                    text_or_value = match.excerpt

                item = _MatchItem()
            facts.append(
                _document_item_fact(
                    page,
                    match.item_index,
                    item,
                    query=content_search.query,
                    excerpt=match.excerpt,
                    match_count=match.match_count,
                    excerpt_truncated=match.excerpt_truncated,
                )
            )
        if content_search.has_more or content_search.matched_item_count > len(facts):
            omissions.append(
                {
                    "id": "omission:document_content_matches",
                    "reason_code": "PAGE_LIMIT",
                    "text": "Additional matching document items were omitted by the content page boundary.",
                    "next_offset": content_search.next_offset,
                    "limit": content_search.limit,
                    "anchor_ids": [
                        _citation_id(
                            "document-source",
                            {
                                "source_sha256": page.source_sha256,
                                "observation_digest": page.document_observation_digest,
                            },
                        )
                    ],
                }
            )
        if content_search.matched_item_count == 0:
            unknowns = [
                {
                    "id": "unknown:document_content:no_match",
                    "reason_code": "CONTENT_NO_MATCH",
                    "text": "No normalized document item matched the requested content query.",
                    "query": content_search.query,
                    "anchor_ids": [],
                }
            ]
        else:
            unknowns = []
    else:
        for index, item in enumerate(page.items[:MAX_GROUNDED_EVIDENCE_FACTS], start=page.offset):
            facts.append(_document_item_fact(page, index, item))
        unknowns = []
        if page.status != "COMPLETE":
            unknowns.append(
                {
                    "id": "unknown:document_observation:status",
                    "reason_code": page.status,
                    "text": "The document observation did not complete; no authoritative content fact was inferred.",
                    "anchor_ids": [],
                }
            )
        if page.full_item_count > page.offset + len(page.items):
            omissions.append(
                {
                    "id": "omission:document_items",
                    "reason_code": "PAGE_LIMIT",
                    "text": "Additional normalized document items were omitted by the document page boundary.",
                    "next_offset": page.offset + len(page.items),
                    "limit": page.limit,
                    "anchor_ids": [],
                }
            )
    if len(facts) < len(page.items) and content_search is None:
        omissions.append(
            {
                "id": "omission:grounded_packet_facts",
                "reason_code": "PACKET_FACT_LIMIT",
                "text": "The grounded packet retained only its bounded fact budget.",
                "next_offset": page.offset + len(facts),
                "limit": MAX_GROUNDED_EVIDENCE_FACTS,
                "anchor_ids": [],
            }
        )
    bounds: dict[str, Any] = {
        "document_page": {
            "limit": page.limit,
            "offset": page.offset,
            "returned_count": page.returned_count,
            "has_more": page.has_more,
            "next_offset": page.next_offset,
        },
        "fact_limit": MAX_GROUNDED_EVIDENCE_FACTS,
        "text_limit_chars": MAX_GROUNDED_EVIDENCE_TEXT_CHARS,
    }
    if content_search is not None:
        bounds["content_search"] = {
            "query": content_search.query,
            "match_mode": content_search.match_mode,
            "limit": content_search.limit,
            "offset": content_search.offset,
            "matched_item_count": content_search.matched_item_count,
            "matched_occurrence_count": content_search.matched_occurrence_count,
            "returned_count": content_search.returned_count,
            "has_more": content_search.has_more,
            "next_offset": content_search.next_offset,
        }
    if page.continuation is not None:
        media_kind = (
            page.resources.media.get("media_kind")
            if isinstance(page.resources.media, dict)
            else None
        )
        continuation_kind = "video" if media_kind == "VIDEO" else "audio"
        bounds[f"{continuation_kind}_continuation"] = page.continuation
        result_page = page.continuation.get("kind") == "RESULT_PAGE"
        omissions.append(
            {
                "id": (
                    f"omission:{continuation_kind}_result_page"
                    if result_page
                    else f"omission:{continuation_kind}_remaining_timeline"
                ),
                "reason_code": (
                    f"{continuation_kind.upper()}_RESULT_PAGE_LIMIT"
                    if result_page
                    else f"{continuation_kind.upper()}_WINDOW_LIMIT"
                ),
                "text": (
                    f"Additional cached {continuation_kind} results remain in the current inference window."
                    if result_page
                    else f"Additional source time remains outside this bounded {continuation_kind} inference window."
                ),
                "continuation": page.continuation,
                "anchor_ids": [],
            }
        )
    return _finalize(
        "CURRENT_DOCUMENT",
        source,
        verification,
        facts,
        [],
        unknowns,
        omissions,
        bounds,
    )


def _compact_execution(
    value: dict[str, Any] | None,
    *,
    projection: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    attempts: list[dict[str, Any]] = []
    for attempt in _dict_items(value.get("attempts")):
        quality = attempt.get("quality")
        quality_summary = (
            {
                key: quality.get(key)
                for key in (
                    "status",
                    "reason_codes",
                    "text_characters",
                    "text_item_count",
                    "structural_role_count",
                    "page_count",
                    "empty_page_count",
                )
            }
            if isinstance(quality, dict)
            else None
        )
        attempts.append(
            {key: attempt.get(key) for key in ("profile", "status", "backend_name", "cache_status")}
            | {"quality": quality_summary}
        )
    selection = value.get("selection")
    selection_summary: dict[str, Any] | None = None
    if isinstance(selection, dict):
        matched_containers = selection.get("matched_container_ids")
        selected_containers = selection.get("selected_container_ids")
        omitted_containers = selection.get("omitted_matched_container_ids")
        matched_pages = selection.get("matched_page_numbers")
        omitted_pages = selection.get("omitted_matched_page_numbers")
        selection_summary = {
            key: selection.get(key)
            for key in (
                "strategy",
                "map_profile",
                "mapped_item_count",
                "matched_item_count",
                "match_mode",
                "selected_page_start",
                "selected_page_end",
            )
        }
        selection_summary.update(
            {
                "matched_container_count": len(matched_containers)
                if isinstance(matched_containers, list)
                else 0,
                "selected_container_count": len(selected_containers)
                if isinstance(selected_containers, list)
                else 0,
                "omitted_container_count": len(omitted_containers)
                if isinstance(omitted_containers, list)
                else 0,
                "matched_page_count": len(matched_pages) if isinstance(matched_pages, list) else 0,
                "omitted_page_count": len(omitted_pages) if isinstance(omitted_pages, list) else 0,
            }
        )
    return {
        "schema_name": value.get("schema_name"),
        "schema_version": value.get("schema_version"),
        "projection": projection,
        "requested_profile": value.get("requested_profile"),
        "requested_intent": value.get("requested_intent"),
        "requested_view": value.get("requested_view"),
        "initial_profile": value.get("initial_profile"),
        "selected_profile": value.get("selected_profile"),
        "escalation_reason": value.get("escalation_reason"),
        "persistence_effect": value.get("persistence_effect"),
        "reuse_scope": value.get("reuse_scope"),
        "attempts": attempts,
        "selection": selection_summary,
    }


def _build_selected_document_evidence_packet(
    page: DocumentInspectionPage,
    *,
    compact_execution: bool,
    execution_projection: str,
) -> dict[str, Any]:
    """Publish one compact query-selected document evidence packet."""

    selection = page.evidence_selection
    if selection is None:  # pragma: no cover - guarded by the public builder
        raise ValueError("document evidence selection is unavailable")
    source: dict[str, Any] = {
        "source_kind": page.source_kind,
        "scope_id": page.scope_id,
        "relative_path": page.relative_path,
        "source_sha256": page.source_sha256,
        "document_observation_digest": page.document_observation_digest,
        "source_format": page.source_format,
        "backend_name": page.backend_name,
        "backend_version": page.backend_version,
    }
    if page.source_kind == "CURRENT_FILESYSTEM_AUDIO" and page.resources.media is not None:
        source["model"] = {
            key: page.resources.media.get(key)
            for key in (
                "asr_backend",
                "asr_version",
                "asr_model_id",
                "asr_model_revision",
                "asr_model_identity_sha256",
                "vad_backend",
                "vad_version",
            )
        }
    verification: dict[str, Any] = {
        "status": (
            "MODEL_OBSERVATION_COMPLETE"
            if page.status == "COMPLETE" and page.source_kind == "CURRENT_FILESYSTEM_AUDIO"
            else "OBSERVATION_COMPLETE"
            if page.status == "COMPLETE"
            else page.status
        ),
        "source_pinned": page.source_sha256 is not None,
        "source_sha256": page.source_sha256,
        "document_observation_digest": page.document_observation_digest,
        "selection_digest": selection.selection_digest,
    }
    facts: list[dict[str, Any]] = []
    fact_indexes: dict[str, int] = {}
    selected_citation_ids: set[str] = set()
    slices: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    for evidence_slice in selection.slices:
        slice_fact_indexes: list[int] = []
        anchor_citation_id: str | None = None
        anchor_fact_index: int | None = None
        for item in evidence_slice.items:
            citation_id = _citation_id(
                "document-evidence",
                {
                    "source_sha256": page.source_sha256,
                    "selection_digest": selection.selection_digest,
                    "item_index": item.item_index,
                    "node_id": item.node_id,
                    "location": item.location,
                    "native_location": item.native_location,
                },
            )
            selected_citation_ids.add(citation_id)
            if citation_id in fact_indexes:
                fact_index = fact_indexes[citation_id]
                if fact_index not in slice_fact_indexes:
                    slice_fact_indexes.append(fact_index)
                if item.relation == "ANCHOR":
                    anchor_citation_id = citation_id
                    anchor_fact_index = fact_index
                continue
            if len(facts) >= MAX_GROUNDED_EVIDENCE_FACTS:
                continue
            fact: dict[str, Any] = {
                "citation_id": citation_id,
                "role": item.role,
                "node_id": item.node_id,
                "native_location": item.native_location,
                "value": item.text,
                "value_truncated": item.text_truncated,
            }
            if page.source_kind == "CURRENT_FILESYSTEM_AUDIO" and item.kind.startswith(
                ("audio_transcript", "audio_speech")
            ):
                fact.update(
                    {
                        "authority": "MODEL_DERIVED",
                        "model_derived": True,
                        "timestamp_accuracy": "MODEL_APPROXIMATE",
                        "model": source.get("model"),
                    }
                )
            if item.kind.startswith("pdf_ocr_"):
                ocr_model = page.resources.media
                fact.update(
                    {
                        "authority": "MODEL_DERIVED",
                        "model_derived": True,
                        "text_accuracy": "OCR_MODEL_APPROXIMATE",
                        "model": {
                            "backend_name": (
                                ocr_model.get("ocr_backend")
                                if isinstance(ocr_model, dict)
                                else page.backend_name
                            ),
                            "backend_version": (
                                ocr_model.get("ocr_version")
                                if isinstance(ocr_model, dict)
                                else page.backend_version
                            ),
                        },
                    }
                )
            if item.native_location is None:
                fact["location"] = item.location
            fact_index = len(facts)
            facts.append(fact)
            fact_indexes[citation_id] = fact_index
            slice_fact_indexes.append(fact_index)
            if item.relation == "ANCHOR":
                anchor_citation_id = citation_id
                anchor_fact_index = fact_index
        slices.append(
            {
                "slice_id": evidence_slice.slice_id,
                "anchor_item_index": evidence_slice.anchor_item_index,
                "anchor_node_id": evidence_slice.anchor_node_id,
                "selection_mode": evidence_slice.selection_mode,
                "heading_trail": list(evidence_slice.heading_trail),
                "page_numbers": list(evidence_slice.page_numbers),
                "anchor_citation_id": anchor_citation_id,
                "anchor_fact_index": anchor_fact_index,
                "fact_indexes": slice_fact_indexes,
                "selected_character_count": evidence_slice.selected_character_count,
                "omitted_item_count": evidence_slice.omitted_item_count,
                "truncated": evidence_slice.truncated,
            }
        )
        if evidence_slice.truncated:
            omissions.append(
                {
                    "id": f"omission:{evidence_slice.slice_id}:budget",
                    "reason_code": "SLICE_BUDGET",
                    "text": "The evidence slice was bounded before every structural item could be published.",
                    "anchor_ids": [anchor_citation_id] if anchor_citation_id is not None else [],
                }
            )
    if len(facts) < len(selected_citation_ids):
        omissions.append(
            {
                "id": "omission:document_evidence_facts",
                "reason_code": "PACKET_FACT_LIMIT",
                "text": "The packet retained only its bounded evidence fact budget.",
                "anchor_ids": [],
            }
        )
    if selection.has_more:
        omissions.append(
            {
                "id": "omission:document_evidence_matches",
                "reason_code": "MATCH_PAGE_LIMIT",
                "text": "Additional deterministic query anchors remain available.",
                "next_offset": selection.next_offset,
                "anchor_ids": [],
            }
        )
    if page.continuation is not None:
        omissions.append(
            {
                "id": "omission:audio_remaining_timeline",
                "reason_code": "AUDIO_WINDOW_LIMIT",
                "text": "The query was evaluated only over the published bounded audio window; more source time remains.",
                "continuation": page.continuation,
                "anchor_ids": [],
            }
        )
    execution = page.execution.payload() if page.execution is not None else None
    if compact_execution:
        execution = _compact_execution(execution, projection=execution_projection)
    next_target_page: int | None = None
    if page.execution is not None and page.execution.selection is not None:
        omitted_pages = page.execution.selection.omitted_matched_page_numbers
        if omitted_pages:
            next_target_page = omitted_pages[0]
            omissions.append(
                {
                    "id": "omission:targeted_parse_pages",
                    "reason_code": "TARGET_PAGE_LIMIT",
                    "text": "Some globally matched pages were outside the bounded targeted parser interval.",
                    "page_numbers": list(omitted_pages),
                    "anchor_ids": [],
                }
            )
        omitted_containers = page.execution.selection.omitted_matched_container_ids
        if omitted_containers:
            omissions.append(
                {
                    "id": "omission:targeted_parse_containers",
                    "reason_code": "TARGET_CONTAINER_LIMIT",
                    "text": "Some matched native containers were outside the bounded parser selection.",
                    "container_ids": list(omitted_containers),
                    "anchor_ids": [],
                }
            )
    unknowns: list[dict[str, Any]] = []
    if selection.status == "NO_MATCH":
        unknowns.append(
            {
                "id": "unknown:document_evidence:no_match",
                "reason_code": "CONTENT_NO_MATCH",
                "text": "No normalized document item matched the requested query.",
                "query": selection.query,
                "anchor_ids": [],
            }
        )
    elif selection.status != "COMPLETE":
        unknowns.append(
            {
                "id": "unknown:document_evidence:status",
                "reason_code": selection.status,
                "text": "The document was not searchable, so no evidence slice was published.",
                "anchor_ids": [],
            }
        )
    continuation: dict[str, Any] | None = (
        {
            "action": "EVIDENCE",
            "expected_source_sha256": page.source_sha256,
            "content_query": selection.query,
            "content_offset": selection.next_offset if selection.has_more else 0,
            "content_limit": selection.limit,
            "evidence_mode": selection.requested_mode,
            "evidence_context_items": selection.context_items,
            "evidence_max_characters": selection.max_characters,
            **({"evidence_page": next_target_page} if next_target_page is not None else {}),
        }
        if selection.has_more or next_target_page is not None
        else None
    )
    if page.continuation is not None:
        continuation = {
            "action": "EVIDENCE",
            "expected_source_sha256": page.source_sha256,
            "content_query": selection.query,
            "audio_continuation": page.continuation,
        }
    packet: dict[str, Any] = {
        "schema_name": DOCUMENT_EVIDENCE_PACKET_SCHEMA_NAME,
        "schema_version": DOCUMENT_EVIDENCE_PACKET_SCHEMA_VERSION,
        "packet_kind": "CURRENT_DOCUMENT_EVIDENCE",
        "packet_status": "READY" if facts else "NO_EVIDENCE",
        "source": source,
        "verification": verification,
        "question": {"content_query": selection.query},
        "selection": {
            "strategy": "QUERY_MAP_THEN_BOUNDED_GRAPH_SELECTION",
            "match_mode": selection.match_mode,
            "requested_mode": selection.requested_mode,
            "matched_item_count": selection.matched_item_count,
            "matched_occurrence_count": selection.matched_occurrence_count,
            "returned_slice_count": selection.returned_slice_count,
            "selection_digest": selection.selection_digest,
        },
        "slices": slices,
        "facts": facts,
        "unknowns": unknowns,
        "omissions": omissions,
        "execution": execution,
        "bounds": {
            "parsed_item_count": selection.parsed_item_count,
            "selected_item_count": selection.selected_item_count,
            "selected_character_count": selection.selected_character_count,
            "published_fact_count": len(facts),
            "deduplicated_reference_count": max(
                0,
                sum(len(item.items) for item in selection.slices) - len(selected_citation_ids),
            ),
            "max_characters": selection.max_characters,
            "context_items": selection.context_items,
            "slice_limit": selection.limit,
            "fact_limit": MAX_GROUNDED_EVIDENCE_FACTS,
        },
        "continuation": continuation,
        "delivery": {
            "response_contract": "DOCUMENT_EVIDENCE_V3",
            "citation_required": bool(facts),
            "required_answer_fields": [
                "source",
                "verification",
                "citation_id",
                "unknowns",
                "omissions",
            ],
            "interpretation_is_not_evidence": True,
            "unknowns_and_omissions_must_be_preserved": True,
            "model_output_must_not_be_described_as_verbatim": (
                page.source_kind == "CURRENT_FILESYSTEM_AUDIO"
                or any(fact.get("model_derived") is True for fact in facts)
            ),
        },
    }
    packet["packet_digest"] = _document_packet_digest(packet)
    return packet


def build_historical_evidence_packet(
    projection: dict[str, object],
    *,
    routing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap one verified Context Projection without changing its facts."""
    source_value = projection.get("source")
    if not isinstance(source_value, dict):
        raise ValueError("Context Projection source is unavailable")
    source_keys = (
        "scope_id",
        "snapshot_id",
        "snapshot_digest",
        "snapshot_ids",
        "snapshot_digests",
        "run_ids",
        "evidence_ids",
        "verification_status",
        "base_snapshot_id",
        "target_snapshot_id",
    )
    source: dict[str, Any] = {
        "source_kind": "HISTORICAL_SNAPSHOT",
        **{key: source_value[key] for key in source_keys if key in source_value},
    }
    verification: dict[str, Any] = {
        "status": source.get("verification_status"),
        "snapshot_ids": source.get("snapshot_ids", []),
        "snapshot_digests": source.get("snapshot_digests", []),
        "run_ids": source.get("run_ids", []),
        "evidence_ids": source.get("evidence_ids", []),
    }
    facts: list[dict[str, Any]] = []
    for fact_kind, key in (
        ("OBSERVED_FACT", "observed_facts"),
        ("DERIVED_METRIC", "derived_metrics"),
    ):
        values = projection.get(key, [])
        if not isinstance(values, list):
            continue
        for raw in values[: MAX_GROUNDED_EVIDENCE_FACTS - len(facts)]:
            if not isinstance(raw, dict):
                continue
            fact = dict(raw)
            identifier = fact.get("id")
            if not isinstance(identifier, str) or not identifier:
                identifier = _citation_id("historical", fact)
            fact["citation_id"] = identifier
            fact["fact_kind"] = fact_kind
            facts.append(fact)
    unknowns = _dict_items(projection.get("unknowns", []))
    omissions = _dict_items(projection.get("omissions", []))
    source_fact_count = sum(
        len(value)
        for key in ("observed_facts", "derived_metrics")
        if isinstance(value := projection.get(key), list)
    )
    if len(facts) < source_fact_count:
        omissions.append(
            {
                "id": "omission:grounded_packet_facts",
                "reason_code": "PACKET_FACT_LIMIT",
                "text": "The grounded packet retained only its bounded fact budget.",
                "anchor_ids": [],
            }
        )
    interpretations = _dict_items(projection.get("interpretations", []))
    bounds = {
        "projection_kind": projection.get("projection_kind"),
        "projection_digest": projection.get("context_projection_digest"),
        "continuation": projection.get("continuation"),
        "fact_limit": MAX_GROUNDED_EVIDENCE_FACTS,
        "text_limit_chars": MAX_GROUNDED_EVIDENCE_TEXT_CHARS,
    }
    return _finalize(
        "HISTORICAL_SNAPSHOT",
        source,
        verification,
        facts,
        interpretations,
        unknowns,
        omissions,
        bounds,
        routing=routing,
    )


__all__ = [
    "DOCUMENT_EVIDENCE_PACKET_DIGEST_DOMAIN",
    "DOCUMENT_EVIDENCE_PACKET_SCHEMA_NAME",
    "DOCUMENT_EVIDENCE_PACKET_SCHEMA_VERSION",
    "GROUNDED_EVIDENCE_PACKET_DIGEST_DOMAIN",
    "GROUNDED_EVIDENCE_PACKET_SCHEMA_NAME",
    "GROUNDED_EVIDENCE_PACKET_SCHEMA_VERSION",
    "MAX_GROUNDED_EVIDENCE_FACTS",
    "MAX_GROUNDED_EVIDENCE_TEXT_CHARS",
    "build_document_evidence_packet",
    "build_historical_evidence_packet",
]
