"""LOCAL-0003-R1C1B pure Snapshot storage integrity classification checks."""

from dataclasses import replace

import pytest

from local_steward.models import (
    SnapshotInventory,
    SnapshotInventoryItem,
    SnapshotStorageIntegrityStatus,
)
from local_steward.snapshots import classify_snapshot_inventory, inspect_snapshot_inventory

from .test_snapshot_queries import snapshot_fixture


def _item(
    snapshot_id: str | None = "snapshot-a",
    evidence_id: str | None = "evidence-a",
    run_id: str | None = "run-a",
    *,
    evidence_present: bool = True,
    index_present: bool = True,
    run_present: bool = True,
    entry_count: int = 3,
    issue_codes: tuple[str, ...] = (),
) -> SnapshotInventoryItem:
    return SnapshotInventoryItem(
        snapshot_id,
        evidence_id,
        run_id,
        evidence_present,
        index_present,
        run_present,
        entry_count,
        f"runs/{run_id}/00000003_filesystem.snapshot.json" if run_id else None,
        issue_codes,
    )


def _inventory(
    *items: SnapshotInventoryItem,
    issues: tuple[dict[str, str], ...] = (),
) -> SnapshotInventory:
    return SnapshotInventory(
        sum(item.evidence_present for item in items),
        sum(item.index_present for item in items),
        sum(item.index_present for item in items),
        len({item.persistent_run_id for item in items if item.persistent_run_id}),
        tuple(items),
        issues,
        sum(item.indexed_entry_count for item in items if item.index_present),
    )


def test_empty_and_complete_inventory_are_healthy_and_repeatable() -> None:
    assert classify_snapshot_inventory(_inventory()).status == SnapshotStorageIntegrityStatus.HEALTHY
    inventory = _inventory(_item(), _item("snapshot-b", "evidence-b", "run-b"))
    first = classify_snapshot_inventory(inventory)
    assert first == classify_snapshot_inventory(inventory)
    assert first.status == SnapshotStorageIntegrityStatus.HEALTHY
    assert first.healthy_snapshot_count == 2
    assert first.degraded_snapshot_count == first.invalid_snapshot_count == 0
    assert first.healthy_snapshot_count + first.degraded_snapshot_count + first.invalid_snapshot_count == len(
        first.items
    )


@pytest.mark.parametrize(
    ("item", "expected_code"),
    [
        (_item(issue_codes=("SNAPSHOT_EVIDENCE_ORPHANED",)), "SNAPSHOT_EVIDENCE_ORPHANED"),
        (_item(index_present=False), "SNAPSHOT_INDEX_MISSING"),
        (_item(entry_count=2, issue_codes=("SNAPSHOT_INDEX_INCOMPLETE",)), "SNAPSHOT_INDEX_INCOMPLETE"),
        (_item(entry_count=2, issue_codes=("SNAPSHOT_ENTRY_INDEX_INCOMPLETE",)), "SNAPSHOT_ENTRY_INDEX_INCOMPLETE"),
        (_item(entry_count=0, issue_codes=("SNAPSHOT_ENTRY_INDEX_INCOMPLETE",)), "SNAPSHOT_ENTRY_INDEX_INCOMPLETE"),
        (_item(issue_codes=("SNAPSHOT_ENTRY_INDEX_INCONSISTENT",)), "SNAPSHOT_ENTRY_INDEX_INCONSISTENT"),
    ],
)
def test_rebuildable_derived_index_facts_are_degraded(
    item: SnapshotInventoryItem, expected_code: str
) -> None:
    report = classify_snapshot_inventory(_inventory(item))
    assert report.status == SnapshotStorageIntegrityStatus.DEGRADED
    assert report.items[0].status == SnapshotStorageIntegrityStatus.DEGRADED
    assert expected_code in report.items[0].issue_codes
    assert report.degraded_snapshot_count == 1 and report.invalid_snapshot_count == 0


