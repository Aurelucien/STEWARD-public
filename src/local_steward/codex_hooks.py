"""Fail-open Codex lifecycle observer for local Git workspaces.

The observer is loaded by the personal STEWARD plugin.  It binds to the Git
workspace reported by Codex hook input, records bounded metadata around local
tool use, and creates a compact execution receipt.  It never runs a user
command, stores prompt or command text, reads transcripts, or changes the
observed repository.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import time
from typing import Any

from .codex_identity import (
    HOOK_IDENTITY,
    MCP_SERVER_NAME,
    NATIVE_SERVER_VERSION,
    NATIVE_SURFACE_IDENTITY,
    PLUGIN_BASE_VERSION,
    PLUGIN_NAME,
    SKILL_NAME,
    integration_identity_machine_object,
    integration_identity_sha256,
)

try:  # pragma: no cover - exercised on supported macOS/Linux hosts
    import fcntl
except ImportError:  # pragma: no cover - fail-open portability fallback
    fcntl = None  # type: ignore[assignment]


HOOK_STATE_SCHEMA_NAME = "local_steward.codex_hook_turn_state"
HOOK_STATE_SCHEMA_VERSION = 2
RECEIPT_SCHEMA_NAME = "local_steward.codex_host_execution_receipt"
RECEIPT_SCHEMA_VERSION = 2
HANDOFF_SCHEMA_NAME = "local_steward.codex_workspace_handoff"
HANDOFF_SCHEMA_VERSION = 2
HOOK_RUNTIME_IDENTITY_SCHEMA_NAME = "local_steward.codex_hook_runtime_identity"
HOOK_RUNTIME_IDENTITY_SCHEMA_VERSION = 1

MAX_GIT_OUTPUT_BYTES = 1024 * 1024
MAX_STATUS_RECORDS = 512
MAX_PATH_CHARS = 1024
MAX_HASH_FILE_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 128
MAX_JSON_BYTES = 1024 * 1024
MAX_RECEIPT_PATHS = 128
GIT_TIMEOUT_SECONDS = 4.0
TURN_RETENTION_SECONDS = 2 * 24 * 60 * 60
RECEIPT_RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_RECEIPTS = 256
MAX_HANDOFFS = 128

_HEX_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")
_VALIDATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pytest", re.compile(r"(?:^|[\s;&|])(?:python\s+-m\s+)?pytest(?:\s|$)")),
    ("ruff", re.compile(r"(?:^|[\s;&|])ruff(?:\s+(?:check|format))?(?:\s|$)")),
    ("mypy", re.compile(r"(?:^|[\s;&|])mypy(?:\s|$)")),
    ("pip-check", re.compile(r"(?:^|[\s;&|])(?:python\s+-m\s+)?pip\s+check(?:\s|$)")),
    ("npm-test", re.compile(r"(?:^|[\s;&|])npm\s+(?:test|run\s+(?:test|lint))(?:\s|$)")),
    ("pnpm-test", re.compile(r"(?:^|[\s;&|])pnpm\s+(?:test|run\s+(?:test|lint))(?:\s|$)")),
    ("yarn-test", re.compile(r"(?:^|[\s;&|])yarn\s+(?:test|lint)(?:\s|$)")),
    ("cargo-test", re.compile(r"(?:^|[\s;&|])cargo\s+test(?:\s|$)")),
    ("go-test", re.compile(r"(?:^|[\s;&|])go\s+test(?:\s|$)")),
    ("make-test", re.compile(r"(?:^|[\s;&|])make\s+(?:test|check|lint)(?:\s|$)")),
)


JsonObject = dict[str, Any]


class _ObservationUnavailable(Exception):
    """A bounded Git observation could not be completed."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def implementation_sha256() -> str:
    """Return the source identity embedded in the trusted hook command."""

    return _sha256(Path(__file__).read_bytes())


def hook_runtime_identity() -> JsonObject:
    """Return the path-free generation identity persisted in receipts and handoffs."""

    body: JsonObject = {
        "schema_name": HOOK_RUNTIME_IDENTITY_SCHEMA_NAME,
        "schema_version": HOOK_RUNTIME_IDENTITY_SCHEMA_VERSION,
        "integration_identity": integration_identity_machine_object(),
        "integration_identity_sha256": integration_identity_sha256(),
        "hook_implementation_sha256": implementation_sha256(),
    }
    body["hook_generation_sha256"] = _sha256(_canonical_bytes(body))
    return body


