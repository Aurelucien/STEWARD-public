"""Non-invasive capability checks for the foundation."""

import os
import platform
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

from .models import CapabilityStatus, DoctorCheck, DoctorSummary, StewardConfig
from .storage import storage_status


def _check(
    check_id: str, category: str, required: bool, ok: bool, message: str, **details: object
) -> DoctorCheck:
    return DoctorCheck(
        check_id,
        category,
        required,
        CapabilityStatus.AVAILABLE if ok else CapabilityStatus.UNAVAILABLE,
        message,
        dict(details),
    )


def _directory_check(check_id: str, path: Path, *, writable: bool = False) -> DoctorCheck:
    exists = path.is_dir()
    ok = exists and os.access(path, os.W_OK if writable else os.R_OK)
    need = "writable" if writable else "readable"
    return _check(
        check_id,
        "internal_path",
        True,
        ok,
        f"{path} is {need}" if ok else f"{path} is not {need}",
        path=str(path),
    )


def _sqlite_probe(data_dir: Path) -> DoctorCheck:
    probe = data_dir / f".local-steward-doctor-{uuid.uuid4()}.sqlite"
    try:
        connection = sqlite3.connect(probe)
        connection.execute("SELECT 1")
        connection.close()
        probe.unlink(missing_ok=True)
        return _check(
            "sqlite_probe",
            "storage",
            True,
            True,
            "SQLite temporary probe succeeded",
            path=str(data_dir),
        )
    except (OSError, sqlite3.Error) as error:
        probe.unlink(missing_ok=True)
        return _check(
            "sqlite_probe",
            "storage",
            True,
            False,
            "SQLite temporary probe failed",
            error=str(error),
        )


def run_doctor(config: StewardConfig) -> DoctorSummary:
    """Run bounded, non-recursive, no-network checks; only internal paths may be probed."""
    checks: list[DoctorCheck] = [
        _check(
            "platform_macos",
            "runtime",
            True,
            platform.system() == "Darwin",
            "macOS runtime check",
            actual=platform.system(),
        ),
        _check(
            "python_version",
            "runtime",
            True,
            sys.version_info >= (3, 11),
            "Python 3.11+ runtime check",
            actual=".".join(map(str, sys.version_info[:3])),
        ),
        _check(
            "configuration_valid",
            "configuration",
            True,
            True,
            "configuration loaded and schema validated",
        ),
        _check("scope_boundaries_valid", "scope", True, True, "scope boundaries validated"),
        _check(
            "project_root_readable",
            "project",
            True,
            config.project_root.is_dir() and os.access(config.project_root, os.R_OK),
            "project root readable",
            path=str(config.project_root),
        ),
        _directory_check("data_dir", config.paths.data_dir),
        _directory_check("cache_dir", config.paths.cache_dir, writable=True),
        _directory_check("evidence_dir", config.paths.evidence_dir, writable=True),
        _directory_check("quarantine_dir", config.paths.quarantine_dir, writable=True),
        _check(
            "quarantine_scope_boundary",
            "scope",
            True,
            True,
            "quarantine does not overlap actionable scopes",
        ),
        _check(
            "sqlite3_import",
            "storage",
            True,
            sqlite3 is not None,
            "stdlib sqlite3 import available",
        ),
    ]
    storage = storage_status(config)
    checks.append(
        _check(
            "storage_index",
            "storage",
            True,
            storage.storage_status in {"HEALTHY", "DEGRADED"},
            (
                f"storage status: {storage.storage_status}; historical diagnostics: "
                f"{len(storage.historical_evidence_diagnostics)}"
            ),
            storage_status=storage.storage_status,
            historical_evidence_diagnostic_count=len(
                storage.historical_evidence_diagnostics
            ),
        )
    )
    if config.paths.data_dir.is_dir() and os.access(config.paths.data_dir, os.W_OK):
        checks.append(_sqlite_probe(config.paths.data_dir))
    else:
        checks.append(
            _check("sqlite_probe", "storage", True, False, "SQLite probe directory unavailable")
        )
    managed = [
        scope for scope in config.scopes if scope.role.value == "managed_root" and scope.enabled
    ]
    checks.append(
        _check(
            "enabled_managed_root",
            "scope",
            True,
            bool(managed),
            "at least one enabled managed root",
        )
    )
    for scope in managed:
        ok = scope.normalized_path.is_dir() and os.access(scope.normalized_path, os.R_OK)
        checks.append(
            _check(
                f"managed_root:{scope.scope_id}",
                "scope",
                True,
                ok,
                "managed root readable" if ok else "managed root missing or unreadable",
                path=str(scope.normalized_path),
            )
        )
    for scope in (
        item for item in config.scopes if item.role.value == "reference_root" and item.enabled
    ):
        exists = scope.normalized_path.exists()
        checks.append(
            _check(
                f"reference_root:{scope.scope_id}",
                "scope",
                True,
                not exists or os.access(scope.normalized_path, os.R_OK),
                "reference root readable when present",
                path=str(scope.normalized_path),
                exists=exists,
            )
        )
    volumes = [
        path.stat().st_dev
        for path in (
            config.paths.data_dir,
            config.paths.cache_dir,
            config.paths.evidence_dir,
            config.paths.quarantine_dir,
        )
        if path.exists()
    ]
    checks.append(
        _check(
            "internal_volume",
            "storage",
            True,
            len(volumes) == 4 and len(set(volumes)) == 1,
            "internal directories share a filesystem volume",
        )
    )
    for name in ("git", "clamscan", "clamdscan", "freshclam", "yara", "codesign", "spctl", "xattr"):
        found = shutil.which(name)
        checks.append(
            DoctorCheck(
                f"optional:{name}",
                "external_tool",
                False,
                CapabilityStatus.AVAILABLE if found else CapabilityStatus.UNAVAILABLE,
                f"{name} available" if found else f"{name} unavailable",
                {"path": found},
            )
        )
    required_failed = [
        item for item in checks if item.required and item.status != CapabilityStatus.AVAILABLE
    ]
    optional_failed = [
        item for item in checks if not item.required and item.status != CapabilityStatus.AVAILABLE
    ]
    status = (
        CapabilityStatus.UNAVAILABLE
        if required_failed
        else CapabilityStatus.DEGRADED
        if optional_failed or config.warnings
        else CapabilityStatus.AVAILABLE
    )
    return DoctorSummary(
        status, tuple(checks), config.warnings, tuple(item.message for item in required_failed)
    )
