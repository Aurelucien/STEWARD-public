"""Dependency-free runtime failure contract shared by optional profiles."""


class RuntimeFailure(RuntimeError):
    """One safe, turn-local runtime failure classification."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


__all__ = ["RuntimeFailure"]
