from dataclasses import replace
from pathlib import Path

import pytest

from local_steward.agent_session import (
    SelectionPolicy,
    SnapshotSelectionRequest,
    create_steward_session,
    resolve_snapshot,
)
from local_steward.agent_session.errors import StewardPathResolutionError
from local_steward.document_collection import (
    document_collection_machine_object,
    plan_snapshot_documents,
    revalidate_snapshot_document,
)
from local_steward.models import ScanBudget
from local_steward.snapshot_acquisition import SnapshotAcquisitionRequest, acquire_snapshot

from .test_protocol_completion import prepared_config


@pytest.fixture(autouse=True)
def _allow_task_owned_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())


def _fixture(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = prepared_config(tmp_path)
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "report-one.pdf").write_bytes(b"one")
    (scope / "report-two.docx").write_bytes(b"two")
    (scope / "report-code.py").write_text("print('not a document')", encoding="utf-8")
    config = replace(
        config,
        scopes=(replace(config.scopes[0], raw_path=str(scope), normalized_path=scope),),
    )
    report = acquire_snapshot(
        config,
        SnapshotAcquisitionRequest("managed", ScanBudget(max_entries=100), True),
    )
    session = create_steward_session(config)
    snapshot = resolve_snapshot(
        session,
        SnapshotSelectionRequest(
            SelectionPolicy.EXACT_ID,
            exact_snapshot_id=report.snapshot_id,
            scope_id="managed",
        ),
    )
    return scope, session, snapshot


def test_snapshot_plan_is_verified_bounded_and_document_only(tmp_path: Path) -> None:
    scope, session, snapshot = _fixture(tmp_path)

    first = plan_snapshot_documents(
        session,
        snapshot,
        query="report",
        extensions=["PDF", "DOCX"],
        max_documents=2,
    )
    second = plan_snapshot_documents(
        session,
        snapshot,
        query="report",
        extensions=["DOCX", "PDF"],
        max_documents=2,
    )

    assert first.source_kind == "VERIFIED_HISTORICAL_SNAPSHOT"
    assert first.matched_count == first.returned_count == 2
    assert first.candidate_set_digest == second.candidate_set_digest
    assert {item.historical.relative_path for item in first.candidates} == {
        "report-one.pdf",
        "report-two.docx",
    }
    assert all(item.historical.snapshot_id == snapshot.snapshot.snapshot_id for item in first.candidates)
    assert str(scope) not in str(document_collection_machine_object(first))


def test_current_revalidation_reports_drift_without_treating_snapshot_as_current(
    tmp_path: Path,
) -> None:
    scope, session, snapshot = _fixture(tmp_path)
    plan = plan_snapshot_documents(session, snapshot, query="report-one.pdf")
    candidate = plan.candidates[0]

    unchanged = revalidate_snapshot_document(session, candidate)
    assert unchanged.source_kind == "CURRENT_FILESYSTEM_DOCUMENT"
    assert unchanged.historical_metadata_relation == "METADATA_MATCH"

    (scope / "report-one.pdf").write_bytes(b"changed current content")
    changed = revalidate_snapshot_document(session, candidate)
    assert changed.historical_metadata_relation == "METADATA_CHANGED"
    assert changed.relative_path == candidate.historical.relative_path


def test_missing_or_symlinked_snapshot_candidate_fails_current_admission(
    tmp_path: Path,
) -> None:
    scope, session, snapshot = _fixture(tmp_path)
    candidate = plan_snapshot_documents(session, snapshot, query="report-one.pdf").candidates[0]
    source = scope / "report-one.pdf"
    source.unlink()
    source.symlink_to(scope / "report-two.docx")

    with pytest.raises(StewardPathResolutionError):
        revalidate_snapshot_document(session, candidate)
