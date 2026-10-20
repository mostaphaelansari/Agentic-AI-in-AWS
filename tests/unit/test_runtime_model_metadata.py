"""Custom Runtime model-metadata compatibility tests."""

from __future__ import annotations

import json
import queue
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from backends import runtime  # noqa: E402
from backends.runtime import STRONG_MODEL_ID, _with_legacy_model_metadata  # noqa: E402
from components.metrics import (  # noqa: E402
    classifier_display,
    model_display_name,
    router_latency_display,
)


def test_legacy_runtime_metrics_are_labeled_as_actual_strong_model() -> None:
    event = _with_legacy_model_metadata(
        {"type": "metrics", "usage": {"inputTokens": 10}},
        {"model_tier": "cheap"},
    )

    assert event["model_id"] == STRONG_MODEL_ID
    assert event["model_tier"] == "strong"
    assert event["requested_model_tier"] == "cheap"
    assert event["model_metadata_status"] == "legacy-runtime"
    assert "redeploy required" in event["model_route_reason"]


def test_resolved_runtime_metrics_are_not_overwritten() -> None:
    event = {
        "type": "metrics",
        "model_id": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        "model_tier": "cheap",
    }

    assert _with_legacy_model_metadata(event, {"model_tier": "cheap"}) is event


def test_model_chip_names_the_exact_model_family() -> None:
    assert (
        model_display_name(
            {
                "model_id": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
                "model_tier": "cheap",
            }
        )
        == "Claude Haiku 4.5 · Cheap"
    )
    assert (
        model_display_name(
            {
                "model_id": "eu.anthropic.claude-sonnet-4-6",
                "model_tier": "strong",
                "model_metadata_status": "legacy-runtime",
            }
        )
        == "Claude Sonnet 4.6 · Legacy Runtime"
    )


def test_router_latency_is_formatted_in_milliseconds() -> None:
    assert router_latency_display({"model_router_latency_ms": 12.75}) == "12.750 ms"
    assert router_latency_display({}) == "-"
    assert router_latency_display({"model_router_latency_ms": "invalid"}) == "-"


def test_classifier_path_is_visible_and_unambiguous() -> None:
    assert (
        classifier_display(
            {
                "model_router_classifier_path": "llm",
                "model_router_classifier_model": "mistral.ministral-3-3b-instruct",
                "model_router_classifier_tier": "complex",
            }
        )
        == "Ministral 3B · LLM · COMPLEX"
    )
    assert (
        classifier_display(
            {
                "model_router_classifier_path": "heuristic_fallback",
                "model_router_classifier_tier": "SIMPLE",
            }
        )
        == "Heuristic fallback · SIMPLE"
    )
    assert classifier_display({"model_router_classifier_path": "router_fallback"}) == (
        "Router fallback"
    )
    assert classifier_display({"model_router_classifier_path": "manual"}) == "Manual override"
    assert classifier_display({"model_router_classifier_path": "policy_override"}) == (
        "Strong-model policy"
    )
    assert classifier_display({}) == "-"


def test_runtime_payload_carries_router_telemetry(monkeypatch) -> None:
    payloads: list[dict] = []

    class Body:
        def read(self) -> bytes:
            return b"{}"

        def close(self) -> None:
            pass

    class Client:
        def invoke_agent_runtime(self, **kwargs):
            payloads.append(json.loads(kwargs["payload"]))
            return {"response": Body(), "contentType": "application/json"}

    monkeypatch.setattr(runtime, "get_client", lambda _region: Client())
    events: queue.Queue = queue.Queue()
    options = {
        "model_tier": "cheap",
        "model_route_reason": "LLMRouter chose economy",
        "model_router_mode": "auto",
        "model_router_name": "knnrouter",
        "model_router_version": "router-v3",
        "model_router_candidate": "economy",
        "model_router_outcome": "selected",
        "model_router_fallback": False,
        "model_router_latency_ms": 12.75,
    }

    runtime.invoke_runtime(
        {"region": "eu-west-1", "arn": "runtime-arn"},
        "session-id",
        "Hello",
        events,
        options,
    )

    payload = payloads[0]
    assert payload.pop("prompt").endswith("\nHello")
    assert payload == {"enable_caching": False, "enable_thinking": False, **options}


def test_runtime_payload_carries_reasoning_effort(monkeypatch) -> None:
    payloads: list[dict] = []

    class Body:
        def read(self) -> bytes:
            return b"{}"

        def close(self) -> None:
            pass

    class Client:
        def invoke_agent_runtime(self, **kwargs):
            payloads.append(json.loads(kwargs["payload"]))
            return {"response": Body(), "contentType": "application/json"}

    monkeypatch.setattr(runtime, "get_client", lambda _region: Client())

    runtime.invoke_runtime(
        {"region": "eu-west-1", "arn": "runtime-arn"},
        "session-id",
        "Hello",
        queue.Queue(),
        {"reasoning_effort": "high"},
    )

    assert payloads[0]["enable_thinking"] is True
    assert payloads[0]["reasoning_effort"] == "high"
