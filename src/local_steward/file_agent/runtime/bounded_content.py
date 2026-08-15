"""Project-owned bounded current-filesystem UTF-8 observation primitive.

This is the filesystem-observation boundary for File Agent V1.  It is not an
AgentRuntime filesystem shortcut and it does not alter the official metadata
MCP server.  Callers supply only a host-owned ScopeBinding identity plus a
root-relative path; this component owns descriptor-bound, bounded observation.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
from hashlib import sha256
import os
from pathlib import Path
import stat
from typing import Callable

from .scope_binding import ScopeBindings


MAX_CONTENT_BYTES_PER_READ = 8_192
MAX_CONTENT_BYTES_PER_AGENT_TURN = 16_384
MAX_CONTENT_READS_PER_ASSISTANT_BATCH = 1
MAX_SERIALIZED_BYTES_PER_AGENT_TURN = 65_536


@dataclass(frozen=True, slots=True)
class BoundedUtf8ContentResult:
    """One safe complete-file observation with no partial-content variant."""

    status: str
    scope_id: str
    relative_path: str
    source_size_bytes: int | None
    content_bytes_observed: int
    content: str | None
    observed_content_sha256: str | None

    def payload(self) -> dict[str, object]:
        """Return provider-neutral observational data, never a control message."""
        value: dict[str, object] = {
            "status": self.status,
            "source_kind": "CURRENT_FILESYSTEM_CONTENT",
            "scope_id": self.scope_id,
            "relative_path": self.relative_path,
            "observation_tool": "read_bounded_utf8_file",
            "source_size_bytes": self.source_size_bytes,
            "content_bytes_observed": self.content_bytes_observed,
        }
        if self.status in {"COMPLETE", "EMPTY"}:
            value["encoding"] = "UTF-8"
            value["content"] = self.content
            value["observed_content_sha256"] = self.observed_content_sha256
        return value


def _same_state(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


@dataclass(slots=True)
class ProjectOwnedBoundedTextMcp:
    """Descriptor-safe, bounded text primitive owned by this repository.

    The name makes its ownership explicit: it is the project-owned MCP-side
    observation primitive selected by C0.  Its direct tests run in process;
    later acceptance may host it through the established isolated MCP route.
    """

    bindings: ScopeBindings
    read_bytes: Callable[[int, int], bytes] = os.read

    def preflight(self, arguments: dict[str, object]) -> None:
        """Validate the host-owned scope and lexical relative path without I/O."""
        scope_id = arguments.get("scope_id")
        relative_path = arguments.get("relative_path")
        if not isinstance(scope_id, str) or not isinstance(relative_path, str):
            raise ValueError("scope and relative path are required")
        self.bindings.require(scope_id).resolve_relative_path(relative_path)

    def read_bounded_utf8_file(self, arguments: dict[str, object]) -> BoundedUtf8ContentResult:
        """Observe at most the frozen source bound plus one overflow sentinel."""
        scope_id = arguments["scope_id"]
        relative_path = arguments["relative_path"]
        if not isinstance(scope_id, str) or not isinstance(relative_path, str):
            raise ValueError("scope and relative path are required")
        binding = self.bindings.require(scope_id)
        binding.resolve_relative_path(relative_path)
        try:
            return self._read(binding.allowed_root, scope_id, relative_path)
        except OSError as error:
            status = (
                "UNAVAILABLE"
                if error.errno in {errno.EACCES, errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.ESTALE}
                else "TOOL_FAILED"
            )
            return BoundedUtf8ContentResult(status, scope_id, relative_path, None, 0, None, None)

    def _read(self, root: Path, scope_id: str, relative_path: str) -> BoundedUtf8ContentResult:
        root_resolved = root.resolve(strict=True)
        root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError(errno.ENOTSUP, "descriptor-safe no-follow open is unavailable")
        root_fd = os.open(root_resolved, root_flags)
        opened: list[int] = [root_fd]
        try:
            parent_fd = root_fd
            components = relative_path.split("/")
            for component in components[:-1]:
                child_fd = os.open(component, root_flags | nofollow, dir_fd=parent_fd)
                opened.append(child_fd)
                parent_fd = child_fd
            file_fd = os.open(components[-1], os.O_RDONLY | nofollow, dir_fd=parent_fd)
            opened.append(file_fd)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                return BoundedUtf8ContentResult("UNAVAILABLE", scope_id, relative_path, None, 0, None, None)
            if before.st_size > MAX_CONTENT_BYTES_PER_READ:
                return BoundedUtf8ContentResult(
                    "TOO_LARGE", scope_id, relative_path, before.st_size, 0, None, None
                )
            data = self.read_bytes(file_fd, MAX_CONTENT_BYTES_PER_READ + 1)
            if len(data) > MAX_CONTENT_BYTES_PER_READ:
                return BoundedUtf8ContentResult(
                    "TOO_LARGE", scope_id, relative_path, None, 0, None, None
                )
            after = os.fstat(file_fd)
            if not _same_state(before, after):
                return BoundedUtf8ContentResult("UNAVAILABLE", scope_id, relative_path, None, 0, None, None)
            try:
                text = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                return BoundedUtf8ContentResult(
                    "UNDECODABLE", scope_id, relative_path, before.st_size, 0, None, None
                )
            digest = sha256(data).hexdigest()
            status = "EMPTY" if not data else "COMPLETE"
            return BoundedUtf8ContentResult(
                status, scope_id, relative_path, before.st_size, len(data), text, digest
            )
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)
