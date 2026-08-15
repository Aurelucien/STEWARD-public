"""Bounded current-document discovery for the native STEWARD read surface.

Discovery returns safe Scope-relative identities only.  It never opens document
content, follows directory symlinks or chooses a candidate on the caller's
behalf; a unique candidate may be handed directly to the existing parser by
the native adapter.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
from unicodedata import normalize

from .filesystem import scan
from .models import (
    FilesystemObjectType,
    ScanBudget,
    ScopeConfig,
    StewardConfig,
)


DOCUMENT_DISCOVERY_MAX_QUERY_BYTES = 512
DOCUMENT_DISCOVERY_MAX_ENTRIES = 50_000
DOCUMENT_DISCOVERY_MAX_RESULTS = 50
DOCUMENT_DISCOVERY_MAX_SECONDS = 5.0
SUPPORTED_DOCUMENT_EXTENSIONS = (
    ".pdf",
    ".epub",
    ".docx",
    ".xlsx",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".wav",
    ".flac",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".webm",
)
_QUERY_TOKEN_PATTERN = re.compile(r"[^\w.-]+", re.UNICODE)
_CANONICAL_FORMAT_BY_EXTENSION = {".m4v": "MP4"}


def normalize_document_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("document search query is required")
    if "\x00" in query or any(ord(character) < 32 for character in query):
        raise ValueError("document search query is invalid")
    if len(query.encode("utf-8")) > DOCUMENT_DISCOVERY_MAX_QUERY_BYTES:
        raise ValueError("document search query is too long")
    return normalize("NFKC", query.strip()).casefold().replace("\\", "/")


def normalize_document_extensions(
    extensions: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    if extensions is None:
        return SUPPORTED_DOCUMENT_EXTENSIONS
    normalized = tuple(sorted({f".{item.casefold().lstrip('.') }" for item in extensions}))
    if not normalized or any(item not in SUPPORTED_DOCUMENT_EXTENSIONS for item in normalized):
        raise ValueError("document search extension is unsupported")
    return normalized


def match_document_path(relative_path: str, query: str) -> tuple[int, str] | None:
    path = normalize("NFKC", relative_path).casefold()
    basename = normalize("NFKC", Path(relative_path).name).casefold()
    if basename == query:
        return 0, "BASENAME_EXACT"
    if basename.startswith(query):
        return 1, "BASENAME_PREFIX"
    if query in path:
        return 2, "PATH_CONTAINS"
    tokens = tuple(token for token in _QUERY_TOKEN_PATTERN.split(query) if token)
    if tokens and all(token in path for token in tokens):
        return 3, "TOKEN_MATCH"
    return None


def search_current_documents(
    config: StewardConfig,
    scope: ScopeConfig,
    *,
    query: str,
    extensions: tuple[str, ...] | list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return bounded current document candidates without reading their contents."""
    normalized_query = normalize_document_query(query)
    normalized_extensions = normalize_document_extensions(extensions)
    if type(limit) is not int or not 1 <= limit <= DOCUMENT_DISCOVERY_MAX_RESULTS:
        raise ValueError("document search limit is invalid")

    entries, partial = scan(
        config,
        "document-discovery",
        (scope,),
        ScanBudget(
            max_entries=DOCUMENT_DISCOVERY_MAX_ENTRIES,
            max_duration_seconds=DOCUMENT_DISCOVERY_MAX_SECONDS,
        ),
    )
    matches: list[tuple[int, int, str, dict[str, Any]]] = []
    examined = 0
    for entry in entries:
        if entry.relative_path == "." or entry.excluded:
            continue
        examined += 1
        if entry.object_type != FilesystemObjectType.REGULAR_FILE:
            continue
        suffix = Path(entry.relative_path).suffix.casefold()
        if suffix not in normalized_extensions:
            continue
        matched = match_document_path(entry.relative_path, normalized_query)
        if matched is None:
            continue
        rank, match_kind = matched
        matches.append(
            (
                rank,
                -(entry.mtime_ns or 0),
                entry.relative_path,
                {
                    "scope_id": entry.scope_id,
                    "relative_path": entry.relative_path,
                    "source_format": _CANONICAL_FORMAT_BY_EXTENSION.get(
                        suffix, suffix[1:].upper()
                    ),
                    "size_bytes": entry.size_bytes,
                    "mtime_ns": entry.mtime_ns,
                    "readable": entry.readable,
                    "match_kind": match_kind,
                },
            )
        )
    matches.sort(key=lambda item: (item[0], item[1], item[2].encode("utf-8", "surrogateescape")))
    candidates = [item[3] for item in matches[:limit]]
    return {
        "status": "PARTIAL" if partial else "COMPLETE",
        "scope_id": scope.scope_id,
        "query": query,
        "extensions": [item[1:].upper() for item in normalized_extensions],
        "examined_entries": examined,
        "matched_count": len(matches),
        "returned_count": len(candidates),
        "has_more": len(matches) > limit,
        "candidates": candidates,
        "warnings": ["DISCOVERY_BUDGET_REACHED"] if partial else [],
    }


__all__ = [
    "DOCUMENT_DISCOVERY_MAX_ENTRIES",
    "DOCUMENT_DISCOVERY_MAX_QUERY_BYTES",
    "DOCUMENT_DISCOVERY_MAX_RESULTS",
    "SUPPORTED_DOCUMENT_EXTENSIONS",
    "match_document_path",
    "normalize_document_extensions",
    "normalize_document_query",
    "search_current_documents",
]
