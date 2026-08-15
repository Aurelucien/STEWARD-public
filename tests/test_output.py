import json
from uuid import UUID

from local_steward.output import envelope, json_text, safe_text


def test_envelope_is_json_and_has_uuid() -> None:
    payload = json.loads(json_text(envelope("test", "OK", {})))
    UUID(payload["run_id"])
    assert set(payload) == {
        "schema_version",
        "command",
        "status",
        "run_id",
        "result",
        "errors",
        "warnings",
    }


def test_human_text_escapes_controls() -> None:
    assert safe_text("one\n\x1b[2m") == "one\\n\\x1b[2m"
