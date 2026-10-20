"""Tests for routing-decision telemetry on the MLflow-exported root span."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from opentelemetry import trace

AGENT_PATH = Path(__file__).parents[2] / "agents" / "custom" / "agent.py"
SPEC = importlib.util.spec_from_file_location("custom_agent_for_router_telemetry_test", AGENT_PATH)
custom_agent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(custom_agent)


class RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.events: list[tuple[str, dict]] = []

    def is_recording(self) -> bool:
        return True

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        self.events.append((name, attributes or {}))


def test_router_decision_is_attached_to_mlflow_exported_root_span(monkeypatch) -> None:
    span = RecordingSpan()
    monkeypatch.setattr(trace, "get_current_span", lambda: span)

    custom_agent._annotate_current_invocation_span(
        prompt="What is the weather in Paris?",
        mode="weather",
        weather_only=True,
        model_id="eu.example.haiku",
        model_tier="cheap",
        model_route_reason="LLMRouter chose economy",
        model_router_mode="auto",
        model_router_name="knnrouter",
        model_router_version="router-v3",
        model_router_candidate="economy",
        model_router_outcome="selected",
        model_router_fallback=False,
        model_router_latency_ms=12.75,
    )

    assert span.attributes["agent.model.router.mode"] == "auto"
    assert span.attributes["agent.model.router.name"] == "knnrouter"
    assert span.attributes["agent.model.router.version"] == "router-v3"
    assert span.attributes["agent.model.router.candidate"] == "economy"
    assert span.attributes["agent.model.router.outcome"] == "selected"
    assert span.attributes["agent.model.router.fallback"] is False
    assert span.attributes["agent.model.router.latency_ms"] == 12.75
    assert span.attributes["agent.model.selected_tier"] == "cheap"
    assert span.attributes["agent.model.selected_id"] == "eu.example.haiku"
