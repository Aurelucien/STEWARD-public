"""Bounded, non-recursive-stack, read-only os.scandir snapshot observation."""

import hashlib
import os
import stat
import time
from pathlib import Path

from .errors import SnapshotScopeError
from .models import (
    FilesystemEntry,
    FilesystemObjectType,
    FilesystemObservationStatus,
    ScanBudget,
    ScopeConfig,
    ScopeRole,
    StewardConfig,
)


def _kind(mode: int) -> FilesystemObjectType:
    if stat.S_ISREG(mode):
        return FilesystemObjectType.REGULAR_FILE
    if stat.S_ISDIR(mode):
        return FilesystemObjectType.DIRECTORY
    if stat.S_ISLNK(mode):
        return FilesystemObjectType.SYMLINK
    if stat.S_ISFIFO(mode):
        return FilesystemObjectType.FIFO
    if stat.S_ISSOCK(mode):
        return FilesystemObjectType.SOCKET
    if stat.S_ISCHR(mode):
        return FilesystemObjectType.CHARACTER_DEVICE
    if stat.S_ISBLK(mode):
        return FilesystemObjectType.BLOCK_DEVICE
    return FilesystemObjectType.UNKNOWN


def _relative(path: Path, root: Path) -> str:
    raw = path.relative_to(root)
    return "." if str(raw) == "." else raw.as_posix()


def _entry(
    snapshot_id: str,
    scope_id: str,
    relative: str,
    path: Path,
    st: os.stat_result | None,
    status: FilesystemObservationStatus = FilesystemObservationStatus.OBSERVED,
    error: OSError | None = None,
    excluded: bool = False,
) -> FilesystemEntry:
    if st is None:
        return FilesystemEntry(
            hashlib.sha256(f"{snapshot_id}\0{scope_id}\0{relative}".encode()).hexdigest(),
            snapshot_id,
            scope_id,
            relative,
            FilesystemObjectType.UNKNOWN,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            False,
            status,
            str(error.errno) if error else None,
            str(error) if error else None,
            excluded,
        )
    target = None
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = None
    return FilesystemEntry(
        hashlib.sha256(f"{snapshot_id}\0{scope_id}\0{relative}".encode()).hexdigest(),
        snapshot_id,
        scope_id,
        relative,
        _kind(st.st_mode),
        st.st_dev,
        st.st_ino,
        st.st_mode,
        st.st_uid,
        st.st_gid,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
        getattr(st, "st_birthtime_ns", None),
        st.st_nlink,
        target,
        os.access(path, os.R_OK, follow_symlinks=False),
        os.access(path, os.W_OK, follow_symlinks=False),
        os.access(path, os.X_OK, follow_symlinks=False),
        status,
        None,
        None,
        excluded,
    )


def select_scopes(config: StewardConfig, requested: tuple[str, ...]) -> tuple[ScopeConfig, ...]:
    by_id = {scope.scope_id: scope for scope in config.scopes}
    unknown = tuple(sorted(set(requested).difference(by_id)))
    if unknown:
        raise SnapshotScopeError("unknown scope IDs: " + ", ".join(unknown))
    selected = (
        [by_id[item] for item in requested]
        if requested
        else [
            scope
            for scope in config.scopes
            if scope.enabled and scope.role == ScopeRole.MANAGED_ROOT
        ]
    )
    if not selected:
        raise SnapshotScopeError("no enabled managed scopes selected")
    for scope in selected:
        if (
            scope.role not in (ScopeRole.MANAGED_ROOT, ScopeRole.REFERENCE_ROOT)
            or not scope.enabled
        ):
            raise SnapshotScopeError(f"scope unavailable: {scope.scope_id}")
        if scope.follow_directory_symlinks:
            raise SnapshotScopeError(f"scope symlink policy unsupported: {scope.scope_id}")
    return tuple(sorted(selected, key=lambda item: item.scope_id))


def scan(
    config: StewardConfig, snapshot_id: str, scopes: tuple[ScopeConfig, ...], budget: ScanBudget
) -> tuple[tuple[FilesystemEntry, ...], bool]:
    """Return deterministic observations and whether a budget/error made it partial."""
    started = time.monotonic()
    entries: list[FilesystemEntry] = []
    partial = False
    byte_total = 0
    excluded = [
        scope.normalized_path for scope in config.scopes if scope.role == ScopeRole.EXCLUDED_ROOT
    ]
    internal = (
        config.paths.data_dir,
        config.paths.cache_dir,
        config.paths.evidence_dir,
        config.paths.quarantine_dir,
    )
    for scope in scopes:
        root = scope.normalized_path
        if not root.is_dir():
            partial = True
            continue
        try:
            root_stat = os.lstat(root)
        except OSError as error:
            entries.append(
                _entry(
                    snapshot_id,
                    scope.scope_id,
                    ".",
                    root,
                    None,
                    FilesystemObservationStatus.PERMISSION_DENIED
                    if error.errno == 13
                    else FilesystemObservationStatus.IO_ERROR,
                    error,
                )
            )
            partial = True
            continue
        entries.append(_entry(snapshot_id, scope.scope_id, ".", root, root_stat))
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and not partial:
            directory, depth = stack.pop()
            if budget.max_depth is not None and depth >= budget.max_depth:
                continue
            try:
                children = list(os.scandir(directory))
            except OSError:
                partial = True
                continue
            for child in children:
                if (
                    len(entries) >= budget.max_entries
                    or time.monotonic() - started >= budget.max_duration_seconds
                ):
                    partial = True
                    break
                path = Path(child.path)
                relative = _relative(path, root)
                try:
                    info = child.stat(follow_symlinks=False)
                except FileNotFoundError as error:
                    entries.append(
                        _entry(
                            snapshot_id,
                            scope.scope_id,
                            relative,
                            path,
                            None,
                            FilesystemObservationStatus.NOT_FOUND,
                            error,
                        )
                    )
                    partial = True
                    continue
                except PermissionError as error:
                    entries.append(
                        _entry(
                            snapshot_id,
                            scope.scope_id,
                            relative,
                            path,
                            None,
                            FilesystemObservationStatus.PERMISSION_DENIED,
                            error,
                        )
                    )
                    partial = True
                    continue
                except OSError as error:
                    entries.append(
                        _entry(
                            snapshot_id,
                            scope.scope_id,
                            relative,
                            path,
                            None,
                            FilesystemObservationStatus.IO_ERROR,
                            error,
                        )
                    )
                    partial = True
                    continue
                skipped = any(
                    path == item or item in path.parents for item in (*excluded, *internal)
                )
                entries.append(
                    _entry(snapshot_id, scope.scope_id, relative, path, info, excluded=skipped)
                )
                if skipped:
                    continue
                if stat.S_ISREG(info.st_mode):
                    byte_total += info.st_size
                    if (
                        budget.max_total_stat_bytes is not None
                        and byte_total > budget.max_total_stat_bytes
                    ):
                        partial = True
                        break
                if stat.S_ISDIR(info.st_mode) and (
                    scope.allow_cross_mount or info.st_dev == root_stat.st_dev
                ):
                    stack.append((path, depth + 1))
    return tuple(
        sorted(
            entries,
            key=lambda item: (item.scope_id, item.relative_path.encode("utf-8", "surrogateescape")),
        )
    ), partial
