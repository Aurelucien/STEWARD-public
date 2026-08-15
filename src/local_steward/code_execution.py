"""Read-only repository grounding around Codex-owned code execution.

This module observes a fixed Git repository and returns a stateless baseline or
postflight packet.  It never accepts a command string, starts a shell, writes a
file, or treats caller-reported test results as verified execution evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any, Mapping, Sequence

from .models import StewardConfig
from .errors import (
    CodeExecutionBaselineError,
    CodeExecutionError,
    CodeExecutionRepositoryError,
    CodeExecutionResourceError,
)


CODE_EXECUTION_PACKET_SCHEMA_NAME = "local_steward.code_execution_packet"
CODE_EXECUTION_PACKET_SCHEMA_VERSION = 1
CODE_EXECUTION_PACKET_DIGEST_DOMAIN = "local_steward.code_execution_packet.v1"
MAX_TARGET_PATHS = 64
MAX_TARGET_PATH_CHARS = 256
MAX_STATUS_RECORDS = 256
MAX_STATUS_OUTPUT_BYTES = 256 * 1024
MAX_FILE_HASH_BYTES = 4 * 1024 * 1024
MAX_VALIDATION_CLAIMS = 32
MAX_CHECK_ID_CHARS = 128
GIT_TIMEOUT_SECONDS = 5.0
EXECUTION_RECEIPT_SCHEMA_NAME = "local_steward.execution_receipt"
EXECUTION_RECEIPT_SCHEMA_VERSION = 2

_PROTECTED_SIDEcars = ("state.db-wal", "state.db-shm", "state.db-journal")
_KNOWN_RESIDUE_ROOTS = (".pytest_cache", ".mypy_cache", ".ruff_cache")
_TOOLCHAIN_MARKERS = (
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
)
_VALIDATION_STATUSES = {"PASS", "FAIL", "SKIPPED", "NOT_RUN", "UNKNOWN"}
_RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
_REVIEW_CATEGORIES = (
    "SOURCE",
    "TEST",
    "DOCUMENTATION",
    "CONFIGURATION",
    "DEPENDENCY",
    "PUBLIC_API",
    "PLUGIN",
    "SCHEMA_OR_MIGRATION",
    "SECURITY_SENSITIVE",
    "GENERATED_ARTIFACT",
    "OTHER",
)
_REVIEW_OMISSIONS = (
    {
        "reason_code": "COMMAND_EXECUTION_NOT_OBSERVED",
        "text": "STEWARD does not observe or execute the Codex command stream.",
    },
    {
        "reason_code": "AUTOMATIC_ROLLBACK_NOT_AVAILABLE",
        "text": "Rollback remains a Codex-owned or user-approved VCS action.",
    },
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        CODE_EXECUTION_PACKET_DIGEST_DOMAIN.encode("utf-8") + b"\0" + _canonical(value)
    ).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_relative(raw: str) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_TARGET_PATH_CHARS:
        raise CodeExecutionError("relative code target is invalid")
    if "\x00" in raw or raw.startswith("/") or "\\" in raw:
        raise CodeExecutionError("code targets must be relative POSIX paths")
    path = PurePosixPath(raw)
    if any(part in {"", ".", ".."} for part in path.parts):
        if raw != "." and not (path.parts and all(part not in {".."} for part in path.parts)):
            raise CodeExecutionError("code target traversal is not allowed")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        return "."
    if normalized.startswith("../") or normalized == "..":
        raise CodeExecutionError("code target traversal is not allowed")
    return normalized


def _normalize_targets(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_TARGET_PATHS:
        raise CodeExecutionError("code target list is invalid")
    targets = {_safe_relative(item) for item in value}
    return tuple(sorted(targets))


def _under(path: str, parent: str) -> bool:
    if parent == ".":
        return True
    return path == parent or path.startswith(parent + "/")


def _target_contains(path: str, targets: tuple[str, ...]) -> bool:
    return not targets or any(_under(path, target) or _under(target, path) for target in targets)


def _visibility(status: str) -> str:
    if status == "!!":
        return "IGNORED"
    if status == "??":
        return "UNTRACKED"
    return "TRACKED"


def _run_git(root: Path, arguments: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CodeExecutionRepositoryError("fixed Git observation is unavailable") from error
    if completed.returncode != 0:
        raise CodeExecutionRepositoryError("configured project is not an observable Git repository")
    if len(completed.stdout) > MAX_STATUS_OUTPUT_BYTES:
        raise CodeExecutionResourceError("Git observation exceeds the bounded output budget")
    return completed.stdout


def _git_identity(root: Path) -> dict[str, Any]:
    top = _run_git(root, ("rev-parse", "--show-toplevel")).decode("utf-8", "replace").strip()
    try:
        top_path = Path(top).resolve(strict=False)
    except OSError as error:
        raise CodeExecutionRepositoryError("Git repository root is unavailable") from error
    if top_path != root.resolve(strict=False):
        raise CodeExecutionRepositoryError("Git repository root differs from the configured project")
    head = _run_git(root, ("rev-parse", "HEAD")).decode("ascii", "replace").strip()
    branch = _run_git(root, ("symbolic-ref", "--short", "-q", "HEAD")).decode(
        "utf-8", "replace"
    ).strip()
    return {
        "root_id": "PROJECT_ROOT",
        "head": head if head else None,
        "branch": branch or "DETACHED",
        "detached": not bool(branch),
    }


def _parse_status(raw: bytes) -> list[tuple[str, str]]:
    try:
        tokens = raw.decode("utf-8", "surrogateescape").split("\0")
    except UnicodeDecodeError as error:
        raise CodeExecutionRepositoryError("Git status encoding is unavailable") from error
    records: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4 or token[2] != " ":
            raise CodeExecutionRepositoryError("Git status record is malformed")
        status = token[:2]
        path = token[3:]
        if "R" in status or "C" in status:
            if index >= len(tokens) or not tokens[index]:
                raise CodeExecutionRepositoryError("Git rename record is incomplete")
            path = tokens[index]
            index += 1
        records.append((status, _safe_relative(path)))
    if len(records) > MAX_STATUS_RECORDS:
        raise CodeExecutionResourceError("Git status exceeds the bounded record budget")
    return records


def _protected_roots(config: StewardConfig, root: Path) -> dict[str, str]:
    values = {"git": root / ".git"}
    values.update(
        {
            "data": config.paths.data_dir,
            "cache": config.paths.cache_dir,
            "evidence": config.paths.evidence_dir,
            "quarantine": config.paths.quarantine_dir,
        }
    )
    result: dict[str, str] = {}
    for label, path in values.items():
        try:
            relative = path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
        except ValueError:
            continue
        result[label] = relative or "."
    return result


def _protected_label(path: str, protected: Mapping[str, str]) -> str | None:
    labels = sorted(
        (label for label, root in protected.items() if _under(path, root)),
        key=lambda item: (len(protected[item]), item),
        reverse=True,
    )
    return labels[0] if labels else None


def _content_observation(root: Path, relative: str, protected: Mapping[str, str]) -> dict[str, Any]:
    label = _protected_label(relative, protected)
    path = root / relative
    try:
        value = path.lstat()
    except OSError:
        return {"object_type": "missing", "size_bytes": None, "content_sha256": None}
    if label is not None:
        return {
            "object_type": "protected",
            "size_bytes": value.st_size,
            "content_sha256": None,
            "protected_area": label,
        }
    if path.is_symlink():
        return {"object_type": "symlink", "size_bytes": value.st_size, "content_sha256": None}
    if not path.is_file():
        return {"object_type": "non_regular", "size_bytes": value.st_size, "content_sha256": None}
    if value.st_size > MAX_FILE_HASH_BYTES:
        return {
            "object_type": "regular_file",
            "size_bytes": value.st_size,
            "content_sha256": None,
            "hash_status": "SIZE_LIMIT",
        }
    try:
        digest = _sha256_bytes(path.read_bytes())
    except OSError:
        digest = None
    return {
        "object_type": "regular_file",
        "size_bytes": value.st_size,
        "content_sha256": digest,
        "hash_status": "OBSERVED" if digest is not None else "UNAVAILABLE",
    }


def _observe_state(config: StewardConfig) -> dict[str, Any]:
    root = config.project_root.resolve(strict=False)
    identity = _git_identity(root)
    status_raw = _run_git(
        root,
        (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        ),
    )
    tracked_diff = _run_git(root, ("diff", "--binary", "--no-ext-diff", "--no-textconv"))
    index_diff = _run_git(
        root, ("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv")
    )
    protected = _protected_roots(config, root)
    records: list[dict[str, Any]] = []
    for status, relative in _parse_status(status_raw):
        record: dict[str, Any] = {"path": relative, "status": status}
        record["visibility"] = _visibility(status)
        record.update(_content_observation(root, relative, protected))
        record["protected"] = _protected_label(relative, protected)
        records.append(record)
    records.sort(key=lambda item: (item["path"], item["status"]))
    ignored_paths = [
        item["path"] for item in records if item.get("visibility") == "IGNORED"
    ]
    state_without_digest: dict[str, Any] = {
        "identity": identity,
        "dirty": any(item.get("visibility") != "IGNORED" for item in records),
        "ignored_dirty": bool(ignored_paths),
        "ignored_paths": ignored_paths,
        "status_digest": _sha256_bytes(status_raw),
        "tracked_diff_digest": _sha256_bytes(tracked_diff),
        "index_diff_digest": _sha256_bytes(index_diff),
        "records": records,
        "toolchain_markers": [name for name in _TOOLCHAIN_MARKERS if (root / name).is_file()],
        "protected_roots": protected,
        "sidecars": {
            name: (config.paths.data_dir / name).exists() for name in _PROTECTED_SIDEcars
        },
        "known_residue": {
            name: (root / name).exists() for name in _KNOWN_RESIDUE_ROOTS
        },
    }
    return {**state_without_digest, "state_digest": _digest(state_without_digest)}


def _claims(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_VALIDATION_CLAIMS:
        raise CodeExecutionError("validation claim list is invalid")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise CodeExecutionError("validation claim is invalid")
        check_id = item.get("check_id")
        status = item.get("status")
        if (
            not isinstance(check_id, str)
            or not check_id
            or len(check_id) > MAX_CHECK_ID_CHARS
            or not isinstance(status, str)
            or status not in _VALIDATION_STATUSES
        ):
            raise CodeExecutionError("validation claim is invalid")
        claim: dict[str, Any] = {
            "check_id": check_id,
            "status": status,
            "evidence_class": "CALLER_REPORTED",
            "verification": "NOT_VERIFIED",
        }
        exit_code = item.get("exit_code")
        if exit_code is not None:
            if isinstance(exit_code, bool) or not isinstance(exit_code, int) or abs(exit_code) > 255:
                raise CodeExecutionError("validation claim exit code is invalid")
            claim["exit_code"] = exit_code
        output_sha256 = item.get("output_sha256")
        if output_sha256 is not None:
            if not isinstance(output_sha256, str) or len(output_sha256) != 64:
                raise CodeExecutionError("validation claim output digest is invalid")
            try:
                int(output_sha256, 16)
            except ValueError as error:
                raise CodeExecutionError("validation claim output digest is invalid") from error
            claim["output_sha256"] = output_sha256
        result.append(claim)
    return result


def _delivery() -> dict[str, Any]:
    return {
        "response_contract": "CODE_EXECUTION_PACKET_V1",
        "required_answer_fields": [
            "scope",
            "observed_changes",
            "execution_receipt",
            "change_review",
            "reported_checks",
            "unverified_checks",
            "unknowns",
            "omissions",
            "rollback",
        ],
        "caller_reported_is_not_verified": True,
        "codex_remains_execution_owner": True,
        "automatic_rollback": False,
    }


def _execution_receipt(
    *,
    phase: str,
    packet_status: str,
    workspace: dict[str, Any],
    target_paths: tuple[str, ...],
    baseline: dict[str, Any] | None,
    changes: dict[str, Any],
    change_review: dict[str, Any],
    claims: list[dict[str, Any]],
    unknowns: list[dict[str, Any]],
    thread_attribution: dict[str, Any] | None,
) -> dict[str, Any]:
    identity = workspace.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    observed = {
        "project_root": "PROJECT_ROOT",
        "head": identity.get("head"),
        "branch": identity.get("branch"),
        "target_paths": list(target_paths),
        "target_policy": "PROJECT_ROOT" if not target_paths else "EXACT_RELATIVE_TARGETS",
        "change_status": changes.get("status", "NOT_APPLICABLE"),
        "change_review_status": change_review.get("status"),
        "change_review_risk": change_review.get("risk_level"),
        "changed_path_count": len(changes.get("changed_paths", [])),
        "protected_path_count": len(changes.get("protected_paths", [])),
        "ignored_artifact_change_count": len(changes.get("ignored_artifact_changes", [])),
        "observation_digest": workspace.get("state_digest"),
    }
    baseline_digest = baseline.get("baseline_digest") if isinstance(baseline, dict) else None
    receipt = {
        "schema_name": EXECUTION_RECEIPT_SCHEMA_NAME,
        "schema_version": EXECUTION_RECEIPT_SCHEMA_VERSION,
        "receipt_kind": "CODE_EXECUTION",
        "phase": phase,
        "packet_status": packet_status,
        "baseline_digest": baseline_digest,
        "observed": observed,
        "observed_facts": [
            "PROJECT_ROOT_IDENTITY",
            "GIT_IDENTITY",
            "GIT_STATUS",
            "PROTECTED_PATHS",
            "IGNORED_ARTIFACT_PATHS",
        ],
        "caller_reported_checks": claims,
        "unverified_checks": claims,
        "unknowns": unknowns,
        "omissions": list(_REVIEW_OMISSIONS),
        "boundary": {
            "codex_remains_execution_owner": True,
            "steward_executed_commands": False,
            "automatic_rollback": False,
            "persisted_as_evidence": False,
        },
    }
    if thread_attribution is not None:
        receipt["thread_attribution"] = thread_attribution
    return receipt


def _packet(
    *,
    phase: str,
    status: str,
    workspace: dict[str, Any],
    target_paths: tuple[str, ...],
    baseline: dict[str, Any] | None,
    changes: dict[str, Any],
    change_review: dict[str, Any],
    claims: list[dict[str, Any]],
    unknowns: list[dict[str, Any]],
    omissions: list[dict[str, Any]],
    thread_attribution: dict[str, Any] | None,
) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "schema_name": CODE_EXECUTION_PACKET_SCHEMA_NAME,
        "schema_version": CODE_EXECUTION_PACKET_SCHEMA_VERSION,
        "packet_kind": "CODE_EXECUTION",
        "packet_status": status,
        "phase": phase,
        "scope": {
            "root_id": "PROJECT_ROOT",
            "target_paths": list(target_paths),
            "target_policy": "PROJECT_ROOT" if not target_paths else "EXACT_RELATIVE_TARGETS",
        },
        "workspace": workspace,
        "baseline": baseline,
        "changes": changes,
        "change_review": change_review,
        "reported_checks": claims,
        "unverified_checks": claims,
        "unknowns": unknowns,
        "omissions": omissions,
        "rollback": {
            "automatic": False,
            "action": "Codex-owned diff review or user-approved VCS action",
        },
        "delivery": _delivery(),
    }
    packet["execution_receipt"] = _execution_receipt(
        phase=phase,
        packet_status=status,
        workspace=workspace,
        target_paths=target_paths,
        baseline=baseline,
        changes=changes,
        change_review=change_review,
        claims=claims,
        unknowns=unknowns,
        thread_attribution=thread_attribution,
    )
    packet["packet_digest"] = _digest(packet)
    return packet


def _baseline_core(config: StewardConfig, targets: tuple[str, ...], state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": CODE_EXECUTION_PACKET_SCHEMA_NAME,
        "schema_version": CODE_EXECUTION_PACKET_SCHEMA_VERSION,
        "root_id": "PROJECT_ROOT",
        "project_name": config.project_name,
        "target_paths": list(targets),
        "state": state,
    }


def _baseline_digest(baseline: dict[str, Any]) -> str:
    value = dict(baseline)
    value.pop("baseline_digest", None)
    return _digest(value)


def _record_map(records: object) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise CodeExecutionBaselineError("baseline records are unavailable")
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise CodeExecutionBaselineError("baseline records are malformed")
        result[record["path"]] = record
    return result


def _review_categories(path: str, visibility: str) -> list[str]:
    lowered = path.lower()
    categories: set[str] = set()
    if visibility == "IGNORED":
        categories.add("GENERATED_ARTIFACT")
    if lowered.startswith("tests/") or "/tests/" in lowered or lowered.startswith("test_"):
        categories.add("TEST")
    if lowered.endswith((".md", ".rst", ".txt")) or lowered.startswith("docs/"):
        categories.add("DOCUMENTATION")
    if lowered.endswith((".py", ".js", ".ts", ".tsx", ".java", ".go", ".rs")):
        categories.add("SOURCE")
    if lowered in {
        "pyproject.toml",
        "poetry.lock",
        "uv.lock",
        "requirements.txt",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "cargo.lock",
    }:
        categories.add("DEPENDENCY")
    if (
        lowered.startswith(("config/", ".codex/", ".agents/"))
        or lowered.endswith((".toml", ".ini", ".yaml", ".yml"))
        or lowered.endswith(".json")
    ):
        categories.add("CONFIGURATION")
    if lowered.startswith(("skills/", "plugins/")) or ".codex-plugin/" in lowered:
        categories.add("PLUGIN")
    if any(token in lowered for token in ("migration", "schema", "database", "snapshot")):
        categories.add("SCHEMA_OR_MIGRATION")
    if (
        lowered.startswith(".github/workflows/")
        or lowered.startswith(".git/hooks/")
        or any(token in lowered for token in (".env", "credential", "secret", "token"))
    ):
        categories.add("SECURITY_SENSITIVE")
    if lowered in {
        "src/local_steward/cli.py",
        "src/local_steward/__init__.py",
        "src/local_steward/file_agent/__init__.py",
        "src/local_steward/native_mcp_server/protocol.py",
    }:
        categories.add("PUBLIC_API")
    if not categories:
        categories.add("OTHER")
    return [category for category in _REVIEW_CATEGORIES if category in categories]


def _review_risk(categories: list[str]) -> str:
    if any(
        category in categories
        for category in (
            "SECURITY_SENSITIVE",
            "SCHEMA_OR_MIGRATION",
            "PUBLIC_API",
            "CONFIGURATION",
            "DEPENDENCY",
            "PLUGIN",
        )
    ):
        return "HIGH"
    if "SOURCE" in categories or "GENERATED_ARTIFACT" in categories:
        return "MEDIUM"
    return "LOW"


def _change_review(changes: dict[str, Any]) -> dict[str, Any]:
    changed = changes.get("changed_paths")
    if not isinstance(changed, list):
        changed = []
    items: list[dict[str, Any]] = []
    category_counts = {category: 0 for category in _REVIEW_CATEGORIES}
    highest = "LOW"
    artifact_changes: list[str] = []
    for value in changed:
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            continue
        record = value.get("after") or value.get("before") or {}
        visibility = record.get("visibility", "TRACKED") if isinstance(record, dict) else "TRACKED"
        categories = _review_categories(value["path"], visibility)
        risk = _review_risk(categories)
        if visibility == "IGNORED":
            artifact_changes.append(value["path"])
        for category in categories:
            category_counts[category] += 1
        if _RISK_ORDER[risk] > _RISK_ORDER[highest]:
            highest = risk
        items.append(
            {
                "path": value["path"],
                "change_type": value.get("change_type", "MODIFIED"),
                "visibility": visibility,
                "categories": categories,
                "risk": risk,
            }
        )
    protected = changes.get("protected_paths")
    protected_count = len(protected) if isinstance(protected, list) else 0
    if protected_count:
        highest = "HIGH"
    if not items and not protected_count:
        status = "NOT_APPLICABLE"
        risk_level = "NONE"
    else:
        status = "REVIEW_REQUIRED" if highest == "HIGH" or protected_count else "OBSERVED"
        risk_level = highest
    return {
        "schema_name": "local_steward.change_review",
        "schema_version": 1,
        "status": status,
        "basis": "DETERMINISTIC_PATH_STATUS_CLASSIFICATION",
        "risk_level": risk_level,
        "review_required": status == "REVIEW_REQUIRED",
        "items": items,
        "category_counts": {key: value for key, value in category_counts.items() if value},
        "artifact_changes": artifact_changes,
        "protected_change_count": protected_count,
    }


def _postflight_changes(
    baseline: dict[str, Any], state: dict[str, Any], targets: tuple[str, ...]
) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    old_state = baseline.get("state")
    if not isinstance(old_state, dict):
        raise CodeExecutionBaselineError("baseline state is unavailable")
    old_identity = old_state.get("identity")
    new_identity = state.get("identity")
    identity_drift = old_identity != new_identity
    old_records = _record_map(old_state.get("records"))
    new_records = _record_map(state.get("records"))
    paths = sorted(set(old_records) | set(new_records))
    changed: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    ignored_artifacts: list[str] = []
    untracked_paths: list[str] = []
    for path in paths:
        old = old_records.get(path)
        new = new_records.get(path)
        if old == new:
            continue
        item: dict[str, Any] = {"path": path, "change_type": "MODIFIED"}
        if old is None:
            item["change_type"] = "ADDED"
        elif new is None:
            item["change_type"] = "REMOVED"
        item["before"] = old
        item["after"] = new
        changed.append(item)
        visibility = (new or old or {}).get("visibility")
        if visibility == "IGNORED":
            ignored_artifacts.append(path)
        elif visibility == "UNTRACKED":
            untracked_paths.append(path)
        if not _target_contains(path, targets):
            unexpected.append(item)
        protected_label = (new or old or {}).get("protected")
        if protected_label is not None:
            protected.append({"path": path, "area": protected_label, "change_type": item["change_type"]})
    old_sidecars = old_state.get("sidecars", {})
    new_sidecars = state.get("sidecars", {})
    sidecar_changes = [
        name
        for name in _PROTECTED_SIDEcars
        if isinstance(old_sidecars, dict)
        and isinstance(new_sidecars, dict)
        and old_sidecars.get(name) != new_sidecars.get(name)
    ]
    for name in sidecar_changes:
        protected.append({"path": f"data/{name}", "area": "data", "change_type": "SIDECAR"})
    state_digest_changed = old_state.get("state_digest") != state.get("state_digest")
    if identity_drift:
        status = "IDENTITY_DRIFT"
    elif protected:
        status = "PROTECTED_CHANGE"
    elif unexpected:
        status = "SCOPE_DRIFT"
    elif changed or state_digest_changed:
        status = "CHANGED"
    else:
        status = "NO_NET_CHANGE"
    changes = {
        "status": status,
        "identity_drift": identity_drift,
        "changed_paths": changed,
        "unexpected_paths": unexpected,
        "protected_paths": protected,
        "ignored_artifact_changes": sorted(ignored_artifacts),
        "untracked_changes": sorted(untracked_paths),
        "sidecar_changes": sidecar_changes,
        "state_digest_changed": state_digest_changed,
        "before_state_digest": old_state.get("state_digest"),
        "after_state_digest": state.get("state_digest"),
    }
    unknowns: list[dict[str, Any]] = []
    if state.get("records") and any(item.get("content_sha256") is None for item in state["records"]):
        unknowns.append(
            {
                "id": "unknown:code_execution:content_digest",
                "reason_code": "CONTENT_DIGEST_UNAVAILABLE",
                "text": "At least one changed path did not receive a content digest.",
            }
        )
    return status, changes, unknowns, protected


def build_code_execution_packet(
    config: StewardConfig,
    *,
    phase: str,
    target_paths: object = None,
    baseline: object = None,
    validation_claims: object = None,
    thread_attribution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one stateless preflight or postflight packet for the project repo."""
    if phase not in {"PREFLIGHT", "POSTFLIGHT"}:
        raise CodeExecutionError("code execution phase is invalid")
    targets = _normalize_targets(target_paths)
    root = config.project_root.resolve(strict=False)
    protected = _protected_roots(config, root)
    if any(target != "." and _protected_label(target, protected) is not None for target in targets):
        raise CodeExecutionError("protected code target is not admissible")
    state = _observe_state(config)
    claims = _claims(validation_claims)
    if phase == "PREFLIGHT":
        if baseline is not None:
            raise CodeExecutionBaselineError("PREFLIGHT does not accept a baseline")
        baseline_core = _baseline_core(config, targets, state)
        baseline_value = {**baseline_core, "baseline_digest": _baseline_digest(baseline_core)}
        change_review = {
            "schema_name": "local_steward.change_review",
            "schema_version": 1,
            "status": "NOT_APPLICABLE",
            "basis": "DETERMINISTIC_PATH_STATUS_CLASSIFICATION",
            "risk_level": "NONE",
            "review_required": False,
            "items": [],
            "category_counts": {},
            "artifact_changes": [],
            "protected_change_count": 0,
        }
        return _packet(
            phase=phase,
            status="READY",
            workspace=state,
            target_paths=targets,
            baseline=baseline_value,
            changes={"status": "NOT_APPLICABLE"},
            change_review=change_review,
            claims=claims,
            unknowns=[],
            omissions=[],
            thread_attribution=thread_attribution,
        )
    if not isinstance(baseline, dict):
        raise CodeExecutionBaselineError("POSTFLIGHT requires a preflight baseline")
    if (
        baseline.get("schema_name") != CODE_EXECUTION_PACKET_SCHEMA_NAME
        or baseline.get("schema_version") != CODE_EXECUTION_PACKET_SCHEMA_VERSION
        or baseline.get("root_id") != "PROJECT_ROOT"
        or baseline.get("project_name") != config.project_name
    ):
        raise CodeExecutionBaselineError("POSTFLIGHT baseline identity does not match the project")
    supplied_digest = baseline.get("baseline_digest")
    if not isinstance(supplied_digest, str) or supplied_digest != _baseline_digest(baseline):
        raise CodeExecutionBaselineError("POSTFLIGHT baseline digest does not match")
    baseline_targets = _normalize_targets(baseline.get("target_paths"))
    if target_paths is not None and baseline_targets != targets:
        raise CodeExecutionBaselineError("POSTFLIGHT target scope differs from the baseline")
    status, changes, unknowns, _protected = _postflight_changes(baseline, state, baseline_targets)
    change_review = _change_review(changes)
    return _packet(
        phase=phase,
        status=status,
        workspace=state,
        target_paths=baseline_targets,
        baseline={
            "baseline_digest": supplied_digest,
            "state_digest": baseline.get("state", {}).get("state_digest"),
        },
        changes=changes,
        change_review=change_review,
        claims=claims,
        unknowns=unknowns,
        omissions=[],
        thread_attribution=thread_attribution,
    )


__all__ = [
    "CODE_EXECUTION_PACKET_DIGEST_DOMAIN",
    "CODE_EXECUTION_PACKET_SCHEMA_NAME",
    "CODE_EXECUTION_PACKET_SCHEMA_VERSION",
    "build_code_execution_packet",
]
