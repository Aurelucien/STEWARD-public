"""Read-only composition of the frozen Steward observation services."""

from .change_semantics import change_events_from_snapshot_diff, summarize_change_events
from .errors import DiffError
from .models import (
    EvidenceVerificationReport,
    SystemStatusEvidenceHealth,
    SystemStatusRecentChanges,
    SystemStatusReview,
    StewardConfig,
)
from .resources import observe_resources
from .snapshot_diff import compute_verified_snapshot_diff
from .snapshots import list_snapshots, verify_snapshot
from .storage import storage_status, verify_evidence_report


def _evidence_health(report: EvidenceVerificationReport) -> SystemStatusEvidenceHealth:
    """Preserve verifier facts while deriving only a concise display status."""
    if not report.verifications and report.snapshot_evidence.evidence_count == 0:
        status = "UNAVAILABLE"
    elif (
        all(item.status == "VALID" for item in report.verifications)
        and report.snapshot_evidence.invalid_count == 0
    ):
        status = "VALID"
    else:
        status = "INVALID"
    return SystemStatusEvidenceHealth(status, report.verifications, report.snapshot_evidence)


def _recent_changes(config: StewardConfig) -> SystemStatusRecentChanges:
    """Review the latest two verifier-approved Snapshot facts, newest on the right."""
    valid_ids: list[str] = []
    for snapshot in list_snapshots(config, limit=None):
        if verify_snapshot(config, snapshot.snapshot_id).status == "VALID":
            valid_ids.append(snapshot.snapshot_id)
            if len(valid_ids) == 2:
                break
    if len(valid_ids) < 2:
        return SystemStatusRecentChanges(
            "UNAVAILABLE",
            None,
            None,
            None,
            (),
            None,
            "INSUFFICIENT_VALID_SNAPSHOTS",
        )

    left_snapshot_id, right_snapshot_id = valid_ids[1], valid_ids[0]
    try:
        snapshot_diff = compute_verified_snapshot_diff(config, left_snapshot_id, right_snapshot_id)
    except DiffError:
        return SystemStatusRecentChanges(
            "UNAVAILABLE",
            left_snapshot_id,
            right_snapshot_id,
            None,
            (),
            None,
            "RECENT_SNAPSHOT_DIFF_UNAVAILABLE",
        )
    events = change_events_from_snapshot_diff(snapshot_diff)
    return SystemStatusRecentChanges(
        "AVAILABLE",
        left_snapshot_id,
        right_snapshot_id,
        snapshot_diff.summary,
        events,
        summarize_change_events(events),
        None,
    )


def build_system_status_review(
    config: StewardConfig,
    sample_seconds: float = 1.0,
    top: int = 20,
    sort: str = "cpu",
) -> SystemStatusReview:
    """Compose existing read-only services without creating or modifying facts."""
    resources = observe_resources(sample_seconds, top, sort)
    evidence_health = _evidence_health(verify_evidence_report(config))
    recent_changes = _recent_changes(config)
    limitations = tuple(
        sorted(
            set(
                (*resources.warnings,)
                + ((recent_changes.limitation,) if recent_changes.limitation else ())
            )
        )
    )
    return SystemStatusReview(
        resources,
        evidence_health,
        storage_status(config),
        recent_changes,
        limitations,
    )
