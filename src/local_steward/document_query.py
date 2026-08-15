"""Deterministic document-query normalization with source-text backreferences.

The matching modes in this module are deliberately lexical. They repair
representation artifacts such as soft hyphens and line wrapping, but never
claim semantic similarity or OCR correction.
"""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata


EXACT_MATCH_MODE = "SUBSTRING_CASEFOLD_NFKC"
SOFT_HYPHEN_MATCH_MODE = "SUBSTRING_SOFT_HYPHEN_REMOVED"
DEHYPHENATED_MATCH_MODE = "SUBSTRING_LINEBREAK_DEHYPHENATED"
WHITESPACE_MATCH_MODE = "SUBSTRING_WHITESPACE_NORMALIZED"
MIXED_MATCH_MODE = "MIXED_DETERMINISTIC_NORMALIZATION"


@dataclass(frozen=True, slots=True)
class DocumentTextMatch:
    """One lexical match class and its exact source-text span."""

    count: int
    mode: str
    source_start: int
    source_end: int


def _folded_with_offsets(value: str) -> tuple[str, tuple[int, ...]]:
    folded: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(value):
        part = unicodedata.normalize("NFKC", char).casefold()
        folded.append(part)
        offsets.extend([index] * len(part))
    return "".join(folded), tuple(offsets)


def _remove_soft_hyphens(
    value: str, offsets: tuple[int, ...]
) -> tuple[str, tuple[int, ...]]:
    projected = [(char, offsets[index]) for index, char in enumerate(value) if char != "\u00ad"]
    return "".join(char for char, _offset in projected), tuple(
        offset for _char, offset in projected
    )


def _dehyphenate_linebreaks(
    value: str, offsets: tuple[int, ...]
) -> tuple[str, tuple[int, ...]]:
    chars: list[str] = []
    mapped: list[int] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char in {"-", "‐", "‑"} and chars and chars[-1].isalnum():
            cursor = index + 1
            while cursor < len(value) and value[cursor] in {" ", "\t"}:
                cursor += 1
            linebreak = cursor
            while cursor < len(value) and value[cursor] in {"\r", "\n"}:
                cursor += 1
            if cursor > linebreak:
                while cursor < len(value) and value[cursor] in {" ", "\t"}:
                    cursor += 1
                if cursor < len(value) and value[cursor].isalnum():
                    index = cursor
                    continue
        chars.append(char)
        mapped.append(offsets[index])
        index += 1
    return "".join(chars), tuple(mapped)


def _collapse_whitespace(
    value: str, offsets: tuple[int, ...]
) -> tuple[str, tuple[int, ...]]:
    chars: list[str] = []
    mapped: list[int] = []
    whitespace = False
    for index, char in enumerate(value):
        if char.isspace():
            if chars and not whitespace:
                chars.append(" ")
                mapped.append(offsets[index])
            whitespace = True
            continue
        chars.append(char)
        mapped.append(offsets[index])
        whitespace = False
    if chars and chars[-1] == " ":
        chars.pop()
        mapped.pop()
    return "".join(chars), tuple(mapped)


def _variant(
    value: str,
    *,
    remove_soft_hyphens: bool = False,
    dehyphenate: bool = False,
    collapse_whitespace: bool = False,
) -> tuple[str, tuple[int, ...]]:
    projected, offsets = _folded_with_offsets(value)
    if remove_soft_hyphens:
        projected, offsets = _remove_soft_hyphens(projected, offsets)
    if dehyphenate:
        projected, offsets = _dehyphenate_linebreaks(projected, offsets)
    if collapse_whitespace:
        projected, offsets = _collapse_whitespace(projected, offsets)
    return projected, offsets


def match_document_text(value: str | None, query: str) -> DocumentTextMatch | None:
    """Match one query using an ordered, bounded set of lossless text repairs."""

    if value is None or not query:
        return None
    strategies: tuple[tuple[str, dict[str, bool]], ...] = (
        (EXACT_MATCH_MODE, {}),
        (SOFT_HYPHEN_MATCH_MODE, {"remove_soft_hyphens": True}),
        (
            DEHYPHENATED_MATCH_MODE,
            {"remove_soft_hyphens": True, "dehyphenate": True},
        ),
        (
            WHITESPACE_MATCH_MODE,
            {
                "remove_soft_hyphens": True,
                "dehyphenate": True,
                "collapse_whitespace": True,
            },
        ),
    )
    for mode, options in strategies:
        projected_value, offsets = _variant(value, **options)
        projected_query, _query_offsets = _variant(query, **options)
        if not projected_query or not offsets:
            continue
        start = projected_value.find(projected_query)
        if start < 0:
            continue
        end = start + len(projected_query)
        source_start = offsets[start]
        source_end = offsets[end - 1] + 1
        return DocumentTextMatch(
            projected_value.count(projected_query),
            mode,
            source_start,
            source_end,
        )
    return None


def document_match_mode(modes: set[str]) -> str:
    """Return one stable packet-level mode without hiding mixed normalization."""

    if not modes:
        return EXACT_MATCH_MODE
    if len(modes) == 1:
        return next(iter(modes))
    return MIXED_MATCH_MODE


__all__ = [
    "DEHYPHENATED_MATCH_MODE",
    "DocumentTextMatch",
    "EXACT_MATCH_MODE",
    "MIXED_MATCH_MODE",
    "SOFT_HYPHEN_MATCH_MODE",
    "WHITESPACE_MATCH_MODE",
    "document_match_mode",
    "match_document_text",
]
