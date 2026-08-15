"""Supported same-Scope Snapshot refresh and bounded deterministic change review."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Callable

from .change_semantics import change_events_from_snapshot_diff, summarize_change_events
from .errors import (
    SnapshotAcquisitionConfirmationError,
    SnapshotChangeReviewError,
    SnapshotChangeReviewResourceError,
    SnapshotNotFoundError,
    SnapshotRefreshBaseError,
    StewardError,
)
from .evidence import canonical_json
from .models import (
    ChangeEventSummary,
    ChangeEventType,
    RunStatus,
    ScanBudget,
    SnapshotDiffSummary,
    SnapshotStatus,
    SnapshotVerificationResult,
    StewardConfig,
)
from .snapshot_acquisition import (
    SnapshotAcquisitionReport,
    SnapshotAcquisitionRequest,
    _bind_scope,
    _ledger_state,
    _validate_budget,
    acquire_snapshot,
)
from .snapshot_diff import compute_verified_snapshot_diff
from .snapshots import get_snapshot, verify_snapshot


REFRESH_PROTOCOL_VERSION = 1
MAX_REFRESH_ENTRIES = 100_000
MAX_CHANGE_PAGE = 1_000


@dataclass(frozen=True, slots=True)
class SnapshotRefreshRequest:
    scope_id: str
    base_snapshot_id: str
    budget: ScanBudget = ScanBudget(max_entries=MAX_REFRESH_ENTRIES)
    confirmed: bool = False
    change_limit: int = 100
    change_offset: int = 0


@dataclass(frozen=True, slots=True)
class SnapshotChangeReviewRequest:
    base_snapshot_id: str
    target_snapshot_id: str
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True, slots=True)
class SnapshotChangeReviewItem:
    scope_id: str
    relative_path: str
    event_type: ChangeEventType
    changed_fields: tuple[str, ...]
    size_delta: int | None
    hash_changed: bool | None
    metadata_changed: bool


@dataclass(frozen=True, slots=True)
class SnapshotChangeReview:
    protocol_version: int
    base_snapshot_id: str
    target_snapshot_id: str
    base_run_id: str
    target_run_id: str
    base_evidence_id: str
    target_evidence_id: str
    scope_id: str
    base_binding_digest: str
    target_binding_digest: str
    base_verification: SnapshotVerificationResult
    target_verification: SnapshotVerificationResult
    diff_summary: SnapshotDiffSummary
    event_summary: ChangeEventSummary
    items: tuple[SnapshotChangeReviewItem, ...]
    full_event_count: int
    returned_count: int
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None
    review_digest: str


@dataclass(frozen=True, slots=True)
class SnapshotRefreshReport:
    protocol_version: int
    disposition: str
    base_snapshot_id: str
    scope_id: str
    acquisition: SnapshotAcquisitionReport
    review: SnapshotChangeReview | None
    review_errors: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _SupportedSnapshotContext:
    snapshot_id: str
    run_id: str
    evidence_id: str
    scope_id: str
    binding: dict[str, object]
    verification: SnapshotVerificationResult
    entry_count: int


def _page(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_CHANGE_PAGE:
        raise SnapshotChangeReviewError("change review limit must be an integer from 1 to 1000")
    if type(offset) is not int or offset < 0:
        raise SnapshotChangeReviewError("change review offset must be a nonnegative integer")


def _context(
    config: StewardConfig,
    snapshot_id: str,
    *,
    field: str,
    error_type: type[SnapshotRefreshBaseError] | type[SnapshotChangeReviewError],
) -> _SupportedSnapshotContext:
    try:
        snapshot = get_snapshot(config, snapshot_id)
    except SnapshotNotFoundError as error:
        raise error_type(f"{field} Snapshot is unavailable") from error
    verification = verify_snapshot(config, snapshot_id)
    if verification.status != "VALID":
        raise error_type(f"{field} Snapshot must be authoritatively VALID")
    state = _ledger_state(config, snapshot.run_id)
    if (
        not state.governed
        or state.status != RunStatus.VERIFIED
        or state.snapshot is None
        or state.snapshot.snapshot_id != snapshot_id
        or state.snapshot.status != SnapshotStatus.COMPLETE
        or len(state.snapshot.scope_ids) != 1
        or state.snapshot_evidence_id is None
    ):
        raise error_type(
            f"{field} Snapshot must be complete terminal supported-acquisition-v1 Evidence"
        )
    binding = state.metadata.get("scope_binding")
    if not isinstance(binding, dict):
        raise error_type(f"{field} Snapshot Scope binding is unavailable")
    return _SupportedSnapshotContext(
        snapshot_id,
        state.run_id,
        state.snapshot_evidence_id,
        state.snapshot.scope_ids[0],
        binding,
        verification,
        state.snapshot.entry_count,
    )


def _stable_binding(binding: dict[str, object]) -> tuple[object, ...]:
    return tuple(
        binding.get(key)
        for key in (
            "scope_id",
            "role",
            "device_id",
            "inode",
            "follow_directory_symlinks",
            "allow_cross_mount",
        )
    )


def _binding_digest(
    context: _SupportedSnapshotContext,
    error_type: type[SnapshotRefreshBaseError] | type[SnapshotChangeReviewError],
) -> str:
    value = context.binding.get("binding_digest")
    if not isinstance(value, str):
        raise error_type("supported Snapshot binding digest is unavailable")
    return value


def _preflight_refresh(
    config: StewardConfig,
    request: SnapshotRefreshRequest,
) -> ScanBudget:
    budget = _validate_budget(request.budget)
    if budget.max_entries > MAX_REFRESH_ENTRIES:
        raise SnapshotChangeReviewResourceError(
            "Snapshot refresh max_entries cannot exceed 100000"
        )
    base = _context(
        config,
        request.base_snapshot_id,
        field="base",
        error_type=SnapshotRefreshBaseError,
    )
    if base.scope_id != request.scope_id:
        raise SnapshotRefreshBaseError("base Snapshot Scope differs from the requested Scope")
    current = _bind_scope(config, request.scope_id, budget)
    current_binding = current.metadata["scope_binding"]
    if not isinstance(current_binding, dict) or _stable_binding(base.binding) != _stable_binding(
        current_binding
    ):
        raise SnapshotRefreshBaseError(
            "current Scope authority differs from the base Snapshot binding"
        )
    return budget


def _review_item_projection(item: SnapshotChangeReviewItem) -> dict[str, object]:
    return {
        "scope_id": item.scope_id,
        "relative_path": item.relative_path,
        "event_type": item.event_type.value,
        "changed_fields": list(item.changed_fields),
        "size_delta": item.size_delta,
        "hash_changed": item.hash_changed,
        "metadata_changed": item.metadata_changed,
    }


def review_snapshot_changes(
    config: StewardConfig,
    request: SnapshotChangeReviewRequest,
) -> SnapshotChangeReview:
    """Review a bounded page derived only from two compatible verified Snapshots."""
    _page(request.limit, request.offset)
    base = _context(
        config,
        request.base_snapshot_id,
        field="base",
        error_type=SnapshotChangeReviewError,
    )
    target = _context(
        config,
        request.target_snapshot_id,
        field="target",
        error_type=SnapshotChangeReviewError,
    )
    if base.scope_id != target.scope_id or _stable_binding(base.binding) != _stable_binding(
        target.binding
    ):
        raise SnapshotChangeReviewError(
            "change review requires matching single-Scope acquisition authority"
        )
    if base.entry_count > MAX_REFRESH_ENTRIES or target.entry_count > MAX_REFRESH_ENTRIES:
        raise SnapshotChangeReviewResourceError(
            "change review Snapshot entry count exceeds 100000"
        )
    snapshot_diff = compute_verified_snapshot_diff(
        config, request.base_snapshot_id, request.target_snapshot_id
    )
    events = change_events_from_snapshot_diff(snapshot_diff)
    diff_items = {
        (item.scope_id, item.relative_path): item for item in snapshot_diff.items
    }
    items = tuple(
        SnapshotChangeReviewItem(
            event.scope_id,
            event.relative_path,
            event.event_type,
            diff_items[(event.scope_id, event.relative_path)].changed_fields,
            event.size_delta,
            event.hash_changed,
            event.metadata_changed,
        )
        for event in events
    )
    event_summary = summarize_change_events(events)
    digest_value = {
        "domain": "local_steward.snapshot_change_review.v1",
        "base_snapshot_id": base.snapshot_id,
        "target_snapshot_id": target.snapshot_id,
        "base_binding_digest": _binding_digest(base, SnapshotChangeReviewError),
        "target_binding_digest": _binding_digest(target, SnapshotChangeReviewError),
        "diff_summary": {
            "added_count": snapshot_diff.summary.added_count,
            "removed_count": snapshot_diff.summary.removed_count,
            "modified_count": snapshot_diff.summary.modified_count,
            "unchanged_count": snapshot_diff.summary.unchanged_count,
            "item_count": snapshot_diff.summary.item_count,
        },
        "event_summary": {
            "created_count": event_summary.created_count,
            "deleted_count": event_summary.deleted_count,
            "modified_count": event_summary.modified_count,
            "event_count": event_summary.event_count,
        },
        "items": [_review_item_projection(item) for item in items],
    }
    review_digest = sha256(canonical_json(digest_value)).hexdigest()
    page = items[request.offset : request.offset + request.limit]
    next_offset = request.offset + len(page)
    has_more = next_offset < len(items)
    return SnapshotChangeReview(
        REFRESH_PROTOCOL_VERSION,
        base.snapshot_id,
        target.snapshot_id,
        base.run_id,
        target.run_id,
        base.evidence_id,
        target.evidence_id,
        base.scope_id,
        _binding_digest(base, SnapshotChangeReviewError),
        _binding_digest(target, SnapshotChangeReviewError),
        base.verification,
        target.verification,
        snapshot_diff.summary,
        event_summary,
        page,
        len(items),
        len(page),
        request.limit,
        request.offset,
        has_more,
        next_offset if has_more else None,
        review_digest,
    )


def _refresh_snapshot(
    config: StewardConfig,
    request: SnapshotRefreshRequest,
    *,
    reviewer: Callable[
        [StewardConfig, SnapshotChangeReviewRequest], SnapshotChangeReview
    ] = review_snapshot_changes,
) -> SnapshotRefreshReport:
    if request.confirmed is not True:
        raise SnapshotAcquisitionConfirmationError(
            "Snapshot refresh requires explicit confirmation"
        )
    _page(request.change_limit, request.change_offset)
    budget = _preflight_refresh(config, request)
    acquisition = acquire_snapshot(
        config,
        SnapshotAcquisitionRequest(request.scope_id, budget, confirmed=True),
    )
    if acquisition.disposition == "PARTIAL":
        return SnapshotRefreshReport(
            REFRESH_PROTOCOL_VERSION,
            "PARTIAL_NO_REVIEW",
            request.base_snapshot_id,
            request.scope_id,
            acquisition,
            None,
        )
    if acquisition.snapshot_id is None:
        return SnapshotRefreshReport(
            REFRESH_PROTOCOL_VERSION,
            "ACQUIRED_REVIEW_UNAVAILABLE",
            request.base_snapshot_id,
            request.scope_id,
            acquisition,
            None,
            ({"code": "SNAPSHOT_CHANGE_REVIEW_TARGET_MISSING", "message": "acquisition published no target Snapshot identity"},),
        )
    try:
        review = reviewer(
            config,
            SnapshotChangeReviewRequest(
                request.base_snapshot_id,
                acquisition.snapshot_id,
                request.change_limit,
                request.change_offset,
            ),
        )
    except StewardError as error:
        return SnapshotRefreshReport(
            REFRESH_PROTOCOL_VERSION,
            "ACQUIRED_REVIEW_UNAVAILABLE",
            request.base_snapshot_id,
            request.scope_id,
            acquisition,
            None,
            ({"code": error.code, "message": str(error)},),
        )
    except Exception:
        return SnapshotRefreshReport(
            REFRESH_PROTOCOL_VERSION,
            "ACQUIRED_REVIEW_UNAVAILABLE",
            request.base_snapshot_id,
            request.scope_id,
            acquisition,
            None,
            ({"code": "SNAPSHOT_CHANGE_REVIEW_FAILED", "message": "change review could not be published"},),
        )
    return SnapshotRefreshReport(
        REFRESH_PROTOCOL_VERSION,
        "COMPLETE",
        request.base_snapshot_id,
        request.scope_id,
        acquisition,
        review,
    )


def refresh_snapshot(
    config: StewardConfig,
    request: SnapshotRefreshRequest,
) -> SnapshotRefreshReport:
    """Acquire once and publish one bounded review without adding new authority."""
    return _refresh_snapshot(config, request)