def _bounded_digest(value: object) -> tuple[str | None, str]:
    try:
        payload = _canonical_bytes(value)
    except (TypeError, ValueError, UnicodeError):
        return None, "UNAVAILABLE"
    if len(payload) > MAX_JSON_BYTES:
        return None, "OMITTED_SIZE_LIMIT"
    return _sha256(payload), "OBSERVED"


def _ensure_state_root(raw: Path) -> Path:
    if not raw.is_absolute():
        raise ValueError("hook state root must be absolute")
    root = raw.resolve(strict=False)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = root.lstat()
    if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
        raise ValueError("hook state root is unavailable")
    try:
        root.chmod(0o700)
    except OSError:
        pass
    for name in ("turns", "receipts", "handoffs", "locks"):
        child = root / name
        child.mkdir(mode=0o700, exist_ok=True)
        child_value = child.lstat()
        if not stat.S_ISDIR(child_value.st_mode) or stat.S_ISLNK(child_value.st_mode):
            raise ValueError("hook state directory is unavailable")
    return root


def _load_or_create_key(root: Path) -> bytes:
    path = root / ".observer-key"
    try:
        value = path.lstat()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode):
        raise ValueError("hook signing key is unavailable")
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("hook signing key is invalid")
    return key


def _signature(key: bytes, domain: str, value: str) -> str:
    return hmac.new(key, f"{domain}\0{value}".encode(), hashlib.sha256).hexdigest()


