"""Thin official-Python-SDK adapter for the read-only filesystem MCP server."""

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

FILESYSTEM_READ_ONLY_ALLOWLIST = (
    "list_allowed_directories",
    "list_directory",
    "list_directory_with_sizes",
    "directory_tree",
    "search_files",
    "get_file_info",
)


class McpClientError(RuntimeError):
    """A safe turn-local filesystem MCP failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    """The subset of a server descriptor retained for one live session."""

    name: str
    input_schema: dict[str, Any]
    read_only_hint: bool


@dataclass(frozen=True, slots=True)
class McpToolResult:
    """Structured MCP observation data; it remains untrusted tool output."""

    tool_name: str
    content: tuple[dict[str, Any], ...]
    structured_content: dict[str, Any] | None
    is_error: bool


SessionFactory = Any


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _json_value(value: object) -> Any:
    """Convert MCP Pydantic values to JSON-only data without interpreting text."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _object(value: object, *, message: str) -> dict[str, Any]:
    normalized = _json_value(value)
    if not isinstance(normalized, dict):
        raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", message)
    return normalized


@asynccontextmanager
async def _official_session(
    command: str, args: tuple[str, ...], env: Mapping[str, str] | None = None
) -> AsyncIterator[Any]:
    """Use the audited mcp==1.28.1 stdio lifecycle without a second protocol."""
    parameters = StdioServerParameters(command=command, args=list(args), env=dict(env) if env is not None else None)
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            yield session


@dataclass(slots=True)
class FilesystemMcpClient:
    """Session-local discovery, hard allowlisting, dispatch, and cleanup only."""

    allowed_root: Path
    command: str = "npx"
    package: str = "@modelcontextprotocol/server-filesystem"
    session_factory: SessionFactory | None = None
    subprocess_env: Mapping[str, str] | None = None
    _stack: AsyncExitStack | None = None
    _session: Any = None
    _descriptors: dict[str, McpToolDescriptor] | None = None
    _observed_tool_names: tuple[str, ...] | None = None

    async def __aenter__(self) -> "FilesystemMcpClient":
        if self._stack is not None:
            raise McpClientError("INTERNAL_INVARIANT_FAILED", "MCP client is already open")
        root = self.allowed_root.resolve(strict=True)
        if not root.is_dir():
            raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "allowed root is not a directory")
        # The server compares its normalized real root lexically.  Keep the
        # exact value supplied to it for subsequent caller-visible requests.
        self.allowed_root = root
        self._stack = AsyncExitStack()
        try:
            factory = self.session_factory
            context = (
                factory()
                if factory is not None
                else _official_session(self.command, ("--yes", self.package, str(root)), self.subprocess_env)
            )
            session = await self._stack.enter_async_context(context)
            self._session = session
            await session.initialize()
            await self.discover_tools()
        except McpClientError:
            await self.aclose()
            raise
        except Exception as error:
            await self.aclose()
            raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP session is unavailable") from error
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        self._descriptors = None
        self._observed_tool_names = None
        if stack is not None:
            await stack.aclose()

    @property
    def descriptors(self) -> tuple[McpToolDescriptor, ...]:
        if self._descriptors is None:
            raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP session is not open")
        return tuple(self._descriptors[name] for name in FILESYSTEM_READ_ONLY_ALLOWLIST)

    @property
    def observed_tool_names(self) -> tuple[str, ...]:
        """Names discovered from this live server session, before filtering."""
        if self._observed_tool_names is None:
            raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP session is not open")
        return self._observed_tool_names

    async def discover_tools(self) -> tuple[McpToolDescriptor, ...]:
        """List server tools and retain only the frozen local metadata allowlist."""
        if self._session is None:
            raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP session is not open")
        observed: list[object] = []
        cursor: str | None = None
        for _ in range(8):
            try:
                page = await self._session.list_tools(cursor)
            except Exception as error:
                raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP tool listing failed") from error
            tools = _field(page, "tools")
            if not isinstance(tools, list):
                raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP tool list is invalid")
            observed.extend(tools)
            next_cursor = _field(page, "nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP continuation is invalid")
            cursor = next_cursor
        else:
            raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP tool list is unbounded")

        descriptors: dict[str, McpToolDescriptor] = {}
        for item in observed:
            name = _field(item, "name")
            schema = _field(item, "inputSchema")
            annotations = _field(item, "annotations")
            hint = _field(annotations, "readOnlyHint")
            if not isinstance(name, str) or not isinstance(schema, dict) or not isinstance(hint, bool):
                raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP tool descriptor is invalid")
            if name in descriptors:
                raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP tool names are not unique")
            if name in FILESYSTEM_READ_ONLY_ALLOWLIST:
                descriptors[name] = McpToolDescriptor(name, _object(schema, message="tool schema is invalid"), hint)

        missing = [name for name in FILESYSTEM_READ_ONLY_ALLOWLIST if name not in descriptors]
        if missing or any(not item.read_only_hint for item in descriptors.values()):
            raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP allowlist does not match protocol")
        self._descriptors = descriptors
        names = [_field(item, "name") for item in observed]
        self._observed_tool_names = tuple(sorted(name for name in names if isinstance(name, str)))
        return self.descriptors

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> McpToolResult:
        """Call one locally allowlisted tool after local JSON-schema validation."""
        if self._session is None or self._descriptors is None:
            raise McpClientError("FILESYSTEM_MCP_UNAVAILABLE", "filesystem MCP session is not open")
        descriptor = self._descriptors.get(name)
        if descriptor is None:
            raise McpClientError("TOOL_NOT_ALLOWED", "filesystem tool is not allowlisted")
        if arguments is not None and not isinstance(arguments, dict):
            raise McpClientError("TOOL_ARGUMENT_INVALID", "filesystem tool arguments must be an object")
        payload = {} if arguments is None else arguments
        try:
            Draft202012Validator(descriptor.input_schema).validate(payload)
        except (SchemaError, ValidationError) as error:
            raise McpClientError("TOOL_ARGUMENT_INVALID", "filesystem tool arguments do not match its schema") from error
        try:
            raw = await self._session.call_tool(name, payload)
        except Exception as error:
            raise McpClientError("FILESYSTEM_TOOL_FAILED", "filesystem MCP tool call failed") from error
        content = _field(raw, "content")
        if not isinstance(content, list):
            raise McpClientError("FILESYSTEM_TOOL_FAILED", "filesystem MCP result is invalid")
        normalized_content = tuple(_object(item, message="filesystem MCP content is invalid") for item in content)
        structured = _field(raw, "structuredContent")
        structured_content = None if structured is None else _object(structured, message="structured MCP result is invalid")
        is_error = _field(raw, "isError", False)
        if not isinstance(is_error, bool):
            raise McpClientError("FILESYSTEM_TOOL_FAILED", "filesystem MCP error flag is invalid")
        return McpToolResult(name, normalized_content, structured_content, is_error)
