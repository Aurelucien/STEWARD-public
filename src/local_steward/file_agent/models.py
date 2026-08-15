"""Framework-neutral models for the read-only Steward Agent facade."""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Any

from ..models import StewardConfig


class SourceKind(str, Enum):
    HISTORICAL_SNAPSHOT = "HISTORICAL_SNAPSHOT"
    HISTORICAL_SNAPSHOT_PAIR = "HISTORICAL_SNAPSHOT_PAIR"
    DERIVED_STRUCTURE = "DERIVED_STRUCTURE"
    DERIVED_GROWTH = "DERIVED_GROWTH"
    DERIVED_DUPLICATE = "DERIVED_DUPLICATE"
    DERIVED_RELATION = "DERIVED_RELATION"
    DERIVED_PROJECTION = "DERIVED_PROJECTION"


class ToolResultStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL_RESULT = "PARTIAL_RESULT"


@dataclass(frozen=True, slots=True)
class ToolBudgetLimits:
    max_steward_calls_per_turn: int | None = None
    max_items_per_call: int | None = None
    max_items_per_turn: int | None = None
    max_serialized_bytes_per_call: int | None = None
    max_serialized_bytes_per_turn: int | None = None
    max_depth: int | None = None
    max_elapsed_ms_per_call: int | None = None
    max_elapsed_ms_per_turn: int | None = None


@dataclass(frozen=True, slots=True)
class ToolBudgetUsage:
    calls: int = 0
    items: int = 0
    serialized_bytes: int = 0
    elapsed_ms: int = 0


@dataclass(frozen=True, slots=True)
class ToolBudgetReport:
    limits: ToolBudgetLimits
    consumed: ToolBudgetUsage
    remaining_calls: int | None
    remaining_items: int | None
    remaining_serialized_bytes: int | None
    remaining_elapsed_ms: int | None


@dataclass(frozen=True, slots=True)
class AgentToolEnvelope:
    tool_name: str
    source_kind: SourceKind
    snapshot_ids: tuple[str, ...]
    scope_id: str | None
    result: Any
    result_digest: str | None
    entries_examined: int | None
    entries_returned: int | None
    serialized_bytes: int
    elapsed_ms: int
    truncated: bool
    limit: int | None
    offset: int | None
    warnings: tuple[str, ...]
    status: ToolResultStatus
    budget: ToolBudgetReport


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Explicit per-turn dependencies; it contains no process-global state."""

    config: StewardConfig
    budget: "SharedToolBudget"


class SharedToolBudget:
    """Mutable only within one explicitly supplied execution context."""

    def __init__(self, limits: ToolBudgetLimits | None = None) -> None:
        self.limits = limits or ToolBudgetLimits()
        for field in fields(self.limits):
            value = getattr(self.limits, field.name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError("budget limits must be non-negative integers or None")
        self._usage = ToolBudgetUsage()

    @property
    def usage(self) -> ToolBudgetUsage:
        return self._usage

    def report(self) -> ToolBudgetReport:
        def remaining(limit: int | None, used: int) -> int | None:
            return None if limit is None else max(0, limit - used)

        return ToolBudgetReport(
            self.limits,
            self._usage,
            remaining(self.limits.max_steward_calls_per_turn, self._usage.calls),
            remaining(self.limits.max_items_per_turn, self._usage.items),
            remaining(self.limits.max_serialized_bytes_per_turn, self._usage.serialized_bytes),
            remaining(self.limits.max_elapsed_ms_per_turn, self._usage.elapsed_ms),
        )

    def begin_call(self, *, depth: int | None = None) -> None:
        if depth is not None and self.limits.max_depth is not None and depth > self.limits.max_depth:
            raise AgentToolError("QUERY_TOO_BROAD", "depth exceeds the configured tool budget")
        if self.limits.max_steward_calls_per_turn is not None and self._usage.calls >= self.limits.max_steward_calls_per_turn:
            raise AgentToolError("BUDGET_EXHAUSTED", "the per-turn tool-call budget is exhausted")
        self._usage = ToolBudgetUsage(
            self._usage.calls + 1,
            self._usage.items,
            self._usage.serialized_bytes,
            self._usage.elapsed_ms,
        )

    def effective_limit(self, requested: int | None) -> tuple[int | None, bool]:
        if requested is None:
            return None, False
        capped = requested
        if self.limits.max_items_per_call is not None:
            capped = min(capped, self.limits.max_items_per_call)
        if self.limits.max_items_per_turn is not None:
            capped = min(capped, max(0, self.limits.max_items_per_turn - self._usage.items))
        if capped < 1:
            raise AgentToolError("BUDGET_EXHAUSTED", "the item budget is exhausted")
        return capped, capped != requested

    def consume_result(self, *, items: int, serialized_bytes: int, elapsed_ms: int) -> None:
        if self.limits.max_items_per_call is not None and items > self.limits.max_items_per_call:
            raise AgentToolError("BUDGET_EXHAUSTED", "the per-call item budget is exhausted")
        if self.limits.max_serialized_bytes_per_call is not None and serialized_bytes > self.limits.max_serialized_bytes_per_call:
            raise AgentToolError("BUDGET_EXHAUSTED", "the per-call serialized-byte budget is exhausted")
        if self.limits.max_serialized_bytes_per_turn is not None and self._usage.serialized_bytes + serialized_bytes > self.limits.max_serialized_bytes_per_turn:
            raise AgentToolError("BUDGET_EXHAUSTED", "the per-turn serialized-byte budget is exhausted")
        if self.limits.max_elapsed_ms_per_call is not None and elapsed_ms > self.limits.max_elapsed_ms_per_call:
            raise AgentToolError("BUDGET_EXHAUSTED", "the per-call elapsed-time budget is exhausted")
        if self.limits.max_elapsed_ms_per_turn is not None and self._usage.elapsed_ms + elapsed_ms > self.limits.max_elapsed_ms_per_turn:
            raise AgentToolError("BUDGET_EXHAUSTED", "the per-turn elapsed-time budget is exhausted")
        if self.limits.max_items_per_turn is not None and self._usage.items + items > self.limits.max_items_per_turn:
            raise AgentToolError("BUDGET_EXHAUSTED", "the per-turn item budget is exhausted")
        self._usage = ToolBudgetUsage(
            self._usage.calls,
            self._usage.items + items,
            self._usage.serialized_bytes + serialized_bytes,
            self._usage.elapsed_ms + elapsed_ms,
        )


class AgentToolError(Exception):
    """Facade-local safe error; domain exceptions never become a new hierarchy."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")