@pytest.mark.parametrize(
    ("item", "expected_code"),
    [
        (_item(evidence_present=False), "SNAPSHOT_EVIDENCE_MISSING"),
        (_item(issue_codes=("SNAPSHOT_INDEX_EVIDENCE_MISSING",)), "SNAPSHOT_INDEX_EVIDENCE_MISSING"),
        (_item(issue_codes=("SNAPSHOT_EVIDENCE_INVALID",)), "SNAPSHOT_EVIDENCE_INVALID"),
        (_item(issue_codes=("SNAPSHOT_INDEX_EVIDENCE_TYPE_MISMATCH",)), "SNAPSHOT_INDEX_EVIDENCE_TYPE_MISMATCH"),
        (_item(issue_codes=("SNAPSHOT_INDEX_EVIDENCE_ID_MISMATCH",)), "SNAPSHOT_INDEX_EVIDENCE_ID_MISMATCH"),
        (_item(issue_codes=("SNAPSHOT_INDEX_SNAPSHOT_ID_MISMATCH",)), "SNAPSHOT_INDEX_SNAPSHOT_ID_MISMATCH"),
        (_item(issue_codes=("SNAPSHOT_ID_DUPLICATE",)), "SNAPSHOT_ID_DUPLICATE"),
        (_item(issue_codes=("SNAPSHOT_RUN_DUPLICATE",)), "SNAPSHOT_RUN_DUPLICATE"),
        (_item(issue_codes=("SNAPSHOT_EVIDENCE_INDEX_DUPLICATE",)), "SNAPSHOT_EVIDENCE_INDEX_DUPLICATE"),
        (_item(run_present=False), "SNAPSHOT_RUN_MISSING"),
        (_item(issue_codes=("SNAPSHOT_RUN_KIND_INVALID",)), "SNAPSHOT_RUN_KIND_INVALID"),
        (_item(issue_codes=("SNAPSHOT_RUN_STATUS_INVALID",)), "SNAPSHOT_RUN_STATUS_INVALID"),
        (_item(issue_codes=("SNAPSHOT_ENTRY_ORPHANED",)), "SNAPSHOT_ENTRY_ORPHANED"),
        (_item(issue_codes=("SNAPSHOT_ENTRY_CROSS_REFERENCE",)), "SNAPSHOT_ENTRY_CROSS_REFERENCE"),
        (_item(issue_codes=("UNKNOWN_SNAPSHOT_INTEGRITY_FACT",)), "UNKNOWN_SNAPSHOT_INTEGRITY_FACT"),
    ],
)
def test_fact_damage_identity_conflicts_and_unknowns_are_invalid(
    item: SnapshotInventoryItem, expected_code: str
) -> None:
    report = classify_snapshot_inventory(_inventory(item))
    assert report.status == SnapshotStorageIntegrityStatus.INVALID
    assert report.items[0].status == SnapshotStorageIntegrityStatus.INVALID
    assert expected_code in report.items[0].issue_codes


def test_invalid_wins_over_degraded_for_one_object_and_global_report() -> None:
    report = classify_snapshot_inventory(
        _inventory(
            _item(issue_codes=("SNAPSHOT_EVIDENCE_ORPHANED", "SNAPSHOT_RUN_MISSING")),
            _item("snapshot-b", "evidence-b", "run-b", issue_codes=("SNAPSHOT_INDEX_INCOMPLETE",)),
        )
    )
    assert report.status == SnapshotStorageIntegrityStatus.INVALID
    assert report.invalid_snapshot_count == 1 and report.degraded_snapshot_count == 1
    assert report.items[0].status == SnapshotStorageIntegrityStatus.INVALID


