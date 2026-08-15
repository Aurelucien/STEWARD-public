"""Deterministic, provider-free routing for natural historical questions."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


AUTO_CONTEXT_PROFILE = "AUTO"
_CHANGE_TERMS = (
    "change",
    "changed",
    "compare",
    "comparison",
    "difference",
    "diff",
    "before",
    "after",
    "变化",
    "变更",
    "差异",
    "比较",
    "对比",
    "更新",
    "新增",
    "删除",
    "演变",
)
_STRUCTURE_TERMS = (
    "structure",
    "directory",
    "directories",
    "folder",
    "folders",
    "tree",
    "hierarchy",
    "layout",
    "organization",
    "目录",
    "结构",
    "文件夹",
    "层级",
    "树状",
)
_TERM_BOUNDARY = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ContextProfileDecision:
    requested_profile: str
    selected_profile: str
    reason_code: str
    matched_terms: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "requested_profile": self.requested_profile,
            "selected_profile": self.selected_profile,
            "reason_code": self.reason_code,
            "matched_terms": list(self.matched_terms),
        }


def select_context_profile(question: str) -> ContextProfileDecision:
    """Select one existing projection profile without model/provider inference."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("automatic Context Projection routing requires a question")
    normalized = unicodedata.normalize("NFKC", question).casefold()
    tokens = set(token for token in _TERM_BOUNDARY.split(normalized) if token)
    change = tuple(term for term in _CHANGE_TERMS if term in tokens or term in normalized)
    if change:
        return ContextProfileDecision(AUTO_CONTEXT_PROFILE, "CHANGE_TRIAGE", "QUESTION_CHANGE", change)
    structure = tuple(term for term in _STRUCTURE_TERMS if term in tokens or term in normalized)
    if structure:
        return ContextProfileDecision(
            AUTO_CONTEXT_PROFILE, "STRUCTURE_OVERVIEW", "QUESTION_STRUCTURE", structure
        )
    return ContextProfileDecision(AUTO_CONTEXT_PROFILE, "GENERAL", "QUESTION_GENERAL", ())


__all__ = ["AUTO_CONTEXT_PROFILE", "ContextProfileDecision", "select_context_profile"]
