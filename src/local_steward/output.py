"""One output envelope and safe human text rendering."""

import json
import re
import uuid
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

from .constants import SCHEMA_VERSION
from .errors import OutputSerializationError, StewardError
from .models import CommandEnvelope

_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")


def safe_text(value: object) -> str:
    """Escape controls and bidi controls before emitting human text."""
    text = str(value).replace("\\", "\\\\")
    text = (
        text.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t").replace("\x1b", "\\x1b")
    )
    return "".join(
        f"\\u{ord(char):04x}" if ord(char) < 32 or _BIDI.fullmatch(char) else char for char in text
    )


def to_jsonable(value: Any) -> Any:
    """Convert protocol models deterministically into JSON values."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return to_jsonable(asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def envelope(
    command: str,
    status: str,
    result: dict[str, Any],
    *,
    errors: list[dict[str, str]] | None = None,
    warnings: list[str] | None = None,
) -> CommandEnvelope:
    return CommandEnvelope(
        SCHEMA_VERSION, command, status, str(uuid.uuid4()), result, errors or [], warnings or []
    )


def error_envelope(command: str, error: StewardError) -> CommandEnvelope:
    return envelope(command, "ERROR", {}, errors=[{"code": error.code, "message": str(error)}])


def json_text(payload: CommandEnvelope) -> str:
    """Serialize a complete JSON document or surface a stable failure."""
    try:
        return json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise OutputSerializationError("unable to serialize command output") from error
