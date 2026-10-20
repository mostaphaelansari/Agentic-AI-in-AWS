"""Client disconnect must stop the Custom Runtime agent, not just the stream.

The entrypoint is a generator, so a hung-up client closes it and raises
GeneratorExit at the yield. That is where Strands' public, thread-safe
cancellation is triggered, and the partial usage still has to be reported so a
stopped run can be priced instead of recorded as zero.
"""

from __future__ import annotations

import importlib.util
import queue
import threading
from pathlib import Path

AGENT_PATH = Path(__file__).parents[2] / "agents" / "custom" / "agent.py"
SPEC = importlib.util.spec_from_file_location("custom_agent_for_cancel_test", AGENT_PATH)
custom_agent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(custom_agent)


class _FakeAgent:
    """Streams one delta, then blocks like a real invocation until cancelled."""

    def __init__(self, event_queue=None) -> None:
        self.cancelled = threading.Event()
        self.event_queue = event_queue
        self.stop_reason: str | None = None

    def __call__(self, prompt):
        if self.event_queue is not None:
            self.event_queue.put({"type": "text", "content": "Planning"})
        self.cancelled.wait(timeout=10)
        self.stop_reason = "cancelled"
        return _FakeResult()

    def cancel(self) -> None:
        self.cancelled.set()


class _FakeResult:
    class metrics:
        accumulated_usage = {
            "inputTokens": 120,
            "outputTokens": 30,
            "totalTokens": 150,
        }
        cycle_count = 1


class _FakeToolHook:
    count = 2
    tool_names = ["get_weather"]


def _build(monkeypatch, agent: _FakeAgent):
    def fake_build_agent(event_queue, **_kwargs):
        agent.event_queue = event_queue
        return agent, _FakeToolHook(), type("Cb", (), {"saw_reasoning": False})()

    monkeypatch.setattr(custom_agent, "_build_agent", fake_build_agent)
    monkeypatch.delenv("GATEWAY_URL", raising=False)


def test_client_disconnect_cancels_the_strands_loop(monkeypatch) -> None:
    agent = _FakeAgent()
    _build(monkeypatch, agent)

    stream = custom_agent.invoke(
        {"prompt": "plan a trip"},
        type("Ctx", (), {"session_id": "streamlit_demo-abc--def"})(),
    )
    # Cancellation is delivered at a yield, so consume the first streamed event
    # the way a live client would before hanging up.
    assert next(stream)["content"] == "Planning"
    # Closing the generator is exactly what Starlette does on disconnect.
    stream.close()

    assert agent.cancelled.is_set()


def test_partial_usage_is_emitted_and_logged_for_a_cancelled_run(monkeypatch, caplog) -> None:
    event_queue: queue.Queue = queue.Queue()
    result = _FakeResult()

    with caplog.at_level("INFO"):
        custom_agent._emit_metrics(
            event_queue,
            result,
            _FakeToolHook(),
            session_id="streamlit_demo-abc--def",
        )

    event = event_queue.get_nowait()
    assert event["type"] == "metrics"
    assert event["usage"]["totalTokens"] == 150
    assert event["tool_calls"] == 2
    # The session-keyed line is what the UI reads back to price a stopped run.
    assert any(
        "comparison_token_usage=" in record.message and "streamlit_demo-abc--def" in record.message
        for record in caplog.records
    )
