"""Operation-scoped host-file admission for the Codex native read surface."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import stat

from ..agent_session import StewardSession
from ..agent_session.errors import StewardPathResolutionError
from ..document_discovery import SUPPORTED_DOCUMENT_EXTENSIONS
from ..models import ScopeConfig, ScopeRole, StewardConfig
from ..paths import canonicalize_host_absolute_path, contains


HOST_FILE_SCOPE_ID = "steward_host_file"
HOST_FILE_SELECTION_POLICY = "HOST_AUTHORIZED_EXACT_PATH"
_MAX_PATH_BYTES = 16_384


@dataclass(frozen=True, slots=True)
class HostFileBinding:
    """One non-persistent exact-file binding used by a single native operation."""

    config: StewardConfig
    scope_id: str
    relative_path: str


def _validate_text(absolute_path: str) -> Path:
    if (
        not isinstance(absolute_path, str)
        or not absolute_path
        or "\x00" in absolute_path
        or any(ord(character) < 32 for character in absolute_path)
    ):
        raise StewardPathResolutionError("absolute host file path is invalid")
    try:
        bounded = len(absolute_path.encode("utf-8")) <= _MAX_PATH_BYTES
    except UnicodeEncodeError:
        bounded = False
    candidate = Path(absolute_path)
    if not bounded or not candidate.is_absolute() or ".." in candidate.parts:
        raise StewardPathResolutionError("absolute host file path is invalid")
    return candidate


def _validate_components(candidate: Path) -> None:
    current = Path(candidate.anchor)
    try:
        for component in candidate.parts[1:]:
            current = current / component
            state = current.lstat()
            if stat.S_ISLNK(state.st_mode):
                raise StewardPathResolutionError("host file path contains a symbolic link")
        state = candidate.lstat()
    except StewardPathResolutionError:
        raise
    except OSError as error:
        raise StewardPathResolutionError("host file is unavailable") from error
    if not stat.S_ISREG(state.st_mode) or not os.access(candidate, os.R_OK):
        raise StewardPathResolutionError("host path is not a readable regular file")
    if candidate.suffix.casefold() not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise StewardPathResolutionError("host file format is unsupported")


def admit_host_absolute_file(session: StewardSession, absolute_path: str) -> HostFileBinding:
    """Bind one exact user/host-selected file without changing persistent Scope policy."""

    candidate = _validate_text(absolute_path)
    candidate = canonicalize_host_absolute_path(candidate)
    _validate_components(candidate)
    config = session.config
    protected = (
        config.paths.data_dir,
        config.paths.cache_dir,
        config.paths.evidence_dir,
        config.paths.quarantine_dir,
    )
    if any(contains(root, candidate) for root in protected):
        raise StewardPathResolutionError("host file is inside STEWARD internal data")
    if any(
        scope.enabled
        and scope.role == ScopeRole.EXCLUDED_ROOT
        and contains(scope.normalized_path, candidate)
        for scope in config.scopes
    ):
        raise StewardPathResolutionError("host file is inside a configured exclusion")

    parent = candidate.parent
    scope = ScopeConfig(
        HOST_FILE_SCOPE_ID,
        ScopeRole.REFERENCE_ROOT,
        str(parent),
        parent,
        True,
        False,
        False,
    )
    return HostFileBinding(
        replace(config, scopes=(scope,)),
        HOST_FILE_SCOPE_ID,
        candidate.name,
    )


__all__ = [
    "HOST_FILE_SCOPE_ID",
    "HOST_FILE_SELECTION_POLICY",
    "HostFileBinding",
    "admit_host_absolute_file",
]
