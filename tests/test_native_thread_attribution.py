"""Thread-correlation provenance for the native Codex surface."""

from __future__ import annotations

import json

from local_steward.native_mcp_server import thread_attribution_machine_object


def test_host_session_meta_produces_stable_non_authoritative_signature() -> None:
    first = thread_attribution_machine_object({"openai/session": "codex-thread-006"})
    second = thread_attribution_machine_object(
        {"openai/session": "codex-thread-006", "openai/locale": "zh-CN"}
    )

    assert first == second
    assert first["status"] == "HOST_BOUND"
    assert first["thread_reference"] == "codex-thread-006"
    assert len(first["thread_signature"]) == 64
    assert first["source"] == "MCP_REQUEST_META_OPENAI_SESSION"
    assert first["correlation_only"] is True
    assert first["authorization_effect"] == "NONE"
    assert first["model_supplied"] is False


def test_absent_or_invalid_host_session_meta_is_explicitly_unavailable() -> None:
    absent = thread_attribution_machine_object(None)
    invalid = thread_attribution_machine_object({"openai/session": " model supplied "})

    assert absent["status"] == "HOST_UNAVAILABLE"
    assert absent["reason_code"] == "MCP_CLIENT_SESSION_META_ABSENT"
    assert absent["thread_reference"] is None
    assert absent["thread_signature"] is None
    assert invalid["status"] == "HOST_UNAVAILABLE"
    assert invalid["reason_code"] == "MCP_CLIENT_SESSION_META_INVALID"
    assert "model supplied" not in json.dumps(invalid)