def _safe_json_read(path: Path) -> JsonObject | None:
    try:
        value = path.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or value.st_size > MAX_JSON_BYTES
        ):
            return None
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    payload = _canonical_bytes(value) + b"\n"
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("hook state exceeds the bounded size")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _state_lock(root: Path, turn_signature: str) -> Iterator[None]:
    path = root / "locks" / f"{turn_signature}.lock"
    with path.open("a+b") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _git_environment() -> dict[str, str]:
    return {
        **os.environ,
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_git(cwd: Path, arguments: Sequence[str], *, allow_failure: bool = False) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _ObservationUnavailable from error
    if completed.returncode != 0:
        if allow_failure:
            return b""
        raise _ObservationUnavailable
    if len(completed.stdout) > MAX_GIT_OUTPUT_BYTES:
        raise _ObservationUnavailable
    return completed.stdout


def _safe_relative_path(value: str) -> str:
    if not value or len(value) > MAX_PATH_CHARS or "\x00" in value:
        raise _ObservationUnavailable
    normalized = PurePosixPath(value).as_posix()
    parts = PurePosixPath(normalized).parts
    if normalized.startswith("/") or not parts or any(part in {"", ".", ".."} for part in parts):
        raise _ObservationUnavailable
    return normalized


def _status_records(raw: bytes) -> list[tuple[str, str]]:
    tokens = raw.decode("utf-8", "replace").split("\0")
    records: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4 or token[2] != " ":
            raise _ObservationUnavailable
        status_value = token[:2]
        relative = token[3:]
        if "R" in status_value or "C" in status_value:
            if index >= len(tokens) or not tokens[index]:
                raise _ObservationUnavailable
            relative = tokens[index]
            index += 1
        records.append((status_value, _safe_relative_path(relative)))
    if len(records) > MAX_STATUS_RECORDS:
        raise _ObservationUnavailable
    return records


def _file_metadata(root: Path, relative: str) -> JsonObject:
    path = root / relative
    try:
        value = path.lstat()
    except OSError:
        return {"object_type": "missing"}
    result: JsonObject = {
        "size_bytes": value.st_size,
        "mtime_ns": value.st_mtime_ns,
    }
    if stat.S_ISLNK(value.st_mode):
        result["object_type"] = "symlink"
        return result
    if not stat.S_ISREG(value.st_mode):
        result["object_type"] = "non_regular"
        return result
    result["object_type"] = "regular_file"
    if value.st_size > MAX_HASH_FILE_BYTES:
        result["content_digest_status"] = "OMITTED_SIZE_LIMIT"
        return result
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            payload = stream.read(MAX_HASH_FILE_BYTES + 1)
    except OSError:
        result["content_digest_status"] = "UNAVAILABLE"
        return result
    if len(payload) > MAX_HASH_FILE_BYTES:
        result["content_digest_status"] = "OMITTED_SIZE_LIMIT"
    else:
        result["content_sha256"] = _sha256(payload)
        result["content_digest_status"] = "OBSERVED"
    return result


def _workspace_root(cwd: Path) -> Path:
    raw = _run_git(cwd, ("rev-parse", "--show-toplevel"))
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        raise _ObservationUnavailable
    root = Path(text).resolve(strict=True)
    value = root.lstat()
    if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
        raise _ObservationUnavailable
    try:
        cwd.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as error:
        raise _ObservationUnavailable from error
    return root


def _observe_workspace(cwd: Path, key: bytes) -> tuple[Path, JsonObject]:
    root = _workspace_root(cwd)
    status_raw = _run_git(
        root,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
    )
    head_raw = _run_git(root, ("rev-parse", "--verify", "HEAD"), allow_failure=True)
    branch_raw = _run_git(
        root,
        ("symbolic-ref", "--short", "-q", "HEAD"),
        allow_failure=True,
    )
    records: list[JsonObject] = []
    for status_value, relative in _status_records(status_raw):
        record: JsonObject = {
            "path": relative,
            "status": status_value,
            "visibility": "UNTRACKED" if status_value == "??" else "TRACKED",
        }
        record.update(_file_metadata(root, relative))
        records.append(record)
    records.sort(key=lambda item: (item["path"], item["status"]))
    head = head_raw.decode("ascii", "replace").strip() or None
    branch = branch_raw.decode("utf-8", "replace").strip() or "DETACHED"
    state: JsonObject = {
        "workspace_id": _signature(key, "workspace", str(root)),
        "workspace_name": root.name or "workspace",
        "head": head,
        "branch": branch,
        "detached": branch == "DETACHED",
        "dirty": bool(records),
        "status_sha256": _sha256(status_raw),
        "records": records,
    }
    state["state_digest"] = _sha256(_canonical_bytes(state))
    return root, state


def _event_cwd(payload: Mapping[str, object]) -> Path:
    raw_cwd = payload.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd:
        raise _ObservationUnavailable
    cwd = Path(raw_cwd)
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        candidate_raw = tool_input.get("workdir")
        if isinstance(candidate_raw, str) and candidate_raw:
            candidate = Path(candidate_raw)
            cwd = candidate if candidate.is_absolute() else cwd / candidate
    try:
        cwd = cwd.resolve(strict=True)
    except OSError as error:
        raise _ObservationUnavailable from error
    if not cwd.is_dir():
        raise _ObservationUnavailable
    return cwd


def _validation_checks(tool_name: str, tool_input: object) -> list[str]:
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return []
    command = tool_input.get("command")
    if not isinstance(command, str) or len(command) > 64 * 1024:
        return []
    lowered = command.lower()
    return [name for name, pattern in _VALIDATION_PATTERNS if pattern.search(lowered)]


def _find_exit_code(value: object, *, depth: int = 0) -> int | None:
    if depth > 3:
        return None
    if isinstance(value, dict):
        direct = value.get("exit_code")
        if isinstance(direct, int) and not isinstance(direct, bool):
            return direct
        for nested in value.values():
            found = _find_exit_code(nested, depth=depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value[:32]:
            found = _find_exit_code(nested, depth=depth + 1)
            if found is not None:
                return found
    return None


def _operation_status(tool_name: str, response: object) -> tuple[str, int | None]:
    exit_code = _find_exit_code(response)
    if exit_code is not None:
        return ("PASS" if exit_code == 0 else "FAIL"), exit_code
    if tool_name == "apply_patch" and response is not None:
        return "COMPLETED", None
    return "UNKNOWN", None


def _empty_turn_state(session_signature: str, turn_signature: str) -> JsonObject:
    runtime_identity = hook_runtime_identity()
    return {
        "schema_name": HOOK_STATE_SCHEMA_NAME,
        "schema_version": HOOK_STATE_SCHEMA_VERSION,
        "session_signature": session_signature,
        "turn_signature": turn_signature,
        "hook_generation_sha256": runtime_identity["hook_generation_sha256"],
        "workspaces": {},
        "events": [],
        "delivery_injected": False,
    }


def _load_turn_state(
    root: Path,
    session_signature: str,
    turn_signature: str,
) -> JsonObject:
    path = root / "turns" / f"{turn_signature}.json"
    value = _safe_json_read(path)
    if (
        value is None
        or value.get("schema_name") != HOOK_STATE_SCHEMA_NAME
        or value.get("schema_version") != HOOK_STATE_SCHEMA_VERSION
        or value.get("session_signature") != session_signature
        or value.get("turn_signature") != turn_signature
        or value.get("hook_generation_sha256")
        != hook_runtime_identity()["hook_generation_sha256"]
    ):
        return _empty_turn_state(session_signature, turn_signature)
    if not isinstance(value.get("workspaces"), dict) or not isinstance(value.get("events"), list):
        return _empty_turn_state(session_signature, turn_signature)
    return value


def _save_turn_state(root: Path, turn_signature: str, value: Mapping[str, object]) -> None:
    _atomic_json_write(root / "turns" / f"{turn_signature}.json", value)


def _event_signature(key: bytes, payload: Mapping[str, object]) -> str:
    value = payload.get("tool_use_id")
    if not isinstance(value, str) or not value:
        digest, _status = _bounded_digest(payload.get("tool_input"))
        value = digest or "unavailable"
    return _signature(key, "tool-use", value)


def _workspace_record(
    turn_state: JsonObject,
    root: Path,
    observed: JsonObject,
) -> JsonObject:
    workspaces = turn_state["workspaces"]
    if not isinstance(workspaces, dict):
        raise ValueError
    workspace_id = observed["workspace_id"]
    current = workspaces.get(workspace_id)
    if not isinstance(current, dict):
        current = {
            "workspace_root_private": str(root),
            "baseline": observed,
            "latest": observed,
        }
        workspaces[workspace_id] = current
    else:
        current["latest"] = observed
    return current


def _pre_tool_use(
    payload: Mapping[str, object],
    root: Path,
    key: bytes,
    session_signature: str,
    turn_signature: str,
) -> None:
    cwd = _event_cwd(payload)
    workspace_root, observed = _observe_workspace(cwd, key)
    with _state_lock(root, turn_signature):
        state = _load_turn_state(root, session_signature, turn_signature)
        _workspace_record(state, workspace_root, observed)
        events = state["events"]
        if not isinstance(events, list):
            raise ValueError
        event_id = _event_signature(key, payload)
        if not any(isinstance(item, dict) and item.get("event_id") == event_id for item in events):
            tool_name = payload.get("tool_name")
            tool_name = tool_name if isinstance(tool_name, str) else "UNKNOWN"
            input_digest, input_status = _bounded_digest(payload.get("tool_input"))
            events.append(
                {
                    "event_id": event_id,
                    "workspace_id": observed["workspace_id"],
                    "tool_name": tool_name,
                    "operation_kind": "EDIT" if tool_name == "apply_patch" else "COMMAND",
                    "validation_checks": _validation_checks(tool_name, payload.get("tool_input")),
                    "input_sha256": input_digest,
                    "input_digest_status": input_status,
                    "result_status": "PENDING",
                    "exit_code": None,
                    "output_sha256": None,
                    "output_digest_status": "PENDING",
                    "preflight_observed": True,
                }
            )
        if len(events) > MAX_EVENTS:
            del events[: len(events) - MAX_EVENTS]
        _save_turn_state(root, turn_signature, state)


def _post_tool_use(
    payload: Mapping[str, object],
    root: Path,
    key: bytes,
    session_signature: str,
    turn_signature: str,
) -> None:
    cwd = _event_cwd(payload)
    workspace_root, observed = _observe_workspace(cwd, key)
    with _state_lock(root, turn_signature):
        state = _load_turn_state(root, session_signature, turn_signature)
        _workspace_record(state, workspace_root, observed)
        events = state["events"]
        if not isinstance(events, list):
            raise ValueError
        event_id = _event_signature(key, payload)
        event = next(
            (
                item
                for item in events
                if isinstance(item, dict) and item.get("event_id") == event_id
            ),
            None,
        )
        tool_name = payload.get("tool_name")
        tool_name = tool_name if isinstance(tool_name, str) else "UNKNOWN"
        if event is None:
            input_digest, input_status = _bounded_digest(payload.get("tool_input"))
            event = {
                "event_id": event_id,
                "workspace_id": observed["workspace_id"],
                "tool_name": tool_name,
                "operation_kind": "EDIT" if tool_name == "apply_patch" else "COMMAND",
                "validation_checks": _validation_checks(tool_name, payload.get("tool_input")),
                "input_sha256": input_digest,
                "input_digest_status": input_status,
                "preflight_observed": False,
            }
            events.append(event)
        result_status, exit_code = _operation_status(tool_name, payload.get("tool_response"))
        output_digest, output_status = _bounded_digest(payload.get("tool_response"))
        event.update(
            {
                "result_status": result_status,
                "exit_code": exit_code,
                "output_sha256": output_digest,
                "output_digest_status": output_status,
            }
        )
        if len(events) > MAX_EVENTS:
            del events[: len(events) - MAX_EVENTS]
        _save_turn_state(root, turn_signature, state)


def _record_map(value: object) -> dict[str, JsonObject]:
    if not isinstance(value, list):
        return {}
    return {
        item["path"]: item
        for item in value
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }


def _committed_paths(root: Path, before_head: object, after_head: object) -> list[str]:
    if (
        not isinstance(before_head, str)
        or not isinstance(after_head, str)
        or before_head == after_head
        or not _HEX_OBJECT_ID.fullmatch(before_head)
        or not _HEX_OBJECT_ID.fullmatch(after_head)
    ):
        return []
    try:
        raw = _run_git(root, ("diff", "--name-only", "-z", before_head, after_head, "--"))
    except _ObservationUnavailable:
        return []
    paths: list[str] = []
    for value in raw.decode("utf-8", "replace").split("\0"):
        if not value:
            continue
        try:
            paths.append(_safe_relative_path(value))
        except _ObservationUnavailable:
            continue
    return paths[:MAX_RECEIPT_PATHS]


def _path_categories(path: str) -> list[str]:
    lowered = path.lower()
    result: list[str] = []
    if lowered.startswith("tests/") or "/tests/" in lowered or lowered.startswith("test_"):
        result.append("TEST")
    if lowered.startswith("docs/") or lowered.endswith((".md", ".rst")):
        result.append("DOCUMENTATION")
    if lowered.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java")):
        result.append("SOURCE")
    if lowered in {
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
    }:
        result.append("BUILD_OR_DEPENDENCY")
    if lowered.startswith((".github/", ".codex/", ".agents/", "config/")):
        result.append("CONFIGURATION")
    return result or ["OTHER"]


def _workspace_change(root: Path, value: Mapping[str, object]) -> JsonObject:
    baseline = value.get("baseline")
    latest = value.get("latest")
    if not isinstance(baseline, dict) or not isinstance(latest, dict):
        return {"status": "PARTIAL", "reason_code": "WORKSPACE_OBSERVATION_INCOMPLETE"}
    before_records = _record_map(baseline.get("records"))
    after_records = _record_map(latest.get("records"))
    changed_paths = [
        path
        for path in sorted(set(before_records) | set(after_records))
        if before_records.get(path) != after_records.get(path)
    ]
    committed = _committed_paths(root, baseline.get("head"), latest.get("head"))
    combined = sorted(set(changed_paths) | set(committed))
    head_changed = baseline.get("head") != latest.get("head")
    branch_changed = baseline.get("branch") != latest.get("branch")
    changed = baseline.get("state_digest") != latest.get("state_digest")
    categories: Counter[str] = Counter()
    for path in combined:
        categories.update(_path_categories(path))
    return {
        "status": "CHANGED" if changed else "NO_NET_CHANGE",
        "workspace_id": latest.get("workspace_id"),
        "workspace_name": latest.get("workspace_name"),
        "head_before": baseline.get("head"),
        "head_after": latest.get("head"),
        "branch_before": baseline.get("branch"),
        "branch_after": latest.get("branch"),
        "head_changed": head_changed,
        "branch_changed": branch_changed,
        "dirty_before": baseline.get("dirty"),
        "dirty_after": latest.get("dirty"),
        "state_digest_before": baseline.get("state_digest"),
        "state_digest_after": latest.get("state_digest"),
        "changed_path_count": len(combined),
        "changed_paths": combined[:MAX_RECEIPT_PATHS],
        "paths_truncated": len(combined) > MAX_RECEIPT_PATHS,
        "category_counts": dict(sorted(categories.items())),
    }


def _refresh_workspace_records(turn_state: JsonObject, key: bytes) -> list[JsonObject]:
    workspaces = turn_state.get("workspaces")
    if not isinstance(workspaces, dict):
        return []
    changes: list[JsonObject] = []
    for workspace_id, value in sorted(workspaces.items()):
        if not isinstance(workspace_id, str) or not isinstance(value, dict):
            continue
        private_root = value.get("workspace_root_private")
        if not isinstance(private_root, str):
            changes.append({"status": "PARTIAL", "workspace_id": workspace_id})
            continue
        try:
            root, latest = _observe_workspace(Path(private_root), key)
        except _ObservationUnavailable:
            changes.append(
                {
                    "status": "PARTIAL",
                    "workspace_id": workspace_id,
                    "workspace_name": (
                        value.get("latest", {}).get("workspace_name")
                        if isinstance(value.get("latest"), dict)
                        else "workspace"
                    ),
                    "reason_code": "FINAL_OBSERVATION_UNAVAILABLE",
                }
            )
            continue
        if latest.get("workspace_id") != workspace_id:
            changes.append(
                {
                    "status": "PARTIAL",
                    "workspace_id": workspace_id,
                    "workspace_name": latest.get("workspace_name"),
                    "reason_code": "WORKSPACE_IDENTITY_CHANGED",
                }
            )
            continue
        value["latest"] = latest
        changes.append(_workspace_change(root, value))
    return changes


def _receipt(
    turn_state: JsonObject,
    workspace_changes: list[JsonObject],
) -> JsonObject:
    events = turn_state.get("events")
    event_values = (
        [item for item in events if isinstance(item, dict)] if isinstance(events, list) else []
    )
    tool_counts = Counter(
        item.get("tool_name") for item in event_values if isinstance(item.get("tool_name"), str)
    )
    validations: list[JsonObject] = []
    for item in event_values:
        checks = item.get("validation_checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if isinstance(check, str):
                validations.append(
                    {
                        "check_id": check,
                        "status": item.get("result_status", "UNKNOWN"),
                        "exit_code": item.get("exit_code"),
                        "observation_source": "CODEX_POST_TOOL_USE",
                        "output_sha256": item.get("output_sha256"),
                        "output_digest_status": item.get("output_digest_status"),
                    }
                )
    changed_count = sum(1 for item in workspace_changes if item.get("status") == "CHANGED")
    partial_count = sum(1 for item in workspace_changes if item.get("status") == "PARTIAL")
    status_value = "CHANGED" if changed_count else "NO_NET_CHANGE"
    if partial_count:
        status_value = "PARTIAL"
    runtime_identity = hook_runtime_identity()
    body: JsonObject = {
        "schema_name": RECEIPT_SCHEMA_NAME,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "observer_identity": HOOK_IDENTITY,
        "hook_runtime_identity": runtime_identity,
        "status": status_value,
        "attribution": {
            "host_kind": "CODEX",
            "session_signature": turn_state.get("session_signature"),
            "turn_signature": turn_state.get("turn_signature"),
            "source": "CODEX_HOOK_INPUT",
            "correlation_only": True,
            "authorization_effect": "NONE",
        },
        "workspaces": workspace_changes,
        "operations": {
            "observed_count": len(event_values),
            "tool_counts": dict(sorted(tool_counts.items())),
            "validation_checks": validations,
            "pending_or_unknown_count": sum(
                1 for item in event_values if item.get("result_status") in {"PENDING", "UNKNOWN"}
            ),
        },
        "boundary": {
            "steward_executed_commands": False,
            "steward_modified_workspace": False,
            "prompt_text_persisted": False,
            "command_text_persisted": False,
            "tool_output_persisted": False,
            "absolute_workspace_paths_published": False,
            "snapshot_evidence": False,
            "host_observation_not_attestation": True,
        },
        "unknowns": (
            [{"reason_code": "FINAL_WORKSPACE_OBSERVATION_PARTIAL"}] if partial_count else []
        ),
    }
    body["receipt_id"] = _sha256(_canonical_bytes(body))
    return body


def _handoff(receipt: Mapping[str, object], workspace: Mapping[str, object]) -> JsonObject:
    operations = receipt.get("operations")
    validation_checks = (
        operations.get("validation_checks", []) if isinstance(operations, dict) else []
    )
    return {
        "schema_name": HANDOFF_SCHEMA_NAME,
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "observer_identity": HOOK_IDENTITY,
        "hook_runtime_identity": receipt.get("hook_runtime_identity"),
        "workspace_id": workspace.get("workspace_id"),
        "workspace_name": workspace.get("workspace_name"),
        "receipt_id": receipt.get("receipt_id"),
        "after": {
            "head": workspace.get("head_after"),
            "branch": workspace.get("branch_after"),
            "dirty": workspace.get("dirty_after"),
            "state_digest": workspace.get("state_digest_after"),
        },
        "changes": {
            "changed_path_count": workspace.get("changed_path_count"),
            "changed_paths": workspace.get("changed_paths"),
            "paths_truncated": workspace.get("paths_truncated"),
            "category_counts": workspace.get("category_counts"),
        },
        "validation_checks": validation_checks,
        "attribution": receipt.get("attribution"),
    }


def _handoff_state(
    handoff: Mapping[str, object],
    observed: Mapping[str, object],
) -> str:
    """Classify a handoff without accepting state from another hook generation."""

    if (
        handoff.get("schema_name") != HANDOFF_SCHEMA_NAME
        or handoff.get("schema_version") != HANDOFF_SCHEMA_VERSION
        or handoff.get("observer_identity") != HOOK_IDENTITY
        or handoff.get("hook_runtime_identity") != hook_runtime_identity()
    ):
        return "STALE_HOOK_GENERATION"
    after = handoff.get("after")
    if not isinstance(after, dict):
        return "STALE_WORKSPACE_STATE"
    return (
        "CURRENT"
        if after.get("state_digest") == observed.get("state_digest")
        else "STALE_WORKSPACE_STATE"
    )


def _persist_receipt(root: Path, receipt: JsonObject) -> None:
    receipt_id = receipt.get("receipt_id")
    if not isinstance(receipt_id, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt_id):
        raise ValueError
    _atomic_json_write(root / "receipts" / f"{receipt_id}.json", receipt)
    workspaces = receipt.get("workspaces")
    if not isinstance(workspaces, list):
        return
    for workspace in workspaces:
        if not isinstance(workspace, dict) or workspace.get("status") != "CHANGED":
            continue
        workspace_id = workspace.get("workspace_id")
        if not isinstance(workspace_id, str) or not re.fullmatch(r"[0-9a-f]{64}", workspace_id):
            continue
        _atomic_json_write(root / "handoffs" / f"{workspace_id}.json", _handoff(receipt, workspace))


def _delivery_reason(receipt: Mapping[str, object]) -> str:
    workspaces = receipt.get("workspaces")
    changed = (
        [item for item in workspaces if isinstance(item, dict) and item.get("status") == "CHANGED"]
        if isinstance(workspaces, list)
        else []
    )
    names = (
        ", ".join(str(item.get("workspace_name", "workspace")) for item in changed[:3])
        or "workspace"
    )
    paths: list[str] = []
    for item in changed:
        values = item.get("changed_paths")
        if isinstance(values, list):
            paths.extend(value for value in values if isinstance(value, str))
    operations = receipt.get("operations")
    validations = operations.get("validation_checks", []) if isinstance(operations, dict) else []
    validation_text = (
        ", ".join(
            f"{item.get('check_id')}={item.get('status')}"
            for item in validations
            if isinstance(item, dict)
        )
        or "none observed"
    )
    receipt_id = str(receipt.get("receipt_id", "unavailable"))[:12]
    path_text = ", ".join(sorted(set(paths))[:12]) or "HEAD/workspace metadata change"
    return (
        f"{HOOK_IDENTITY} finalized receipt {receipt_id} for {names}. "
        f"Observed changed paths: {path_text}. Validation observations: {validation_text}. "
        "This is host-correlated execution metadata, not Snapshot Evidence or authorization. "
        "Continue once with the user's final answer and include one concise `STEWARD receipt:` "
        "line when useful. Do not run additional tools solely for this receipt."
    )


def _stop(
    payload: Mapping[str, object],
    root: Path,
    key: bytes,
    session_signature: str,
    turn_signature: str,
) -> JsonObject:
    turn_path = root / "turns" / f"{turn_signature}.json"
    with _state_lock(root, turn_signature):
        state = _safe_json_read(turn_path)
        if (
            state is None
            or state.get("schema_name") != HOOK_STATE_SCHEMA_NAME
            or state.get("session_signature") != session_signature
        ):
            return {}
        workspace_changes = _refresh_workspace_records(state, key)
        receipt = _receipt(state, workspace_changes)
        _persist_receipt(root, receipt)
        changed = any(item.get("status") == "CHANGED" for item in workspace_changes)
        stop_hook_active = payload.get("stop_hook_active") is True
        already_injected = state.get("delivery_injected") is True
        if changed and not stop_hook_active and not already_injected:
            state["delivery_injected"] = True
            state["receipt_id"] = receipt["receipt_id"]
            _save_turn_state(root, turn_signature, state)
            return {"decision": "block", "reason": _delivery_reason(receipt)}
        try:
            turn_path.unlink()
        except FileNotFoundError:
            pass
        return {}


def _session_start(payload: Mapping[str, object], root: Path, key: bytes) -> JsonObject | None:
    try:
        _workspace, observed = _observe_workspace(_event_cwd(payload), key)
    except _ObservationUnavailable:
        return None
    workspace_id = observed["workspace_id"]
    handoff = _safe_json_read(root / "handoffs" / f"{workspace_id}.json")
    handoff_text = "No prior execution handoff is bound to this workspace."
    if handoff is not None:
        handoff_state = _handoff_state(handoff, observed)
        receipt_id = str(handoff.get("receipt_id", "unavailable"))[:12]
        handoff_text = (
            f"Latest handoff receipt {receipt_id} is "
            f"{handoff_state} for the observed repository state."
        )
    runtime_identity = hook_runtime_identity()
    context = (
        f"{HOOK_IDENTITY}_ACTIVE. STEWARD integration identity: plugin {PLUGIN_NAME} "
        f"{PLUGIN_BASE_VERSION}; Skill {SKILL_NAME}; MCP {MCP_SERVER_NAME}; native "
        f"{NATIVE_SURFACE_IDENTITY}; server {NATIVE_SERVER_VERSION}; hook generation "
        f"{str(runtime_identity['hook_generation_sha256'])[:12]}. Codex host hooks bound "
        "the current Git workspace "
        f"{observed['workspace_name']} ({str(workspace_id)[:12]}). Routine local commands and "
        "edits are observed automatically; do not call steward_code_execution PREFLIGHT/POSTFLIGHT "
        "for this workflow while this marker is present. "
        f"{handoff_text} Hook receipts are correlation records, not authorization or Snapshot Evidence."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def _session_end(root: Path, session_signature: str) -> None:
    for path in list((root / "turns").glob("*.json"))[:MAX_RECEIPTS]:
        value = _safe_json_read(path)
        if value is not None and value.get("session_signature") == session_signature:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _prune_directory(path: Path, *, max_age_seconds: int, max_files: int) -> None:
    now = time.time()
    values: list[tuple[float, Path]] = []
    for child in path.glob("*.json"):
        try:
            metadata = child.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            continue
        values.append((metadata.st_mtime, child))
    values.sort(reverse=True)
    for index, (modified, child) in enumerate(values):
        if index >= max_files or now - modified > max_age_seconds:
            try:
                child.unlink()
            except OSError:
                pass


def _prune(root: Path) -> None:
    _prune_directory(
        root / "turns",
        max_age_seconds=TURN_RETENTION_SECONDS,
        max_files=MAX_RECEIPTS,
    )
    _prune_directory(
        root / "receipts",
        max_age_seconds=RECEIPT_RETENTION_SECONDS,
        max_files=MAX_RECEIPTS,
    )
    _prune_directory(
        root / "handoffs",
        max_age_seconds=RECEIPT_RETENTION_SECONDS,
        max_files=MAX_HANDOFFS,
    )


def _handle_hook(payload: Mapping[str, object], state_root: Path) -> JsonObject | None:
    root = _ensure_state_root(state_root)
    key = _load_or_create_key(root)
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if not isinstance(event, str) or not isinstance(session_id, str) or not session_id:
        return {} if event == "Stop" else None
    session_signature = _signature(key, "session", session_id)
    _prune(root)
    if event == "SessionStart":
        return _session_start(payload, root, key)
    if event == "SessionEnd":
        _session_end(root, session_signature)
        return None
    turn_id = payload.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return {} if event == "Stop" else None
    turn_signature = _signature(key, "turn", f"{session_id}\0{turn_id}")
    if event == "PreToolUse":
        _pre_tool_use(payload, root, key, session_signature, turn_signature)
        return None
    if event == "PostToolUse":
        _post_tool_use(payload, root, key, session_signature, turn_signature)
        return None
    if event == "Stop":
        return _stop(payload, root, key, session_signature, turn_signature)
    return None


def handle_hook(payload: Mapping[str, object], state_root: Path) -> JsonObject | None:
    """Handle one trusted Codex hook event without propagating operational failures."""

    try:
        return _handle_hook(payload, state_root)
    except Exception:
        return {} if payload.get("hook_event_name") == "Stop" else None


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--implementation-sha256", required=True)
    try:
        arguments = parser.parse_args()
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = {}
    except (SystemExit, OSError, UnicodeError, json.JSONDecodeError):
        print("{}")
        return
    if arguments.implementation_sha256 != implementation_sha256():
        result: JsonObject | None = {} if payload.get("hook_event_name") == "Stop" else None
    else:
        result = handle_hook(payload, arguments.state_dir)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
