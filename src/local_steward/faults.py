"""Optional dependency-injection checkpoints for deterministic safety validation."""

from typing import Protocol


class FaultInjectionError(Exception):
    """A test-supplied failure at one declared safety boundary."""


class FaultInjector(Protocol):
    def inject(self, operation: str, stage: str) -> None:
        """Optionally raise FaultInjectionError for one declared operation stage."""


def checkpoint(injector: FaultInjector | None, operation: str, stage: str) -> None:
    """Invoke an optional fault injector; production callers pass no injector."""
    if injector is not None:
        injector.inject(operation, stage)
