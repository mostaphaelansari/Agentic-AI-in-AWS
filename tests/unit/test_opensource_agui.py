"""AG-UI adapter tests: internal event stream -> AG-UI protocol events."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "opensource" / "agents" / "custom"

sys.path.insert(0, str(SERVICE_DIR))
import agui  # noqa: E402
import service  # noqa: E402


def _decode(chunks: list[str]) -> list[dict]:
    events = []
    for chunk in chunks:
        assert chunk.startswith("data: ")
        events.append(json.loads(chunk.removeprefix("data: ").strip()))
    return events


def _run(internal_events: list[dict]) -> list[dict]:
    async def inner():
        for event in internal_events:
            yield f"data: {json.dumps(event)}\n\n"

    async def collect():
        return [chunk async for chunk in agui.encode_run(inner(), thread_id="t-1", run_id="r-1")]

    return _decode(asyncio.run(collect()))


def test_run_wraps_text_and_tools_in_agui_events():
    events = _run(
        [
            {"type": "reasoning", "content": "thinking..."},
            {"type": "text", "content": "Checking "},
            {
                "type": "tool_start",
                "tool_use_id": "tool-1",
                "name": "searchLocation",
                "input": {"query": "Antibes"},
            },
            # Intentional hook-side repeat with the completed input.
            {
                "type": "tool_start",
                "tool_use_id": "tool-1",
                "name": "searchLocation",
                "input": {"query": "Antibes, France"},
            },
            {
                "type": "tool_end",
                "tool_use_id": "tool-1",
                "name": "searchLocation",
                "result": "Antibes, France",
            },
            {"type": "text", "content": "It is mild."},
            {"type": "metrics", "usage": {}, "tool_calls": 1},
        ]
    )

    types = [event["type"] for event in events]
    assert types == [
        "RUN_STARTED",
        "THINKING_START",
        "THINKING_TEXT_MESSAGE_START",
        "THINKING_TEXT_MESSAGE_CONTENT",
        "THINKING_TEXT_MESSAGE_END",
        "THINKING_END",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_ARGS",  # repeat by id adds args only, no second START
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "CUSTOM",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]

    start = next(e for e in events if e["type"] == "TOOL_CALL_START")
    assert start["toolCallId"] == "tool-1"
    assert start["toolCallName"] == "searchLocation"
    result = next(e for e in events if e["type"] == "TOOL_CALL_RESULT")
    assert result["content"] == "Antibes, France"
    metrics = next(e for e in events if e["type"] == "CUSTOM")
    assert metrics["name"] == "metrics"
    assert events[0]["threadId"] == "t-1"
    assert events[-1]["runId"] == "r-1"


def test_run_error_ends_stream_without_run_finished():
    events = _run(
        [
            {"type": "text", "content": "partial"},
            {"type": "error", "content": "model exploded"},
        ]
    )
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["message"] == "model exploded"
    assert "RUN_FINISHED" not in [e["type"] for e in events]
    # The open text message is closed before the error.
    assert events[-2]["type"] == "TEXT_MESSAGE_END"


def test_extract_prompt_takes_last_user_message():
    run_input = agui.RunAgentInput.model_validate(
        {
            "threadId": "t-1",
            "runId": "r-1",
            "state": {},
            "messages": [
                {"id": "1", "role": "user", "content": "old question"},
                {"id": "2", "role": "assistant", "content": "old answer"},
                {"id": "3", "role": "user", "content": "Weather in Antibes?"},
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )
    assert agui.extract_prompt(run_input) == "Weather in Antibes?"


def test_agui_endpoint_streams_protocol_events(monkeypatch):
    from fastapi.testclient import TestClient

    class FakeAgent:
        async def stream_async(self, prompt):
            yield {"data": "It is mild."}

    monkeypatch.setattr(service, "_start_mcp_client", lambda: (None, None))
    monkeypatch.setattr(service, "_build_agent", lambda tool_hook, *args, **kwargs: FakeAgent())

    client = TestClient(service.app)
    response = client.post(
        "/agui",
        json={
            "threadId": "t-9",
            "runId": "r-9",
            "state": {},
            "messages": [{"id": "1", "role": "user", "content": "Weather?"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
    )
    assert response.status_code == 200
    events = _decode([line + "\n\n" for line in response.text.split("\n\n") if line.strip()])
    types = [event["type"] for event in events]
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    assert "TEXT_MESSAGE_CONTENT" in types
    assert events[0]["threadId"] == "t-9"


def test_agui_endpoint_rejects_invalid_payload():
    from fastapi.testclient import TestClient

    client = TestClient(service.app)
    assert client.post("/agui", json={"nope": True}).status_code == 422
