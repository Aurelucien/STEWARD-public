"""Scope safety validation; this is the sole scope policy implementation."""

from pathlib import Path

from .constants import SYSTEM_PROTECTED_PATHS
from .errors import ScopeValidationError
from .models import ScopeConfig, ScopeRole
from .paths import contains, overlaps


def validate_scopes(
    scopes: tuple[ScopeConfig, ...], internal_paths: tuple[Path, ...]
) -> tuple[str, ...]:
    """Validate deterministic overlap and protected-boundary rules."""
    warnings: list[str] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    actionable = (ScopeRole.MANAGED_ROOT, ScopeRole.REFERENCE_ROOT)
    for scope in scopes:
        if scope.scope_id in seen_ids:
            raise ScopeValidationError(f"duplicate scope_id: {scope.scope_id}")
        seen_ids.add(scope.scope_id)
        if scope.normalized_path in seen_paths:
            raise ScopeValidationError(f"duplicate normalized scope path: {scope.normalized_path}")
        seen_paths.add(scope.normalized_path)
        if scope.role in actionable:
            for protected in SYSTEM_PROTECTED_PATHS:
                # Root itself is protected, but it cannot make every user path protected.
                protected_overlap = (
                    scope.normalized_path == protected
                    if protected == Path("/")
                    else overlaps(scope.normalized_path, protected)
                )
                if protected_overlap:
                    raise ScopeValidationError(
                        f"scope {scope.scope_id} overlaps system_protected: {protected}"
                    )
            for internal in internal_paths:
                if overlaps(scope.normalized_path, internal):
                    raise ScopeValidationError(
                        f"scope {scope.scope_id} overlaps project internal path: {internal}"
                    )
    for index, left in enumerate(scopes):
        for right in scopes[index + 1 :]:
            if (
                left.role in actionable
                and right.role in actionable
                and overlaps(left.normalized_path, right.normalized_path)
            ):
                raise ScopeValidationError(
                    f"actionable scopes overlap: {left.scope_id}, {right.scope_id}"
                )
            if (
                left.role == ScopeRole.EXCLUDED_ROOT
                and right.role in actionable
                and contains(left.normalized_path, right.normalized_path)
            ):
                raise ScopeValidationError(
                    f"excluded scope {left.scope_id} contains actionable scope {right.scope_id}"
                )
            if (
                right.role == ScopeRole.EXCLUDED_ROOT
                and left.role in actionable
                and contains(right.normalized_path, left.normalized_path)
            ):
                raise ScopeValidationError(
                    f"excluded scope {right.scope_id} contains actionable scope {left.scope_id}"
                )
            if left.role == right.role == ScopeRole.EXCLUDED_ROOT and contains(
                left.normalized_path, right.normalized_path
            ):
                warnings.append(
                    f"redundant excluded scope: {right.scope_id} is inside {left.scope_id}"
                )
            if left.role == right.role == ScopeRole.EXCLUDED_ROOT and contains(
                right.normalized_path, left.normalized_path
            ):
                warnings.append(
                    f"redundant excluded scope: {left.scope_id} is inside {right.scope_id}"
                )
    return tuple(warnings)
