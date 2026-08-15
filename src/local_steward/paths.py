"""Path normalization and containment primitives."""

import re
from pathlib import Path
import sys

from .errors import ConfigurationSchemaError


_MACOS_SYSTEM_PATH_ALIASES = {
    Path("/tmp"): Path("/private/tmp"),
    Path("/var"): Path("/private/var"),
    Path("/etc"): Path("/private/etc"),
}


def canonicalize_host_absolute_path(
    candidate: Path, *, platform_name: str | None = None
) -> Path:
    """Resolve only Apple's fixed root aliases, never arbitrary user symlinks."""
    if (platform_name or sys.platform) != "darwin":
        return candidate
    for alias, target in _MACOS_SYSTEM_PATH_ALIASES.items():
        try:
            relative = candidate.relative_to(alias)
        except ValueError:
            continue
        return target / relative
    return candidate


def normalize_path(raw_path: str, *, base_dir: Path | None = None) -> Path:
    """Expand only ``~`` and return a stable absolute pathlib path."""
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise ConfigurationSchemaError("path must be a non-empty string without NUL")
    if re.search(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|%[^%]+%", raw_path):
        raise ConfigurationSchemaError("paths may not contain unresolved environment variables")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        if base_dir is None:
            raise ConfigurationSchemaError("relative path requires a project root")
        candidate = base_dir / candidate
    return candidate.resolve(strict=False)


def contains(parent: Path, child: Path) -> bool:
    """Return whether paths are equal or parent lexically contains child."""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def overlaps(first: Path, second: Path) -> bool:
    """Return whether either path contains the other."""
    return contains(first, second) or contains(second, first)