def test_relationship_counts_are_unique_and_items_and_issues_are_stably_ordered() -> None:
    first = _item(
        "snapshot-z",
        "evidence-z",
        "run-duplicate",
        issue_codes=("SNAPSHOT_ID_DUPLICATE", "SNAPSHOT_RUN_DUPLICATE"),
    )
    second = _item(
        "snapshot-z",
        "evidence-a",
        "run-duplicate",
        issue_codes=("SNAPSHOT_ID_DUPLICATE", "SNAPSHOT_RUN_DUPLICATE"),
    )
    third = _item(
        "snapshot-a",
        "evidence-a",
        "run-a",
        issue_codes=("SNAPSHOT_EVIDENCE_INDEX_DUPLICATE", "SNAPSHOT_ENTRY_ORPHANED"),
    )
    fourth = _item(
        "snapshot-m",
        "evidence-m",
        "run-m",
        issue_codes=(
            "SNAPSHOT_EVIDENCE_ORPHANED",
            "SNAPSHOT_INDEX_EVIDENCE_MISSING",
            "SNAPSHOT_ENTRY_CROSS_REFERENCE",
        ),
    )
    inventory = _inventory(
        first,
        second,
        third,
        fourth,
        issues=(
            {"code": "SNAPSHOT_ENTRY_ORPHANED", "snapshot_id": "snapshot-a", "evidence_id": "evidence-a"},
            {"code": "SNAPSHOT_EVIDENCE_INDEX_DUPLICATE", "snapshot_id": "snapshot-a", "evidence_id": "evidence-a"},
        ),
    )
    report = classify_snapshot_inventory(inventory)
    reverse = classify_snapshot_inventory(replace(inventory, items=tuple(reversed(inventory.items))))
    assert report == reverse
    assert report.duplicate_snapshot_id_count == 1
    assert report.duplicate_run_snapshot_count == 1
    assert report.duplicate_evidence_index_count == 1
    assert report.orphan_entry_count == 1
    assert report.cross_reference_entry_count == 1
    assert report.orphan_evidence_count == 1
    assert report.missing_evidence_count == 1
    assert [item.snapshot_id for item in report.items] == [
        "snapshot-a",
        "snapshot-m",
        "snapshot-z",
        "snapshot-z",
    ]


def test_input_inventory_is_not_modified_and_raw_global_issue_is_invalid() -> None:
    issue = {"code": "UNKNOWN_GLOBAL_FACT", "snapshot_id": "", "evidence_id": ""}
    inventory = _inventory(_item(), issues=(issue,))
    before = (inventory, dict(issue))
    report = classify_snapshot_inventory(inventory)
    assert report.status == SnapshotStorageIntegrityStatus.INVALID
    assert inventory == before[0] and issue == before[1]
    assert report.invalid_snapshot_count == 0


def test_raw_item_issue_is_classified_and_issue_order_does_not_matter() -> None:
    first = {"code": "SNAPSHOT_INDEX_INCOMPLETE", "snapshot_id": "snapshot-a", "evidence_id": "evidence-a"}
    second = {"code": "UNKNOWN_ASSOCIATED_FACT", "snapshot_id": "snapshot-a", "evidence_id": "evidence-a"}
    inventory = _inventory(_item(), issues=(first, second))
    reversed_inventory = replace(inventory, issues=(second, first))
    report = classify_snapshot_inventory(inventory)
    assert report == classify_snapshot_inventory(reversed_inventory)
    assert report.items[0].status == SnapshotStorageIntegrityStatus.INVALID


def test_classifier_does_not_open_storage_or_reenumerate_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "local_steward.snapshots.open_initialized",
        lambda _config: (_ for _ in ()).throw(AssertionError("storage access")),
    )
    monkeypatch.setattr(
        "local_steward.snapshots.load_run_files",
        lambda _root, _run: (_ for _ in ()).throw(AssertionError("ledger access")),
    )
    assert classify_snapshot_inventory(_inventory(_item())).status == SnapshotStorageIntegrityStatus.HEALTHY


def test_inventory_output_is_classified_without_scanning_or_mutating(tmp_path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    inventory = inspect_snapshot_inventory(config)
    report = classify_snapshot_inventory(inventory)
    item = next(item for item in report.items if item.snapshot_id == snapshot.snapshot_id)
    assert report.status == SnapshotStorageIntegrityStatus.HEALTHY
    assert item.status == SnapshotStorageIntegrityStatus.HEALTHY
