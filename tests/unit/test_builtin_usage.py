"""Measured AgentCore Browser and Code Interpreter usage."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from services import builtin_usage  # noqa: E402


def _event(session_id: str, vcpu: float, memory: float) -> dict:
    return {
        "message": json.dumps(
            {
                "attributes": {"session.id": session_id, "time_elapsed_seconds": 1},
                "metrics": {
                    "codeInterpreter.vcpu.hours.used": vcpu,
                    "codeInterpreter.memory.gb_hours.used": memory,
                },
            }
        )
    }


def test_usage_records_are_priced_with_agentcore_compute_rates() -> None:
    usage = builtin_usage._usage_from_events(
        [_event("tool-session", 0.25, 0.5), _event("tool-session", 0.75, 1.5)]
    )

    assert usage["status"] == builtin_usage.BUILTIN_USAGE_AVAILABLE
    assert usage["usage_records"] == 2
    assert usage["vcpu_hours"] == pytest.approx(1.0)
    assert usage["memory_gb_hours"] == pytest.approx(2.0)
    assert usage["cpu_cost"] == pytest.approx(0.0895)
    assert usage["memory_cost"] == pytest.approx(0.0189)
    assert usage["builtin_cost"] == pytest.approx(0.1084)


def test_fetch_joins_the_app_session_name_to_the_tool_session(monkeypatch) -> None:
    app_session_id = "streamlit_demo-custom"
    tool_session_id = "01TOOLSESSION"

    class AgentCoreClient:
        def list_code_interpreter_sessions(self, **kwargs):
            assert kwargs["codeInterpreterIdentifier"] == "aws.codeinterpreter.v1"
            return {
                "items": [
                    {
                        "name": app_session_id,
                        "sessionId": tool_session_id,
                        "createdAt": datetime.now(timezone.utc),
                    }
                ]
            }

    class LogsClient:
        def get_log_events(self, **kwargs):
            assert kwargs["logStreamName"] == "BedrockAgentCoreCodeInterpreter_UsageLogs"
            return {"events": [_event(tool_session_id, 0.001, 0.002)]}

    def client(service_name, **_kwargs):
        return AgentCoreClient() if service_name == "bedrock-agentcore" else LogsClient()

    monkeypatch.setattr(builtin_usage.boto3, "client", client)

    usage = builtin_usage.fetch_builtin_usage_for_session(
        {
            "region": "eu-test-1",
            "service": "runtime",
            "builtin_usage_log_group": "agentcore-tools",
        },
        app_session_id,
        ["code_interpreter"],
        1000.0,
        1010.0,
    )

    assert usage["status"] == builtin_usage.BUILTIN_USAGE_AVAILABLE
    assert usage["tools"]["code_interpreter"]["session_id"] == tool_session_id
    assert usage["builtin_cost"] == pytest.approx(0.001 * 0.0895 + 0.002 * 0.00945)


def test_ambiguous_time_matches_are_not_attributed() -> None:
    class AgentCoreClient:
        def list_code_interpreter_sessions(self, **_kwargs):
            return {
                "items": [
                    {"name": "opaque-1", "sessionId": "one", "createdAt": 1001.0},
                    {"name": "opaque-2", "sessionId": "two", "createdAt": 1002.0},
                ]
            }

    match = builtin_usage._matching_tool_session(
        AgentCoreClient(),
        "code_interpreter",
        "custom-code",
        "outer-session",
        1000.0,
        1010.0,
    )

    assert match is None
