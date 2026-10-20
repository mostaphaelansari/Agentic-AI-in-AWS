"""Tests for the open-source Ollama/Kimi LiteLLM Auto Router."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "opensource" / "agents" / "custom" / "litellm_auto_router.py"
SPEC = importlib.util.spec_from_file_location("opensource_litellm_auto_router", MODULE_PATH)
assert SPEC and SPEC.loader
router_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router_module)


def _config(**overrides):
    values = {
        "router_model_name": "test-auto-router",
        "ollama_host": "http://ollama:11434",
        "ollama_model_id": "qwen3:1.7b",
        "kimi_base_url": "https://api.moonshot.ai/v1",
        "kimi_api_key": "test-kimi-key",
        "kimi_model_id": "kimi-k2.6",
        "kimi_temperature": 1.0,
        "kimi_timeout": 60.0,
        "kimi_max_tokens": 4096,
        "session_affinity_ttl_seconds": 600,
    }
    values.update(overrides)
    return values


def test_model_list_routes_only_simple_requests_to_ollama() -> None:
    models = router_module.build_router_model_list(**_config())
    deployments = {item["model_name"]: item for item in models}

    ollama = deployments[router_module.OLLAMA_MODEL_GROUP]
    kimi = deployments[router_module.KIMI_MODEL_GROUP]
    auto = deployments["test-auto-router"]["litellm_params"]
    tiers = auto["complexity_router_config"]["tiers"]

    assert ollama["litellm_params"] == {
        "model": "ollama_chat/qwen3:1.7b",
        "api_base": "http://ollama:11434",
    }
    assert kimi["litellm_params"]["model"] == "openai/kimi-k2.6"
    assert kimi["litellm_params"]["max_tokens"] == 4096
    assert tiers == {
        "SIMPLE": router_module.OLLAMA_MODEL_GROUP,
        "MEDIUM": router_module.KIMI_MODEL_GROUP,
        "COMPLEX": router_module.KIMI_MODEL_GROUP,
        "REASONING": router_module.KIMI_MODEL_GROUP,
    }
    assert auto["complexity_router_config"]["tier_boundaries"] == {
        "simple_medium": 0.20,
        "medium_complex": 0.35,
        "complex_reasoning": 0.60,
    }
    assert auto["complexity_router_config"]["session_affinity"] is True
    assert auto["complexity_router_default_model"] == router_module.KIMI_MODEL_GROUP


def test_create_router_configures_bidirectional_single_failover() -> None:
    captured = {}

    class FakeRouter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    created = router_module.create_router(**_config(), router_cls=FakeRouter)

    assert isinstance(created, FakeRouter)
    assert captured["num_retries"] == 0
    assert captured["max_fallbacks"] == 1
    assert captured["fallbacks"] == [
        {router_module.OLLAMA_MODEL_GROUP: [router_module.KIMI_MODEL_GROUP]},
        {router_module.KIMI_MODEL_GROUP: [router_module.OLLAMA_MODEL_GROUP]},
    ]


def test_create_router_requires_kimi_credential() -> None:
    with pytest.raises(RuntimeError, match="requires KIMI_API_KEY"):
        router_module.create_router(**_config(kimi_api_key=""), router_cls=object)


def test_model_adds_per_turn_affinity_metadata() -> None:
    captured = {}
    timing_calls = []
    response = object()

    class FakeRouter:
        async def acompletion(self, **kwargs):
            captured.update(kwargs)
            return response

    model = router_module.LiteLLMAutoRouterModel(
        router=FakeRouter(),
        router_model_name="test-auto-router",
        routing_session_id="turn-123",
        on_model_call=lambda: timing_calls.append("called"),
    )
    result = asyncio.run(
        model._router_completion(
            {
                "model": "test-auto-router",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "metadata": {"request": "original"},
            }
        )
    )

    assert result is response
    assert captured["metadata"] == {"request": "original", "session_id": "turn-123"}
    assert timing_calls == ["called"]


def test_model_records_one_router_operation_per_tool_loop() -> None:
    route_events = []

    class FakeRouter:
        async def acompletion(self, **kwargs):
            kwargs["metadata"]["routing_decision"] = {
                "cause": "heuristic_scorer",
                "tier": "SIMPLE",
                "routed_model": router_module.OLLAMA_MODEL_GROUP,
            }
            return object()

    model = router_module.LiteLLMAutoRouterModel(
        router=FakeRouter(),
        router_model_name="test-auto-router",
        routing_session_id="turn-123",
        on_route=route_events.append,
    )

    asyncio.run(model._router_completion({"messages": [], "metadata": {}}))
    asyncio.run(model._router_completion({"messages": [], "metadata": {}}))

    assert len(route_events) == 1
    assert route_events[0]["status"] == "OK"
    assert route_events[0]["router_model_name"] == "test-auto-router"
    assert route_events[0]["routing_decision"] == {
        "cause": "heuristic_scorer",
        "tier": "SIMPLE",
        "routed_model": router_module.OLLAMA_MODEL_GROUP,
    }
    assert route_events[0]["latency_ms"] >= 0
