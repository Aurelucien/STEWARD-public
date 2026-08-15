"""Isolated acceptance for the R4D-R3B unified STEWARD session."""

from dataclasses import asdict, replace
from pathlib import Path

import pytest

from local_steward.agent_session import (
    PathInputKind,
    ScopeSelectionRequest,
    SelectionPolicy,
    SnapshotSelectionRequest,
    StewardAuthorityDomainError,
    StewardPathResolutionError,
    StewardSelectionAmbiguousError,
    StewardSelectionNotFoundError,
    StewardTaskReferenceError,
    TaskObjectKind,
    TaskObjectMemory,
    TaskObjectReference,
    create_steward_session,
    require_authority_domain,
    resolve_scope,
    resolve_scoped_path,
    resolve_snapshot,
    resolve_user_absolute_path,
    resolve_user_absolute_scope,
    safe_session_identity_payload,
)
from local_steward.models import ScopeConfig, ScopeRole
from local_steward.scan_budget import make_budget
from local_steward.snapshots import create_snapshot

from .test_protocol_completion import prepared_config


@pytest.fixture(autouse=True)
def _admit_task_owned_temporary_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The macOS pytest root is below /private, which production correctly protects."""
    monkeypatch.setattr("local_steward.scopes.SYSTEM_PROTECTED_PATHS", ())


def _snapshot_session(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = prepared_config(tmp_path)
    root = tmp_path / "scope"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    config = replace(
        config,
        scopes=(replace(config.scopes[0], raw_path=str(root), normalized_path=root),),
    )
    snapshots = [create_snapshot(config, (), make_budget())]
    (root / "b.txt").write_text("b", encoding="utf-8")
    snapshots.append(create_snapshot(config, (), make_budget()))
    (root / "c.txt").write_text("c", encoding="utf-8")
    snapshots.append(create_snapshot(config, (), make_budget()))
    return config, root, tuple(snapshots), create_steward_session(config)


def test_session_identity_is_path_safe_and_rejects_split_configuration(tmp_path: Path) -> None:
    config = prepared_config(tmp_path / "first")
    session = create_steward_session(config)
    payload = safe_session_identity_payload(session)
    encoded = repr(payload)

    assert payload["schema_name"] == "local_steward.steward_session"
    assert len(str(payload["configuration_digest"])) == 64
    assert len(str(payload["authority_domain_digest"])) == 64
    assert str(tmp_path) not in encoded
    assert "state.db" not in encoded and "evidence" not in encoded.lower()
    assert "source_path" not in asdict(session.identity)
    assert require_authority_domain(session, config) is session.config

    other = prepared_config(tmp_path / "second")
    with pytest.raises(StewardAuthorityDomainError):
        require_authority_domain(session, other)


def test_snapshot_selection_supports_all_frozen_policies(tmp_path: Path) -> None:
    _config, _root, snapshots, session = _snapshot_session(tmp_path)
    first, second, third = snapshots

    exact = resolve_snapshot(
        session,
        SnapshotSelectionRequest(SelectionPolicy.EXACT_ID, exact_snapshot_id=second.snapshot_id),
    )
    latest = resolve_snapshot(
        session,
        SnapshotSelectionRequest(SelectionPolicy.LATEST_VALID, scope_id="managed"),
    )
    previous = resolve_snapshot(
        session,
        SnapshotSelectionRequest(
            SelectionPolicy.PREVIOUS_VALID,
            scope_id="managed",
            anchor_snapshot_id=third.snapshot_id,
        ),
    )
    memory = TaskObjectMemory(session, "task-selection")
    task_reference = memory.remember_snapshot(first.snapshot_id)
    task_created = resolve_snapshot(
        session,
        SnapshotSelectionRequest(
            SelectionPolicy.TASK_CREATED,
            scope_id="managed",
            task_reference=task_reference,
        ),
        task_memory=memory,
    )

    assert exact.snapshot.snapshot_id == second.snapshot_id
    assert latest.snapshot.snapshot_id == third.snapshot_id
    assert previous.snapshot.snapshot_id == second.snapshot_id
    assert task_created.snapshot.snapshot_id == first.snapshot_id
    assert all(
        item.verification.status == "VALID" for item in (exact, latest, previous, task_created)
    )
    with pytest.raises(StewardSelectionAmbiguousError):
        resolve_snapshot(
            session,
            SnapshotSelectionRequest(SelectionPolicy.ONLY_COMPATIBLE, scope_id="managed"),
        )
    with pytest.raises(StewardSelectionNotFoundError):
        resolve_snapshot(
            session,
            SnapshotSelectionRequest(SelectionPolicy.LATEST_VALID, scope_id="unknown"),
        )


def test_semantic_timestamp_tie_fails_instead_of_using_hidden_id_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _config, _root, snapshots, session = _snapshot_session(tmp_path)
    first = resolve_snapshot(
        session,
        SnapshotSelectionRequest(
            SelectionPolicy.EXACT_ID, exact_snapshot_id=snapshots[0].snapshot_id
        ),
    )
    second = resolve_snapshot(
        session,
        SnapshotSelectionRequest(
            SelectionPolicy.EXACT_ID, exact_snapshot_id=snapshots[1].snapshot_id
        ),
    )
    tied = (
        (replace(first.snapshot, created_at="2026-01-01T00:00:00.000000Z"), first.verification),
        (replace(second.snapshot, created_at="2026-01-01T00:00:00.000000Z"), second.verification),
    )
    monkeypatch.setattr(
        "local_steward.agent_session.service._snapshot_inventory_with_verification",
        lambda _config, _limit: tied,
    )

    with pytest.raises(StewardSelectionAmbiguousError):
        resolve_snapshot(
            session,
            SnapshotSelectionRequest(SelectionPolicy.LATEST_VALID, scope_id="managed"),
        )


def test_scope_resolution_is_exact_task_owned_or_uniquely_compatible(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    root = tmp_path / "scope"
    root.mkdir()
    config = replace(
        config,
        scopes=(replace(config.scopes[0], raw_path=str(root), normalized_path=root),),
    )
    session = create_steward_session(config)
    exact = resolve_scope(
        session, ScopeSelectionRequest(SelectionPolicy.EXACT_ID, exact_scope_id="managed")
    )
    only = resolve_scope(session, ScopeSelectionRequest(SelectionPolicy.ONLY_COMPATIBLE))
    memory = TaskObjectMemory(session, "task-scope")
    reference = memory.remember_scope("managed")
    remembered = resolve_scope(
        session,
        ScopeSelectionRequest(SelectionPolicy.TASK_CREATED, task_reference=reference),
        task_memory=memory,
    )

    assert exact.scope_id == only.scope_id == remembered.scope_id == "managed"

    other_root = tmp_path / "other"
    other_root.mkdir()
    second_scope = ScopeConfig(
        "reference",
        ScopeRole.REFERENCE_ROOT,
        str(other_root),
        other_root,
        True,
        False,
        False,
    )
    ambiguous = create_steward_session(replace(config, scopes=config.scopes + (second_scope,)))
    with pytest.raises(StewardSelectionAmbiguousError):
        resolve_scope(ambiguous, ScopeSelectionRequest(SelectionPolicy.ONLY_COMPATIBLE))


def test_absolute_and_scoped_paths_map_to_internal_identity_only(tmp_path: Path) -> None:
    config = prepared_config(tmp_path)
    root = tmp_path / "scope"
    allowed = root / "folder" / "paper.pdf"
    allowed.parent.mkdir(parents=True)
    allowed.write_bytes(b"pdf")
    excluded = root / "private"
    excluded.mkdir()
    (excluded / "secret.pdf").write_bytes(b"secret")
    config = replace(
        config,
        scopes=(
            replace(config.scopes[0], raw_path=str(root), normalized_path=root),
            ScopeConfig(
                "private",
                ScopeRole.EXCLUDED_ROOT,
                str(excluded),
                excluded,
                True,
                False,
                False,
            ),
        ),
    )
    session = create_steward_session(config)

    absolute = resolve_user_absolute_path(session, str(allowed))
    relative = resolve_scoped_path(session, "managed", "folder/paper.pdf")

    assert absolute.input_kind == PathInputKind.USER_ABSOLUTE
    assert absolute.scope_id == relative.scope_id == "managed"
    assert absolute.relative_path == relative.relative_path == "folder/paper.pdf"
    assert str(root) not in repr(absolute)
    assert resolve_user_absolute_scope(session, str(root)).scope_id == "managed"
    with pytest.raises(StewardPathResolutionError):
        resolve_user_absolute_path(session, str(excluded / "secret.pdf"))
    with pytest.raises(StewardSelectionNotFoundError):
        resolve_user_absolute_path(session, str(tmp_path / "outside.pdf"))
    with pytest.raises(StewardPathResolutionError):
        resolve_scoped_path(session, "managed", "../escape.pdf")


def test_symlink_path_and_foreign_or_fabricated_task_references_fail_closed(
    tmp_path: Path,
) -> None:
    _config, root, snapshots, session = _snapshot_session(tmp_path)
    target = root / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(target)
    with pytest.raises(StewardPathResolutionError):
        resolve_user_absolute_path(session, str(link))

    root_alias = tmp_path / "scope-alias"
    root_alias.symlink_to(root, target_is_directory=True)
    aliased_scope = replace(
        session.config.scopes[0], raw_path=str(root_alias), normalized_path=root
    )
    aliased_session = create_steward_session(replace(session.config, scopes=(aliased_scope,)))
    with pytest.raises(StewardPathResolutionError):
        resolve_scoped_path(aliased_session, "managed", "a.txt")

    first_memory = TaskObjectMemory(session, "first-task")
    reference = first_memory.remember_snapshot(snapshots[0].snapshot_id)
    second_memory = TaskObjectMemory(session, "second-task")
    with pytest.raises(StewardTaskReferenceError):
        second_memory.require(reference, TaskObjectKind.SNAPSHOT)
    fabricated = TaskObjectReference(
        reference.reference_id,
        reference.authority_domain_digest,
        reference.kind,
        snapshots[1].snapshot_id,
        snapshots[1].snapshot_id,
    )
    with pytest.raises(StewardTaskReferenceError):
        first_memory.require(fabricated, TaskObjectKind.SNAPSHOT)


def test_task_entry_reference_resolves_without_publishing_a_host_root(tmp_path: Path) -> None:
    _config, root, snapshots, session = _snapshot_session(tmp_path)
    memory = TaskObjectMemory(session, "entry-task")
    reference = memory.remember_entry(snapshots[-1].snapshot_id, "managed", "a.txt")
    resolved = memory.resolve_entry_path(reference)

    assert reference.kind == TaskObjectKind.ENTRY
    assert resolved.policy == SelectionPolicy.TASK_CREATED
    assert resolved.input_kind == PathInputKind.TASK_CREATED_ENTRY
    assert resolved.scope_id == "managed" and resolved.relative_path == "a.txt"
    assert str(root) not in repr(resolved)
    assert memory.remember_run_from_snapshot(snapshots[-1].snapshot_id).kind == TaskObjectKind.RUN
    assert memory.reference_count == 2
    run_reference = memory.remember_run(snapshots[-1].run_id)
    assert memory.reference(run_reference.reference_id, TaskObjectKind.RUN) == run_reference
