"""Bounded descriptor-based direct payload observations for Snapshot Evidence v2."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import time
from enum import Enum
from pathlib import Path
from typing import Callable

from .errors import SnapshotBudgetError
from .models import (
    FilesystemEntry,
    FilesystemEntryV2,
    FilesystemObjectType,
    PayloadHashPolicy,
    PayloadObservation,
    PayloadObservationProvenance,
    PayloadObservationStatus,
    ScopeConfig,
)


DEFAULT_MAX_HASH_FILE_BYTES = 1_073_741_824
DEFAULT_MAX_TOTAL_HASH_BYTES = 8_589_934_592
DEFAULT_MAX_HASH_DURATION_SECONDS = 300.0
DEFAULT_HASH_CHUNK_SIZE = 1_048_576


class PayloadLocality(str, Enum):
    """A deliberately conservative pre-open locality determination."""

    LOCAL = "LOCAL"
    NON_LOCAL = "NON_LOCAL"
    UNKNOWN = "UNKNOWN"


LocalityProvider = Callable[[Path], PayloadLocality]
VerifiedReuseResolver = Callable[[FilesystemEntry], PayloadObservation | None]


def unknown_locality(_: Path) -> PayloadLocality:
    """Production fallback: never infer local materialization from an open attempt."""
    return PayloadLocality.UNKNOWN


def default_payload_hash_policy(
    *,
    max_hash_file_bytes: int = DEFAULT_MAX_HASH_FILE_BYTES,
    max_total_hash_bytes: int = DEFAULT_MAX_TOTAL_HASH_BYTES,
    max_hash_duration_seconds: float = DEFAULT_MAX_HASH_DURATION_SECONDS,
    hash_chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
    allow_verified_reuse: bool = False,
) -> PayloadHashPolicy:
    """Resolve the one frozen direct-read policy, then validate it once."""
    policy = PayloadHashPolicy(
        algorithm="sha256",
        algorithm_version=1,
        max_hash_file_bytes=max_hash_file_bytes,
        max_total_hash_bytes=max_total_hash_bytes,
        max_hash_duration_seconds=max_hash_duration_seconds,
        hash_chunk_size=hash_chunk_size,
        allow_non_local_content=False,
        allow_verified_reuse=allow_verified_reuse,
    )
    validate_payload_hash_policy(policy)
    return policy


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SnapshotBudgetError(f"PAYLOAD_HASH_POLICY_INVALID: {name} must be a positive integer")
    return value


def validate_payload_hash_policy(policy: PayloadHashPolicy) -> None:
    """Reject all policy variants which cannot produce frozen v2 facts."""
    if not isinstance(policy, PayloadHashPolicy):
        raise SnapshotBudgetError("PAYLOAD_HASH_POLICY_INVALID: policy must be PayloadHashPolicy")
    if policy.algorithm != "sha256" or policy.algorithm_version != 1:
        raise SnapshotBudgetError("PAYLOAD_HASH_POLICY_INVALID: SHA-256 algorithm version 1 is required")
    _positive_int(policy.max_hash_file_bytes, "max_hash_file_bytes")
    _positive_int(policy.max_total_hash_bytes, "max_total_hash_bytes")
    duration = policy.max_hash_duration_seconds
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not __import__("math").isfinite(duration)
        or not 0 < duration <= 86_400
    ):
        raise SnapshotBudgetError(
            "PAYLOAD_HASH_POLICY_INVALID: max_hash_duration_seconds must be finite and 0 < value <= 86400"
        )
    chunk = _positive_int(policy.hash_chunk_size, "hash_chunk_size")
    if not 65_536 <= chunk <= 16_777_216 or chunk & (chunk - 1):
        raise SnapshotBudgetError(
            "PAYLOAD_HASH_POLICY_INVALID: hash_chunk_size must be a power of two from 65536 through 16777216"
        )
    if not isinstance(policy.allow_non_local_content, bool) or not isinstance(
        policy.allow_verified_reuse, bool
    ):
        raise SnapshotBudgetError("PAYLOAD_HASH_POLICY_INVALID: capability flags must be booleans")
    if policy.allow_non_local_content:
        raise SnapshotBudgetError("PAYLOAD_HASH_POLICY_INVALID: non-local content is disabled")


def _failure(
    status: PayloadObservationStatus, *, failure_code: str | None = None, os_error_code: int | None = None
) -> PayloadObservation:
    return PayloadObservation(status, None, None, None, None, None, None, failure_code, os_error_code)


def _success(policy: PayloadHashPolicy, digest: str, size: int) -> PayloadObservation:
    return PayloadObservation(
        PayloadObservationStatus.EMPTY_FILE_HASHED if size == 0 else PayloadObservationStatus.HASHED,
        policy.algorithm,
        policy.algorithm_version,
        digest,
        size,
        PayloadObservationProvenance.DIRECT_READ,
        None,
        None,
        None,
    )


def _path_for(entry: FilesystemEntry, scope_paths: dict[str, Path]) -> Path:
    root = scope_paths[entry.scope_id]
    return root if entry.relative_path == "." else root / entry.relative_path


def _stat_identity(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _entry_matches_stat(entry: FilesystemEntry, st: os.stat_result) -> bool:
    return (
        entry.device_id == st.st_dev
        and entry.inode == st.st_ino
        and entry.size_bytes == st.st_size
        and entry.mtime_ns == st.st_mtime_ns
        and entry.ctime_ns == st.st_ctime_ns
    )


def _open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def observe_direct_payloads(
    entries: tuple[FilesystemEntry, ...],
    scopes: tuple[ScopeConfig, ...],
    policy: PayloadHashPolicy,
    *,
    locality_provider: LocalityProvider = unknown_locality,
    monotonic: Callable[[], float] = time.monotonic,
    opener: Callable[[Path, int], int] | None = None,
    _started: float | None = None,
    _actual_bytes_read: list[int] | None = None,
) -> tuple[FilesystemEntryV2, ...]:
    """Observe payloads in canonical order without changing stat observations.

    The default locality provider is intentionally UNKNOWN.  A platform adapter may
    be supplied only when it can make a read-free locality determination; tests use
    the same interface for deterministic LOCAL/NON_LOCAL cases.
    """
    validate_payload_hash_policy(policy)
    assert policy.max_hash_file_bytes is not None
    assert policy.max_total_hash_bytes is not None
    assert policy.max_hash_duration_seconds is not None
    max_file_bytes = int(policy.max_hash_file_bytes)
    max_total_bytes = int(policy.max_total_hash_bytes)
    max_duration = float(policy.max_hash_duration_seconds)
    chunk_size = int(policy.hash_chunk_size)
    by_scope = {scope.scope_id: scope.normalized_path for scope in scopes}
    opened = opener or (lambda path, flags: os.open(path, flags))
    started = monotonic() if _started is None else _started
    actual_bytes_read = [0] if _actual_bytes_read is None else _actual_bytes_read
    output: list[FilesystemEntryV2] = []
    ordered = sorted(entries, key=lambda item: (item.scope_id, item.relative_path.encode("utf-8", "surrogateescape")))

    for entry in ordered:
        observation: PayloadObservation
        if entry.object_type != FilesystemObjectType.REGULAR_FILE:
            observation = _failure(PayloadObservationStatus.NOT_REGULAR_FILE)
        elif entry.excluded:
            observation = _failure(PayloadObservationStatus.UNSUPPORTED, failure_code="ENTRY_EXCLUDED")
        elif entry.observation_status.value != "observed" or entry.size_bytes is None:
            observation = _failure(PayloadObservationStatus.UNSUPPORTED, failure_code="METADATA_UNAVAILABLE")
        elif monotonic() - started >= max_duration:
            observation = _failure(PayloadObservationStatus.TIME_BUDGET_EXHAUSTED)
        elif entry.size_bytes > max_file_bytes:
            observation = _failure(PayloadObservationStatus.FILE_TOO_LARGE)
        elif entry.size_bytes > max_total_bytes - actual_bytes_read[0]:
            observation = _failure(PayloadObservationStatus.TOTAL_BYTE_BUDGET_EXHAUSTED)
        else:
            path = _path_for(entry, by_scope)
            try:
                locality = locality_provider(path)
            except Exception:
                locality = PayloadLocality.UNKNOWN
            if locality == PayloadLocality.NON_LOCAL:
                observation = _failure(PayloadObservationStatus.NOT_LOCAL)
            elif locality != PayloadLocality.LOCAL:
                observation = _failure(PayloadObservationStatus.UNSUPPORTED, failure_code="LOCALITY_UNKNOWN")
            elif not hasattr(os, "O_NOFOLLOW"):
                observation = _failure(PayloadObservationStatus.UNSUPPORTED, failure_code="NO_NOFOLLOW")
            else:
                descriptor: int | None = None
                try:
                    descriptor = opened(path, _open_flags())
                    before = os.fstat(descriptor)
                    if not stat.S_ISREG(before.st_mode):
                        observation = _failure(PayloadObservationStatus.NOT_REGULAR_FILE)
                    elif not _entry_matches_stat(entry, before):
                        observation = _failure(PayloadObservationStatus.CHANGED_DURING_READ)
                    elif before.st_size > max_file_bytes:
                        observation = _failure(PayloadObservationStatus.FILE_TOO_LARGE)
                    elif before.st_size > max_total_bytes - actual_bytes_read[0]:
                        observation = _failure(PayloadObservationStatus.TOTAL_BYTE_BUDGET_EXHAUSTED)
                    else:
                        hasher = hashlib.sha256()
                        read_count = 0
                        timed_out = False
                        read_outcome: PayloadObservation | None = None
                        while True:
                            if monotonic() - started >= max_duration:
                                timed_out = True
                                break
                            chunk = os.read(descriptor, chunk_size)
                            if not chunk:
                                break
                            read_count += len(chunk)
                            actual_bytes_read[0] += len(chunk)
                            hasher.update(chunk)
                            if read_count > max_file_bytes or actual_bytes_read[0] > max_total_bytes:
                                read_outcome = _failure(PayloadObservationStatus.FILE_TOO_LARGE if read_count > max_file_bytes else PayloadObservationStatus.TOTAL_BYTE_BUDGET_EXHAUSTED)
                                break
                        if read_outcome is not None:
                            observation = read_outcome
                        else:
                            if timed_out:
                                observation = _failure(PayloadObservationStatus.TIME_BUDGET_EXHAUSTED)
                            else:
                                after = os.fstat(descriptor)
                                try:
                                    path_after = os.lstat(path)
                                except FileNotFoundError:
                                    path_after = None
                                if (
                                    read_count != before.st_size
                                    or _stat_identity(before) != _stat_identity(after)
                                    or path_after is None
                                    or _stat_identity(before) != _stat_identity(path_after)
                                ):
                                    observation = _failure(PayloadObservationStatus.CHANGED_DURING_READ)
                                else:
                                    observation = _success(policy, hasher.hexdigest(), read_count)
                except FileNotFoundError as error:
                    observation = _failure(PayloadObservationStatus.NOT_FOUND_DURING_READ, os_error_code=error.errno)
                except PermissionError as error:
                    observation = _failure(PayloadObservationStatus.PERMISSION_DENIED, os_error_code=error.errno)
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOENT}:
                        observation = _failure(PayloadObservationStatus.NOT_FOUND_DURING_READ, os_error_code=error.errno)
                    else:
                        observation = _failure(PayloadObservationStatus.IO_ERROR, os_error_code=error.errno)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
        output.append(
            FilesystemEntryV2(
                entry.entry_id, entry.snapshot_id, entry.scope_id, entry.relative_path, entry.object_type,
                entry.device_id, entry.inode, entry.mode, entry.uid, entry.gid, entry.size_bytes,
                entry.mtime_ns, entry.ctime_ns, entry.birthtime_ns, entry.link_count,
                entry.symlink_target_raw, entry.readable, entry.writable, entry.executable,
                entry.observation_status, entry.error_code, entry.error_message, entry.excluded,
                None, observation,
            )
        )
    return tuple(output)


def _v2_entry(entry: FilesystemEntry, observation: PayloadObservation) -> FilesystemEntryV2:
    return FilesystemEntryV2(
        entry.entry_id, entry.snapshot_id, entry.scope_id, entry.relative_path, entry.object_type,
        entry.device_id, entry.inode, entry.mode, entry.uid, entry.gid, entry.size_bytes,
        entry.mtime_ns, entry.ctime_ns, entry.birthtime_ns, entry.link_count,
        entry.symlink_target_raw, entry.readable, entry.writable, entry.executable,
        entry.observation_status, entry.error_code, entry.error_message, entry.excluded,
        None, observation,
    )


def observe_payloads(
    entries: tuple[FilesystemEntry, ...],
    scopes: tuple[ScopeConfig, ...],
    policy: PayloadHashPolicy,
    *,
    locality_provider: LocalityProvider = unknown_locality,
    reuse_resolver: VerifiedReuseResolver | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    opener: Callable[[Path, int], int] | None = None,
) -> tuple[FilesystemEntryV2, ...]:
    """Reuse verified facts when eligible, otherwise retain direct-read behavior.

    A reuse probe never reads the descriptor.  It deliberately delegates every
    miss to the established direct reader with shared time and byte accounting.
    """
    validate_payload_hash_policy(policy)
    if not policy.allow_verified_reuse or reuse_resolver is None:
        return observe_direct_payloads(
            entries, scopes, policy, locality_provider=locality_provider, monotonic=monotonic, opener=opener
        )
    assert policy.max_hash_duration_seconds is not None
    started = monotonic()
    actual_bytes_read = [0]
    roots = {scope.scope_id: scope.normalized_path for scope in scopes}
    opened = opener or (lambda path, flags: os.open(path, flags))
    output: list[FilesystemEntryV2] = []
    ordered = sorted(entries, key=lambda item: (item.scope_id, item.relative_path.encode("utf-8", "surrogateescape")))
    max_duration = float(policy.max_hash_duration_seconds)

    for entry in ordered:
        reused: PayloadObservation | None = None
        if (
            entry.object_type == FilesystemObjectType.REGULAR_FILE
            and not entry.excluded
            and entry.observation_status.value == "observed"
            and entry.size_bytes is not None
            and monotonic() - started < max_duration
        ):
            path = _path_for(entry, roots)
            try:
                locality = locality_provider(path)
            except Exception:
                locality = PayloadLocality.UNKNOWN
            if locality == PayloadLocality.LOCAL and hasattr(os, "O_NOFOLLOW"):
                descriptor: int | None = None
                try:
                    descriptor = opened(path, _open_flags())
                    before = os.fstat(descriptor)
                    if stat.S_ISREG(before.st_mode) and _entry_matches_stat(entry, before):
                        candidate = reuse_resolver(entry)
                        after = os.fstat(descriptor)
                        if monotonic() - started >= max_duration:
                            output.append(_v2_entry(entry, _failure(PayloadObservationStatus.TIME_BUDGET_EXHAUSTED)))
                            continue
                        if candidate is not None and _stat_identity(before) == _stat_identity(after):
                            reused = candidate
                except OSError:
                    pass
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
            elif locality == PayloadLocality.NON_LOCAL:
                output.append(_v2_entry(entry, _failure(PayloadObservationStatus.NOT_LOCAL)))
                continue
            elif locality != PayloadLocality.LOCAL:
                output.append(
                    _v2_entry(
                        entry,
                        _failure(PayloadObservationStatus.UNSUPPORTED, failure_code="LOCALITY_UNKNOWN"),
                    )
                )
                continue
        if reused is not None:
            output.append(_v2_entry(entry, reused))
            continue
        direct = observe_direct_payloads(
            (entry,),
            scopes,
            policy,
            locality_provider=locality_provider,
            monotonic=monotonic,
            opener=opener,
            _started=started,
            _actual_bytes_read=actual_bytes_read,
        )
        output.extend(direct)
    return tuple(output)
