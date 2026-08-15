"""Single TOML configuration loader and schema validator."""

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .constants import DEFAULT_CONFIG_PATH, PROJECT_ROOT, SCHEMA_VERSION
from .errors import ConfigurationNotFoundError, ConfigurationSchemaError
from .models import PathConfig, ScopeConfig, ScopeRole, StewardConfig
from .paths import contains, normalize_path, overlaps
from .scopes import validate_scopes

_TOP_KEYS = {"schema_version", "project_name", "paths", "scopes"}
_PATH_KEYS = {"data_dir", "cache_dir", "evidence_dir", "quarantine_dir"}
_SCOPE_KEYS = {
    "scope_id",
    "role",
    "path",
    "enabled",
    "follow_directory_symlinks",
    "allow_cross_mount",
}
_SCOPE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def discover_config(explicit: Path | None = None) -> Path:
    """Select exactly one configuration path by frozen precedence."""
    if explicit is not None:
        return explicit.expanduser().resolve(strict=False)
    environment = os.environ.get("LOCAL_STEWARD_CONFIG")
    if environment:
        return Path(environment).expanduser().resolve(strict=False)
    return DEFAULT_CONFIG_PATH


def project_root_for(config_path: Path, explicit: bool) -> Path:
    """Infer a root from an explicit conventional config location for testability."""
    return config_path.parent.parent.resolve(strict=False) if explicit else PROJECT_ROOT


def _expect_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ConfigurationSchemaError(f"unknown {label} field(s): {', '.join(sorted(unknown))}")


def _internal_path(raw: Any, name: str, root: Path) -> Path:
    if not isinstance(raw, str):
        raise ConfigurationSchemaError(f"paths.{name} must be a string")
    value = normalize_path(raw, base_dir=root)
    if not contains(root, value):
        raise ConfigurationSchemaError(f"paths.{name} must stay inside project root")
    return value


def load_config(
    explicit_path: Path | None = None, *, project_root: Path | None = None
) -> StewardConfig:
    """Load and completely validate one TOML file without creating paths."""
    source = discover_config(explicit_path)
    if not source.is_file():
        raise ConfigurationNotFoundError(f"configuration not found: {source}")
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationSchemaError(f"invalid TOML: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigurationSchemaError("configuration must be a TOML table")
    _expect_keys(raw, _TOP_KEYS, "top-level")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationSchemaError("schema_version must be integer 1")
    if not isinstance(raw.get("project_name"), str) or not raw["project_name"]:
        raise ConfigurationSchemaError("project_name must be a non-empty string")
    paths_raw = raw.get("paths")
    if not isinstance(paths_raw, dict):
        raise ConfigurationSchemaError("[paths] is required")
    _expect_keys(paths_raw, _PATH_KEYS, "paths")
    if set(paths_raw) != _PATH_KEYS:
        raise ConfigurationSchemaError("[paths] must contain all internal directory fields")
    root = (project_root or project_root_for(source, explicit_path is not None)).resolve(
        strict=False
    )
    paths = PathConfig(
        *(
            _internal_path(paths_raw[name], name, root)
            for name in ("data_dir", "cache_dir", "evidence_dir", "quarantine_dir")
        )
    )
    if not all(
        contains(paths.data_dir, item)
        for item in (paths.cache_dir, paths.evidence_dir, paths.quarantine_dir)
    ):
        raise ConfigurationSchemaError("cache, evidence and quarantine must be inside data_dir")
    leaf_paths = (paths.cache_dir, paths.evidence_dir, paths.quarantine_dir)
    if any(
        overlaps(left, right)
        for index, left in enumerate(leaf_paths)
        for right in leaf_paths[index + 1 :]
    ):
        raise ConfigurationSchemaError("cache, evidence and quarantine must not overlap")
    scopes_raw = raw.get("scopes")
    if not isinstance(scopes_raw, list):
        raise ConfigurationSchemaError("at least one [[scopes]] table is required")
    scopes: list[ScopeConfig] = []
    for index, item in enumerate(scopes_raw):
        if not isinstance(item, dict):
            raise ConfigurationSchemaError(f"scopes[{index}] must be a table")
        _expect_keys(item, _SCOPE_KEYS, f"scopes[{index}]")
        if set(item) != _SCOPE_KEYS:
            raise ConfigurationSchemaError(f"scopes[{index}] must contain all required fields")
        scope_id = item["scope_id"]
        if not isinstance(scope_id, str) or not _SCOPE_ID.fullmatch(scope_id):
            raise ConfigurationSchemaError(f"invalid scope_id: {scope_id!r}")
        try:
            role = ScopeRole(item["role"])
        except (TypeError, ValueError) as error:
            raise ConfigurationSchemaError(f"invalid role for scope {scope_id}") from error
        booleans = ("enabled", "follow_directory_symlinks", "allow_cross_mount")
        if not all(isinstance(item[name], bool) for name in booleans):
            raise ConfigurationSchemaError(f"scope {scope_id} boolean fields must be boolean")
        path_value = item["path"]
        if not isinstance(path_value, str):
            raise ConfigurationSchemaError(f"scope {scope_id} path must be a string")
        scopes.append(
            ScopeConfig(
                scope_id,
                role,
                path_value,
                normalize_path(path_value),
                *(item[name] for name in booleans),
            )
        )
    warnings = validate_scopes(
        tuple(scopes), (paths.data_dir, paths.cache_dir, paths.evidence_dir, paths.quarantine_dir)
    )
    return StewardConfig(
        SCHEMA_VERSION, raw["project_name"], paths, tuple(scopes), root, source, warnings
    )
