"""Immutable filesystem-observation budget validation."""

from .errors import SnapshotBudgetError
from .models import ScanBudget


def make_budget(
    max_entries: int | None = None,
    max_total_size: int | None = None,
    max_duration_seconds: float | None = None,
    max_depth: int | None = None,
) -> ScanBudget:
    budget = ScanBudget(
        max_entries if max_entries is not None else 1_000_000,
        max_total_size,
        max_duration_seconds if max_duration_seconds is not None else 600.0,
        max_depth,
    )
    if (
        budget.max_entries < 1
        or (budget.max_total_stat_bytes is not None and budget.max_total_stat_bytes < 0)
        or budget.max_duration_seconds <= 0
        or (budget.max_depth is not None and budget.max_depth < 0)
    ):
        raise SnapshotBudgetError("invalid scan budget")
    return budget
