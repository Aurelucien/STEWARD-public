"""Compact, deterministic and non-persistent Context Projection v1.

The projection is an additive 0.6.0 presentation layer.  It consumes verified
Snapshot facts, never writes them, and deliberately keeps the existing Agent
Context Pack path separate so callers without an explicit profile remain
byte-compatible with the 0.5.x result shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from collections.abc import Callable
from typing import Any

from ..evidence import canonical_json
from ..errors import SnapshotNotFoundError, SnapshotScopeError, StewardError
from ..models import (
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemObjectType,
    FilesystemSnapshot,
    FilesystemSnapshotV2,
    SnapshotVerificationResult,
    StewardConfig,
)
from ..snapshots import _verified_snapshot_detail
from ..snapshot_refresh import SnapshotChangeReviewRequest, review_snapshot_changes
from .errors import (
    ContextProjectionContinuationMismatchError,
    ContextProjectionError,
    ContextProjectionResourceError,
    ContextProjectionUnsupportedProfileError,
)


CONTEXT_PROJECTION_SCHEMA_NAME = "local_steward.context_projection"
CONTEXT_PROJECTION_SCHEMA_VERSION = 1
CONTEXT_PROJECTION_ALGORITHM = "context_projection"
CONTEXT_PROJECTION_ALGORITHM_VERSION = 1
CONTEXT_PROJECTION_DIGEST_DOMAIN = "local_steward.context_projection.v1"
MAX_CONTEXT_PROJECTION_BYTES = 100_000
MAX_CONTEXT_PROJECTION_LIMIT = 1_000


class ContextProjectionProfile(str, Enum):
    GENERAL = "GENERAL"
    STRUCTURE_OVERVIEW = "STRUCTURE_OVERVIEW"
    CHANGE_TRIAGE = "CHANGE_TRIAGE"
    STORAGE_HOTSPOTS = "STORAGE_HOTSPOTS"
    PROJECT_CLUSTERS = "PROJECT_CLUSTERS"


@dataclass(frozen=True, slots=True)
class ContextProjectionRequest:
    profile: str
    snapshot_id: str
    scope_id: str | None = None
    path_prefix: str | None = None
    limit: int = 100
    offset: int = 0
    question: str | None = None
    base_snapshot_id: str | None = None
    continuation_digest: str | None = None
    continuation_offset: int | None = None


def _canonical(value: object) -> bytes:
    return canonical_json(value)


def _digest(value: object) -> str:
    return sha256(
        CONTEXT_PROJECTION_DIGEST_DOMAIN.encode("utf-8") + b"\0" + _canonical(value)
    ).hexdigest()


def _location_key(scope_id: str, relative_path: str) -> tuple[str, bytes]:
    return scope_id, relative_path.encode("utf-8", "surrogateescape")


def _path_selected(relative_path: str, path_prefix: str | None) -> bool:
    if path_prefix is None or path_prefix == ".":
        return True
    return relative_path == path_prefix or relative_path.startswith(path_prefix + "/")


def _validate_request(request: ContextProjectionRequest) -> ContextProjectionProfile:
    try:
        profile = ContextProjectionProfile(request.profile)
    except (TypeError, ValueError) as error:
        raise ContextProjectionUnsupportedProfileError(
            "the requested Context Projection profile is unsupported"
        ) from error
    if profile in {
        ContextProjectionProfile.STORAGE_HOTSPOTS,
        ContextProjectionProfile.PROJECT_CLUSTERS,
    }:
        raise ContextProjectionUnsupportedProfileError(
            "the requested Context Projection profile is not enabled in 0.6.0-R0"
        )
    if not isinstance(request.snapshot_id, str) or not request.snapshot_id:
        raise ContextProjectionError("Context Projection Snapshot identity is invalid")
    if type(request.limit) is not int or not 1 <= request.limit <= MAX_CONTEXT_PROJECTION_LIMIT:
        raise ContextProjectionError("Context Projection limit is invalid")
    if type(request.offset) is not int or request.offset < 0:
        raise ContextProjectionError("Context Projection offset is invalid")
    if request.scope_id is not None and (
        not isinstance(request.scope_id, str) or not request.scope_id
    ):
        raise ContextProjectionError("Context Projection Scope identity is invalid")
    if request.path_prefix is not None and (
        not isinstance(request.path_prefix, str)
        or not request.path_prefix
        or request.path_prefix.startswith("/")
        or request.path_prefix == ".."
        or request.path_prefix.startswith("../")
        or "/../" in request.path_prefix
    ):
        raise ContextProjectionError("Context Projection path prefix is invalid")
    if request.continuation_digest is not None and (
        not isinstance(request.continuation_digest, str)
        or len(request.continuation_digest) != 64
        or any(character not in "0123456789abcdef" for character in request.continuation_digest)
    ):
        raise ContextProjectionContinuationMismatchError(
            "Context Projection continuation digest is invalid"
        )
    if request.continuation_offset is not None and (
        type(request.continuation_offset) is not int or request.continuation_offset < 0
    ):
        raise ContextProjectionContinuationMismatchError(
            "Context Projection continuation offset is invalid"
        )
    return profile


def _snapshot_or_error(
    config: StewardConfig, snapshot_id: str
) -> tuple[SnapshotVerificationResult, FilesystemSnapshot | FilesystemSnapshotV2]:
    try:
        verification, snapshot = _verified_snapshot_detail(config, snapshot_id)
    except SnapshotNotFoundError as error:
        raise ContextProjectionError(
            "the requested Snapshot does not exist", cause_code="SNAPSHOT_NOT_FOUND"
        ) from error
    except StewardError:
        raise
    except Exception as error:
        raise ContextProjectionError(
            "the historical Snapshot source is unavailable",
            cause_code="HISTORICAL_SOURCE_UNAVAILABLE",
        ) from error
    if verification.status != "VALID":
        raise ContextProjectionError(
            "Context Projection requires an authoritatively VALID Snapshot",
            cause_code="SNAPSHOT_NOT_VALID",
        )
    return verification, snapshot


def _source(
    snapshots: tuple[tuple[SnapshotVerificationResult, FilesystemSnapshot | FilesystemSnapshotV2], ...],
    scope_id: str | None,
) -> dict[str, Any]:
    first = snapshots[0][1]
    result: dict[str, Any] = {
        "scope_id": scope_id,
        "snapshot_id": first.snapshot_id,
        "snapshot_digest": first.snapshot_digest,
        "snapshot_ids": [item.snapshot_id for _, item in snapshots],
        "snapshot_digests": [item.snapshot_digest for _, item in snapshots],
        "run_ids": [item.run_id for _, item in snapshots],
        "evidence_ids": [item.evidence_id for _, item in snapshots],
        "evidence_relative_paths": [item.evidence_relative_path for _, item in snapshots],
        "verification_status": "VALID",
    }
    if len(snapshots) == 2:
        result["base_snapshot_id"] = snapshots[0][1].snapshot_id
        result["target_snapshot_id"] = snapshots[1][1].snapshot_id
    return result


def _snapshot_anchor(
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
) -> dict[str, Any]:
    return {
        "id": f"snapshot:{snapshot.snapshot_id}",
        "kind": "SNAPSHOT",
        "snapshot_id": snapshot.snapshot_id,
        "run_id": snapshot.run_id,
        "evidence_id": snapshot.evidence_id,
        "snapshot_digest": snapshot.snapshot_digest,
        "evidence_relative_path": snapshot.evidence_relative_path,
    }


def _entry_anchor(
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
    entry: FilesystemEntry | FilesystemEntryV2,
) -> dict[str, Any]:
    return {
        "id": f"entry:{snapshot.snapshot_id}:{entry.scope_id}:{entry.relative_path}",
        "kind": "ENTRY",
        "snapshot_id": snapshot.snapshot_id,
        "run_id": snapshot.run_id,
        "evidence_id": snapshot.evidence_id,
        "snapshot_digest": snapshot.snapshot_digest,
        "scope_id": entry.scope_id,
        "relative_path": entry.relative_path,
        "entry_id": entry.entry_id,
    }


def _fact(
    identifier: str,
    text: str,
    object_kind: str,
    location: dict[str, Any],
    anchors: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "id": identifier,
        "text": text,
        "object_kind": object_kind,
        "location": location,
        "anchor_ids": list(anchors),
    }


def _metric(
    identifier: str,
    name: str,
    value: int,
    unit: str,
    method: str,
    input_range: dict[str, Any],
    anchors: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "id": identifier,
        "name": name,
        "value": value,
        "unit": unit,
        "method": method,
        "input_range": input_range,
        "anchor_ids": list(anchors),
    }


def _unknown(identifier: str, reason_code: str, text: str, anchors: tuple[str, ...]) -> dict[str, Any]:
    return {
        "id": identifier,
        "reason_code": reason_code,
        "text": text,
        "anchor_ids": list(anchors),
    }


def _binding(
    profile: ContextProjectionProfile,
    snapshots: tuple[tuple[SnapshotVerificationResult, FilesystemSnapshot | FilesystemSnapshotV2], ...],
    request: ContextProjectionRequest,
) -> dict[str, Any]:
    return {
        "algorithm": CONTEXT_PROJECTION_ALGORITHM,
        "algorithm_version": CONTEXT_PROJECTION_ALGORITHM_VERSION,
        "profile": profile.value,
        "snapshot_ids": [item.snapshot_id for _, item in snapshots],
        "snapshot_digests": [item.snapshot_digest for _, item in snapshots],
        "scope_id": request.scope_id,
        "path_prefix": request.path_prefix,
        "limit": request.limit,
        "order": "scope_id,relative_path:utf8-surrogateescape",
    }


def _validate_continuation(binding: dict[str, Any], request: ContextProjectionRequest) -> str:
    digest = _digest(binding)
    if request.continuation_digest is not None and request.continuation_digest != digest:
        raise ContextProjectionContinuationMismatchError(
            "Context Projection continuation does not match the source or bounds"
        )
    if request.continuation_offset is not None and request.continuation_offset != request.offset:
        raise ContextProjectionContinuationMismatchError(
            "Context Projection continuation offset does not match the request"
        )
    return digest


def _summary_facts(
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
    verification: SnapshotVerificationResult,
    anchor_id: str,
) -> tuple[dict[str, Any], ...]:
    location = {"scope_ids": list(snapshot.scope_ids)}
    return (
        _fact(
            f"snapshot:{snapshot.snapshot_id}:status",
            f"Snapshot status is {snapshot.status.value} with {snapshot.entry_count} recorded entries.",
            "SNAPSHOT",
            location,
            (anchor_id,),
        ),
        _fact(
            f"snapshot:{snapshot.snapshot_id}:verification",
            f"Snapshot verification status is {verification.status}.",
            "VERIFICATION",
            location,
            (anchor_id,),
        ),
        _fact(
            f"snapshot:{snapshot.snapshot_id}:consistency",
            f"Snapshot consistency is {snapshot.consistency.value}.",
            "SNAPSHOT",
            location,
            (anchor_id,),
        ),
    )


def _summary_metrics(
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
    anchor_id: str,
) -> tuple[dict[str, Any], ...]:
    input_range = {"snapshot_id": snapshot.snapshot_id}
    return (
        _metric(
            f"metric:{snapshot.snapshot_id}:entry_count",
            "entry_count",
            snapshot.entry_count,
            "entries",
            "snapshot.summary.entry_count",
            input_range,
            (anchor_id,),
        ),
        _metric(
            f"metric:{snapshot.snapshot_id}:observed_count",
            "observed_count",
            snapshot.observed_count,
            "entries",
            "snapshot.summary.observed_count",
            input_range,
            (anchor_id,),
        ),
        _metric(
            f"metric:{snapshot.snapshot_id}:logical_bytes",
            "total_regular_file_bytes",
            snapshot.total_regular_file_bytes,
            "logical_bytes",
            "snapshot.summary.total_regular_file_bytes",
            input_range,
            (anchor_id,),
        ),
        _metric(
            f"metric:{snapshot.snapshot_id}:max_depth",
            "max_depth_observed",
            snapshot.max_depth_observed,
            "path_components",
            "snapshot.summary.max_depth_observed",
            input_range,
            (anchor_id,),
        ),
    )


def _filtered_entries(
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
    request: ContextProjectionRequest,
) -> tuple[FilesystemEntry | FilesystemEntryV2, ...]:
    if request.scope_id is not None and request.scope_id not in snapshot.scope_ids:
        raise SnapshotScopeError(
            f"unknown historical scope_id for Snapshot {snapshot.snapshot_id}: {request.scope_id}"
        )
    return tuple(
        sorted(
            (
                entry
                for entry in snapshot.entries
                if (request.scope_id is None or entry.scope_id == request.scope_id)
                and _path_selected(entry.relative_path, request.path_prefix)
            ),
            key=lambda entry: _location_key(entry.scope_id, entry.relative_path),
        )
    )


def _entry_facts(
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
    entries: tuple[FilesystemEntry | FilesystemEntryV2, ...],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    facts: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    for entry in entries:
        anchor = _entry_anchor(snapshot, entry)
        anchors.append(anchor)
        size = "unknown" if entry.size_bytes is None else str(entry.size_bytes)
        facts.append(
            _fact(
                anchor["id"],
                f"{entry.relative_path} is a {entry.object_type.value} with logical size {size} bytes.",
                entry.object_type.value.upper(),
                {"scope_id": entry.scope_id, "relative_path": entry.relative_path},
                (anchor["id"],),
            )
        )
    return tuple(facts), tuple(anchors)


def _directory_nodes(
    entries: tuple[FilesystemEntry | FilesystemEntryV2, ...],
) -> tuple[dict[str, Any], ...]:
    nodes: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        parts = [] if entry.relative_path == "." else entry.relative_path.split("/")
        paths = ["."]
        paths.extend("/".join(parts[:index]) for index in range(1, len(parts)))
        for path in paths:
            node = nodes.setdefault(
                (entry.scope_id, path),
                {
                    "scope_id": entry.scope_id,
                    "relative_path": path,
                    "entry_count": 0,
                    "regular_file_count": 0,
                    "logical_bytes": 0,
                    "unknown_size_count": 0,
                },
            )
            node["entry_count"] += 1
            if entry.object_type == FilesystemObjectType.REGULAR_FILE:
                node["regular_file_count"] += 1
                if entry.size_bytes is None:
                    node["unknown_size_count"] += 1
                else:
                    node["logical_bytes"] += entry.size_bytes
    return tuple(
        sorted(nodes.values(), key=lambda value: _location_key(value["scope_id"], value["relative_path"]))
    )


def _page_omission(
    category: str,
    count: int,
    next_offset: int,
    limit: int,
    anchor_ids: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "id": f"omission:{category.lower()}",
        "category": category,
        "count": count,
        "reason_code": "PAGE_LIMIT",
        "text": f"{count} {category.lower()} omitted by the requested page boundary.",
        "next_query": {"offset": next_offset, "limit": limit},
        "anchor_ids": list(anchor_ids),
    }


def _base_result(
    profile: ContextProjectionProfile,
    source: dict[str, Any],
    facts: tuple[dict[str, Any], ...],
    metrics: tuple[dict[str, Any], ...],
    interpretations: tuple[dict[str, Any], ...],
    unknowns: tuple[dict[str, Any], ...],
    omissions: tuple[dict[str, Any], ...],
    anchors: tuple[dict[str, Any], ...],
    continuation: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "projection_schema_name": CONTEXT_PROJECTION_SCHEMA_NAME,
        "projection_schema_version": CONTEXT_PROJECTION_SCHEMA_VERSION,
        "projection_algorithm": CONTEXT_PROJECTION_ALGORITHM,
        "projection_algorithm_version": CONTEXT_PROJECTION_ALGORITHM_VERSION,
        "projection_kind": profile.value,
        "source": source,
        "observed_facts": list(facts),
        "derived_metrics": list(metrics),
        "interpretations": list(interpretations),
        "unknowns": list(unknowns),
        "omissions": list(omissions),
        "evidence_anchors": list(anchors),
        "continuation": continuation,
        "projection_status": status,
    }
    result["context_projection_digest"] = _digest(result)
    return result


def _fit_page(
    builder: Callable[[tuple[Any, ...]], dict[str, Any]],
    page_values: tuple[Any, ...],
) -> dict[str, Any]:
    """Build a result and shrink only page values if the soft cap is exceeded."""
    current = page_values
    while True:
        result = builder(current)
        if len(_canonical(result)) <= MAX_CONTEXT_PROJECTION_BYTES:
            return result
        if not current:
            raise ContextProjectionResourceError(
                "Context Projection envelope exceeds the bounded serialization limit"
            )
        current = current[: max(0, len(current) // 2)]


def _general_or_structure(
    profile: ContextProjectionProfile,
    snapshot: FilesystemSnapshot | FilesystemSnapshotV2,
    verification: SnapshotVerificationResult,
    request: ContextProjectionRequest,
    binding_digest: str,
) -> dict[str, Any]:
    snapshot_anchor = _snapshot_anchor(snapshot)
    anchor_id = snapshot_anchor["id"]
    base_facts = _summary_facts(snapshot, verification, anchor_id)
    metrics = _summary_metrics(snapshot, anchor_id)
    unknowns: list[dict[str, Any]] = []
    if not isinstance(snapshot, FilesystemSnapshotV2):
        unknowns.append(
            _unknown(
                f"unknown:{snapshot.snapshot_id}:payload",
                "PAYLOAD_NOT_AVAILABLE",
                "Payload observations are not available in this Snapshot schema.",
                (anchor_id,),
            )
        )
    if snapshot.error_count or snapshot.excluded_count:
        unknowns.append(
            _unknown(
                f"unknown:{snapshot.snapshot_id}:observation_coverage",
                "OBSERVATION_COVERAGE_PARTIAL",
                "Some recorded entries have observation errors or exclusions.",
                (anchor_id,),
            )
        )
    if profile == ContextProjectionProfile.GENERAL:
        values = _filtered_entries(snapshot, request)
        page = values[request.offset : request.offset + request.limit]

        def build(current: tuple[FilesystemEntry | FilesystemEntryV2, ...]) -> dict[str, Any]:
            entry_facts, entry_anchors = _entry_facts(snapshot, current)
            omitted = len(values) - request.offset - len(current)
            omissions = () if omitted <= 0 else (_page_omission("ENTRIES", omitted, request.offset + len(current), request.limit, (anchor_id,)),)
            continuation = {
                "has_more": omitted > 0,
                "offset": request.offset,
                "limit": request.limit,
                "next_offset": request.offset + len(current) if omitted > 0 else None,
                "request_digest": binding_digest,
                "order": "scope_id,relative_path:utf8-surrogateescape",
            }
            return _base_result(
                profile,
                _source(((verification, snapshot),), request.scope_id),
                base_facts + entry_facts,
                metrics,
                (),
                tuple(unknowns),
                omissions,
                (snapshot_anchor, *entry_anchors),
                continuation,
                "PARTIAL" if omissions else "COMPLETE",
            )

        return _fit_page(build, page)

    directory_values = _directory_nodes(_filtered_entries(snapshot, request))
    directory_page = directory_values[request.offset : request.offset + request.limit]

    def build_structure(current: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        facts = list(base_facts)
        anchors: list[dict[str, Any]] = [snapshot_anchor]
        for node in current:
            identifier = f"directory:{snapshot.snapshot_id}:{node['scope_id']}:{node['relative_path']}"
            facts.append(
                _fact(
                    identifier,
                    f"{node['relative_path']} contains {node['entry_count']} entries and {node['logical_bytes']} known logical bytes.",
                    "DIRECTORY",
                    {"scope_id": node["scope_id"], "relative_path": node["relative_path"]},
                    (anchor_id,),
                )
            )
        omitted = len(directory_values) - request.offset - len(current)
        omissions = () if omitted <= 0 else (_page_omission("DIRECTORIES", omitted, request.offset + len(current), request.limit, (anchor_id,)),)
        continuation = {
            "has_more": omitted > 0,
            "offset": request.offset,
            "limit": request.limit,
            "next_offset": request.offset + len(current) if omitted > 0 else None,
            "request_digest": binding_digest,
            "order": "scope_id,relative_path:utf8-surrogateescape",
        }
        return _base_result(
            profile,
            _source(((verification, snapshot),), request.scope_id),
            tuple(facts),
            metrics,
            (),
            tuple(unknowns),
            omissions,
            tuple(anchors),
            continuation,
            "PARTIAL" if omissions else "COMPLETE",
        )

    return _fit_page(build_structure, directory_page)


def _change_triage(
    config: StewardConfig,
    request: ContextProjectionRequest,
    binding_digest: str,
) -> dict[str, Any]:
    if request.base_snapshot_id is None:
        raise ContextProjectionError(
            "CHANGE_TRIAGE requires an explicit base Snapshot", cause_code="BASE_SELECTOR_REQUIRED"
        )
    review = review_snapshot_changes(
        config,
        SnapshotChangeReviewRequest(
            request.base_snapshot_id,
            request.snapshot_id,
            limit=MAX_CONTEXT_PROJECTION_LIMIT,
            offset=0,
        ),
    )
    if review.has_more:
        raise ContextProjectionResourceError(
            "CHANGE_TRIAGE source exceeds the bounded deterministic review page",
            cause_code="CHANGE_TRIAGE_SOURCE_TOO_LARGE",
        )
    if request.scope_id is not None and request.scope_id != review.scope_id:
        raise SnapshotScopeError("change triage Scope does not match the selected Snapshots")
    filtered = tuple(
        item
        for item in review.items
        if (request.scope_id is None or item.scope_id == request.scope_id)
        and _path_selected(item.relative_path, request.path_prefix)
    )
    page = filtered[request.offset : request.offset + request.limit]
    base_verification, base_snapshot = _snapshot_or_error(config, review.base_snapshot_id)
    target_verification, target_snapshot = _snapshot_or_error(config, review.target_snapshot_id)
    base_anchor = _snapshot_anchor(base_snapshot)
    target_anchor = _snapshot_anchor(target_snapshot)
    facts: list[dict[str, Any]] = [
        _fact(
            f"change:{review.base_snapshot_id}:{review.target_snapshot_id}:summary",
            f"Change triage found {review.full_event_count} deterministic change events.",
            "SNAPSHOT_PAIR",
            {"scope_id": review.scope_id},
            (base_anchor["id"], target_anchor["id"]),
        )
    ]
    anchors: list[dict[str, Any]] = [base_anchor, target_anchor]
    for item in page:
        identifier = f"change:{review.base_snapshot_id}:{review.target_snapshot_id}:{item.scope_id}:{item.relative_path}"
        facts.append(
            _fact(
                identifier,
                f"{item.relative_path} has change event {item.event_type.value}.",
                "CHANGE_EVENT",
                {"scope_id": item.scope_id, "relative_path": item.relative_path},
                (base_anchor["id"], target_anchor["id"]),
            )
        )
    metrics = (
        _metric(
            f"metric:{review.target_snapshot_id}:change_events",
            "change_event_count",
            len(filtered),
            "events",
            "snapshot_change_review.event_count",
            {
                "base_snapshot_id": review.base_snapshot_id,
                "target_snapshot_id": review.target_snapshot_id,
            },
            (base_anchor["id"], target_anchor["id"]),
        ),
    )
    omitted = len(filtered) - request.offset - len(page)
    omissions = () if omitted <= 0 else (_page_omission("CHANGE_EVENTS", omitted, request.offset + len(page), request.limit, (base_anchor["id"], target_anchor["id"])),)
    result = _base_result(
        ContextProjectionProfile.CHANGE_TRIAGE,
        _source(((base_verification, base_snapshot), (target_verification, target_snapshot)), review.scope_id),
        tuple(facts),
        metrics,
        (),
        (),
        omissions,
        tuple(anchors),
        {
            "has_more": bool(omissions),
            "offset": request.offset,
            "limit": request.limit,
            "next_offset": request.offset + len(page) if omissions else None,
            "request_digest": binding_digest,
            "order": "scope_id,relative_path:utf8-surrogateescape",
        },
        "PARTIAL" if omissions else "COMPLETE",
    )
    return result


def build_context_projection(
    config: StewardConfig,
    request: ContextProjectionRequest,
) -> dict[str, Any]:
    """Build one verified, compact projection without persistence or providers."""
    profile = _validate_request(request)
    if profile == ContextProjectionProfile.CHANGE_TRIAGE:
        if request.base_snapshot_id is None:
            raise ContextProjectionError(
                "CHANGE_TRIAGE requires an explicit base Snapshot", cause_code="BASE_SELECTOR_REQUIRED"
            )
        base_verification, base_snapshot = _snapshot_or_error(config, request.base_snapshot_id)
        target_verification, target_snapshot = _snapshot_or_error(config, request.snapshot_id)
        binding_digest = _validate_continuation(
            _binding(profile, ((base_verification, base_snapshot), (target_verification, target_snapshot)), request),
            request,
        )
        return _change_triage(config, request, binding_digest)
    verification, snapshot = _snapshot_or_error(config, request.snapshot_id)
    binding_digest = _validate_continuation(_binding(profile, ((verification, snapshot),), request), request)
    try:
        return _general_or_structure(profile, snapshot, verification, request, binding_digest)
    except SnapshotScopeError as error:
        raise ContextProjectionError(
            "the requested Scope is not present in the selected Snapshot",
            cause_code="SNAPSHOT_SCOPE_INVALID",
        ) from error
