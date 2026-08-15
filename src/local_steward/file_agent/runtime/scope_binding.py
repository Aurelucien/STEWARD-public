"""Turn-local host-owned bindings from Steward scopes to MCP roots."""

from dataclasses import dataclass
from pathlib import Path

from .failures import RuntimeFailure


@dataclass(frozen=True, slots=True)
class ScopeBinding:
    scope_id: str
    allowed_root: Path

    def resolve_relative_path(self, relative_path: str) -> str:
        """Lexically join a valid historical relative path to the bound root."""
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or relative_path.startswith("/")
            or "\x00" in relative_path
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        ):
            raise RuntimeFailure("SCOPE_BINDING_FAILED", "historical relative path is invalid")
        return str(self.allowed_root / relative_path)


class ScopeBindings:
    """Default-deny host input; mappings are not persisted or model-controlled."""

    def __init__(
        self,
        bindings: tuple[ScopeBinding, ...],
        allowed_directories: tuple[str, ...],
        enabled_scope_ids: tuple[str, ...] | None = None,
    ) -> None:
        roots = {value for value in allowed_directories}
        enabled = None if enabled_scope_ids is None else set(enabled_scope_ids)
        indexed: dict[str, ScopeBinding] = {}
        for binding in bindings:
            root = str(binding.allowed_root)
            if (
                binding.scope_id in indexed
                or root not in roots
                or (enabled is not None and binding.scope_id not in enabled)
            ):
                raise RuntimeFailure(
                    "SCOPE_BINDING_FAILED", "scope binding is not an MCP allowed root"
                )
            indexed[binding.scope_id] = binding
        self._bindings = indexed

    def require(self, scope_id: str) -> ScopeBinding:
        binding = self._bindings.get(scope_id)
        if binding is None:
            raise RuntimeFailure("SCOPE_BINDING_FAILED", "scope binding is unavailable")
        return binding
