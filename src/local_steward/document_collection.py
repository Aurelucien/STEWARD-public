"""Snapshot-guided document discovery with mandatory current identity checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import stat
from typing import Any

from .agent_session import ResolvedScope, ResolvedSnapshot, StewardSession, resolve_scoped_path
from .document_discovery import (
    match_document_path,
    normalize_document_extensions,
    normalize_document_query,
    search_current_documents,
)
from .models import FilesystemEntryV2, PayloadObservationStatus
from .snapshots import verified_snapshot_document_entries


MAX_COLLECTION_DOCUMENTS = 8


@dataclass(frozen=True, slots=True)
class HistoricalDocumentIdentity:
    snapshot_id: str
    run_id: str
    snapshot_digest: str
    entry_id: str
    scope_id: str
    relative_path: str
    size_bytes: int | None
    mtime_ns: int | None
    device_id: int | None
    inode: int | None
    payload_sha256: str | None


@dataclass(frozen=True, slots=True)
class SnapshotDocumentCandidate:
    rank: int
    match_kind: str
    source_format: str
    historical: HistoricalDocumentIdentity


@dataclass(frozen=True, slots=True)
class SnapshotDocumentPlan:
    source_kind: str
    selection_policy: str
    query: str
    extensions: tuple[str, ...]
    matched_count: int
    returned_count: int
    has_more: bool
    candidate_set_digest: str
    candidates: tuple[SnapshotDocumentCandidate, ...]


@dataclass(frozen=True, slots=True)
class CurrentDocumentCandidate:
    rank: int
    match_kind: str
    source_format: str
    scope_id: str
    relative_path: str
    discovered_size_bytes: int | None
    discovered_mtime_ns: int | None


@dataclass(frozen=True, slots=True)
class CurrentDocumentPlan:
    source_kind: str
    selection_policy: str
    query: str
    extensions: tuple[str, ...]
    matched_count: int
    returned_count: int
    has_more: bool
    partial: bool
    warnings: tuple[str, ...]
    candidate_set_digest: str
    candidates: tuple[CurrentDocumentCandidate, ...]


@dataclass(frozen=True, slots=True)
class CurrentDocumentIdentity:
    source_kind: str
    scope_id: str
    relative_path: str
    source_format: str
    size_bytes: int
    mtime_ns: int
    device_id: int
    inode: int
    historical_metadata_relation: str
    historical_payload_sha256: str | None


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def document_collection_request_digest(
    *,
    source_kind: str,
    selection_policy: str,
    query: str,
    content_query: str,
    extensions: tuple[str, ...],
    max_documents: int,
    batch_size: int,
    parser_timeout_seconds: float,
    evidence_mode: str,
    evidence_context_items: int,
    evidence_max_characters: int,
    content_limit: int,
    diagnostic_detail: str,
) -> str:
    """Bind a stateless continuation to every collection-affecting request fact."""

    return _canonical_digest(
        {
            "source_kind": source_kind,
            "selection_policy": selection_policy,
            "query": normalize_document_query(query),
            "content_query": content_query,
            "extensions": extensions,
            "max_documents": max_documents,
            "batch_size": batch_size,
            "parser_timeout_seconds": parser_timeout_seconds,
            "evidence_mode": evidence_mode,
            "evidence_context_items": evidence_context_items,
            "evidence_max_characters": evidence_max_characters,
            "content_limit": content_limit,
            "diagnostic_detail": diagnostic_detail,
        }
    )


def _historical_payload_sha256(entry: object) -> str | None:
    if not isinstance(entry, FilesystemEntryV2):
        return None
    observation = entry.payload_observation
    if (
        observation.status
        not in {PayloadObservationStatus.HASHED, PayloadObservationStatus.EMPTY_FILE_HASHED}
        or observation.algorithm != "sha256"
        or observation.digest is None
    ):
        return None
    return observation.digest


def plan_snapshot_documents(
    session: StewardSession,
    snapshot: ResolvedSnapshot,
    *,
    query: str,
    extensions: tuple[str, ...] | list[str] | None = None,
    max_documents: int = 4,
) -> SnapshotDocumentPlan:
    """Use one valid Snapshot only to shortlist bounded historical candidates."""

    if type(max_documents) is not int or not 1 <= max_documents <= MAX_COLLECTION_DOCUMENTS:
        raise ValueError("collection document limit is invalid")
    normalized_query = normalize_document_query(query)
    normalized_extensions = normalize_document_extensions(extensions)
    verification, materialized, entries = verified_snapshot_document_entries(
        session.config,
        snapshot.snapshot.snapshot_id,
        snapshot.compatible_scope_id,
    )
    if verification.status != "VALID":
        raise ValueError("Snapshot is not valid")
    matches: list[tuple[int, int, str, SnapshotDocumentCandidate]] = []
    for entry in entries:
        suffix = Path(entry.relative_path).suffix.casefold()
        if suffix not in normalized_extensions:
            continue
        matched = match_document_path(entry.relative_path, normalized_query)
        if matched is None:
            continue
        rank, match_kind = matched
        historical = HistoricalDocumentIdentity(
            materialized.snapshot_id,
            materialized.run_id,
            materialized.snapshot_digest,
            entry.entry_id,
            entry.scope_id,
            entry.relative_path,
            entry.size_bytes,
            entry.mtime_ns,
            entry.device_id,
            entry.inode,
            _historical_payload_sha256(entry),
        )
        candidate = SnapshotDocumentCandidate(rank, match_kind, suffix[1:].upper(), historical)
        matches.append(
            (
                rank,
                -(entry.mtime_ns or 0),
                entry.relative_path,
                candidate,
            )
        )
    matches.sort(key=lambda item: (item[0], item[1], item[2].encode("utf-8")))
    candidates = tuple(item[3] for item in matches[:max_documents])
    digest = _canonical_digest([asdict(item) for item in candidates])
    return SnapshotDocumentPlan(
        "VERIFIED_HISTORICAL_SNAPSHOT",
        snapshot.policy.value,
        query,
        tuple(item[1:].upper() for item in normalized_extensions),
        len(matches),
        len(candidates),
        len(matches) > len(candidates),
        digest,
        candidates,
    )


def plan_current_documents(
    session: StewardSession,
    scope: ResolvedScope,
    *,
    query: str,
    extensions: tuple[str, ...] | list[str] | None = None,
    max_documents: int = 4,
) -> CurrentDocumentPlan:
    """Plan bounded candidates from a fresh metadata-only Scope scan."""

    if type(max_documents) is not int or not 1 <= max_documents <= MAX_COLLECTION_DOCUMENTS:
        raise ValueError("collection document limit is invalid")
    discovery = search_current_documents(
        session.config,
        scope.scope,
        query=query,
        extensions=extensions,
        limit=max_documents,
    )
    normalized_query = normalize_document_query(query)
    candidates: list[CurrentDocumentCandidate] = []
    for item in discovery["candidates"]:
        matched = match_document_path(item["relative_path"], normalized_query)
        if matched is None:
            raise ValueError("current discovery candidate no longer matches")
        rank, match_kind = matched
        candidates.append(
            CurrentDocumentCandidate(
                rank,
                match_kind,
                item["source_format"],
                item["scope_id"],
                item["relative_path"],
                item["size_bytes"],
                item["mtime_ns"],
            )
        )
    values = tuple(candidates)
    return CurrentDocumentPlan(
        "CURRENT_SCOPE_DISCOVERY",
        scope.policy.value,
        query,
        tuple(discovery["extensions"]),
        discovery["matched_count"],
        len(values),
        discovery["has_more"],
        discovery["status"] == "PARTIAL",
        tuple(discovery["warnings"]),
        _canonical_digest([asdict(item) for item in values]),
        values,
    )


def revalidate_snapshot_document(
    session: StewardSession,
    candidate: SnapshotDocumentCandidate,
) -> CurrentDocumentIdentity:
    """Admit one historical candidate as a current file without reading its content."""

    historical = candidate.historical
    resolved = resolve_scoped_path(session, historical.scope_id, historical.relative_path)
    scope = next(item for item in session.config.scopes if item.scope_id == resolved.scope_id)
    path = scope.normalized_path.joinpath(*Path(resolved.relative_path).parts)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("current document candidate is not a regular file")
    suffix = path.suffix.casefold().lstrip(".").upper()
    if suffix == "JPG":
        suffix = "JPEG"
    elif suffix in {"TIF", "TIFF"}:
        suffix = "TIFF"
    if suffix != candidate.source_format:
        raise ValueError("current document format differs from the historical candidate")
    observed = (metadata.st_size, metadata.st_mtime_ns, metadata.st_dev, metadata.st_ino)
    expected = (
        historical.size_bytes,
        historical.mtime_ns,
        historical.device_id,
        historical.inode,
    )
    relation = "UNKNOWN" if any(value is None for value in expected) else (
        "METADATA_MATCH" if observed == expected else "METADATA_CHANGED"
    )
    return CurrentDocumentIdentity(
        "CURRENT_FILESYSTEM_DOCUMENT",
        resolved.scope_id,
        resolved.relative_path,
        suffix,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_dev,
        metadata.st_ino,
        relation,
        historical.payload_sha256,
    )


def revalidate_current_document(
    session: StewardSession,
    candidate: CurrentDocumentCandidate,
) -> CurrentDocumentIdentity:
    """Repeat current path admission after discovery and before content parsing."""

    resolved = resolve_scoped_path(session, candidate.scope_id, candidate.relative_path)
    scope = next(item for item in session.config.scopes if item.scope_id == resolved.scope_id)
    path = scope.normalized_path.joinpath(*Path(resolved.relative_path).parts)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("current document candidate is not a regular file")
    suffix = path.suffix.casefold().lstrip(".").upper()
    if suffix == "JPG":
        suffix = "JPEG"
    elif suffix in {"TIF", "TIFF"}:
        suffix = "TIFF"
    if suffix != candidate.source_format:
        raise ValueError("current document format differs from discovery")
    expected = (candidate.discovered_size_bytes, candidate.discovered_mtime_ns)
    observed = (metadata.st_size, metadata.st_mtime_ns)
    relation = "UNKNOWN" if any(value is None for value in expected) else (
        "METADATA_MATCH" if observed == expected else "METADATA_CHANGED"
    )
    return CurrentDocumentIdentity(
        "CURRENT_FILESYSTEM_DOCUMENT",
        resolved.scope_id,
        resolved.relative_path,
        suffix,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_dev,
        metadata.st_ino,
        relation,
        None,
    )


def document_collection_machine_object(
    value: (
        HistoricalDocumentIdentity
        | SnapshotDocumentPlan
        | SnapshotDocumentCandidate
        | CurrentDocumentPlan
        | CurrentDocumentCandidate
        | CurrentDocumentIdentity
    ),
) -> dict[str, Any]:
    """Return the bounded path-safe dataclass representation."""

    result = asdict(value)
    if not isinstance(result, dict):
        raise TypeError
    return result


__all__ = [
    "CurrentDocumentIdentity",
    "CurrentDocumentCandidate",
    "CurrentDocumentPlan",
    "HistoricalDocumentIdentity",
    "MAX_COLLECTION_DOCUMENTS",
    "SnapshotDocumentCandidate",
    "SnapshotDocumentPlan",
    "document_collection_machine_object",
    "document_collection_request_digest",
    "plan_snapshot_documents",
    "plan_current_documents",
    "revalidate_current_document",
    "revalidate_snapshot_document",
]
