"""Tests for the shared AgentCore model-routing policy."""

from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from config import BEDROCK_CHEAP_MODEL_ID, BEDROCK_STRONG_MODEL_ID  # noqa: E402
from services import litellm_complexity_router, model_router  # noqa: E402
from services.litellm_complexity_router import ComplexityDecision  # noqa: E402
from services.model_router import route_model, routed_options  # noqa: E402


def _decision(
    model_group: str,
    tier: str = "SIMPLE",
    score: float | None = None,
    cause: str = "llm_classifier",
) -> ComplexityDecision:
    return ComplexityDecision(
        model_group=model_group,
        tier=tier,
        score=score,
        signals=(),
        cause=cause,
    )


def test_auto_uses_litellm_complexity_selection(monkeypatch) -> None:
    monkeypatch.setattr(
        litellm_complexity_router,
        "route_model_group",
        lambda *_args: _decision("cheap"),
    )

    route = route_model("What is the weather in Paris?", {"model_routing": "auto"})

    assert route.tier == "cheap"
    assert route.model_id == BEDROCK_CHEAP_MODEL_ID
    assert route.router_name == "litellm-complexity-router"
    assert route.classifier_tier == "SIMPLE"
    assert route.reason == (
        "LiteLLM Ministral classifier (mistral.ministral-3-3b-instruct) "
        "scored SIMPLE (n/a) and selected cheap"
    )


def test_router_warmup_starts_once_without_blocking_startup(monkeypatch) -> None:
    calls: list[tuple] = []

    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            assert name == "model-router-warmup"
            assert daemon is True
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(model_router, "_WARMUP_THREAD", None)
    monkeypatch.setattr(model_router, "MODEL_ROUTER_WARMUP_ENABLED", True)
    monkeypatch.setattr(model_router, "MODEL_ROUTING_MODE", "auto")
    monkeypatch.setattr(model_router.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        litellm_complexity_router,
        "route_model_group",
        lambda *args: calls.append(args) or _decision("cheap"),
    )

    assert model_router.start_router_warmup() is True
    assert model_router.start_router_warmup() is False
    assert len(calls) == 1
    assert calls[0][0] == "Hello"


def test_router_warmup_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setattr(model_router, "_WARMUP_THREAD", None)
    monkeypatch.setattr(model_router, "MODEL_ROUTER_WARMUP_ENABLED", False)

    assert model_router.start_router_warmup() is False
    assert model_router._WARMUP_THREAD is None


def test_router_warmup_failure_does_not_escape_to_startup(monkeypatch) -> None:
    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            self.target = target

        def start(self):
            self.target()

    def fail(*_args):
        raise RuntimeError("Bedrock unavailable")

    monkeypatch.setattr(model_router, "_WARMUP_THREAD", None)
    monkeypatch.setattr(model_router, "MODEL_ROUTER_WARMUP_ENABLED", True)
    monkeypatch.setattr(model_router, "MODEL_ROUTING_MODE", "auto")
    monkeypatch.setattr(model_router.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(litellm_complexity_router, "route_model_group", fail)

    assert model_router.start_router_warmup() is True


def test_auto_uses_strong_model_for_reasoning_and_attachments(monkeypatch) -> None:
    prompts: list[str] = []

    def record(prompt, *_args):
        prompts.append(prompt)
        return _decision("cheap")

    monkeypatch.setattr(litellm_complexity_router, "route_model_group", record)

    thinking = route_model(
        "Explain this",
        {"model_routing": "auto", "enable_thinking": True},
    )
    uploaded = route_model(
        "Summarize it",
        {"model_routing": "auto", "uploaded_file": "measurements.csv"},
    )
    long_context = route_model("x" * 4_000, {"model_routing": "auto"})

    assert thinking.model_id == BEDROCK_STRONG_MODEL_ID
    assert thinking.outcome == "override_reasoning"
    assert uploaded.model_id == BEDROCK_STRONG_MODEL_ID
    assert uploaded.outcome == "override_attachment"
    assert long_context.model_id == BEDROCK_CHEAP_MODEL_ID
    assert prompts == ["x" * 4_000]


def test_explicit_reasoning_effort_selects_the_strong_model(monkeypatch) -> None:
    monkeypatch.setattr(
        litellm_complexity_router,
        "route_model_group",
        lambda *_args: (_ for _ in ()).throw(AssertionError("router should not run")),
    )

    route = route_model(
        "Explain this",
        {"model_routing": "auto", "reasoning_effort": "medium"},
    )

    assert route.model_id == BEDROCK_STRONG_MODEL_ID
    assert route.tier == "strong"
    assert route.outcome == "override_reasoning"
    assert "adaptive reasoning (medium effort)" in route.reason


def test_strong_model_requirements_override_manual_economy() -> None:
    reasoning = route_model(
        "Explain this",
        {"model_routing": "cheap", "reasoning_effort": "low"},
    )
    uploaded = route_model(
        "Analyze this",
        {"model_routing": "cheap", "uploaded_file": "measurements.csv"},
    )
    combined = routed_options(
        "Analyze carefully",
        {
            "model_routing": "cheap",
            "reasoning_effort": "high",
            "uploaded_file": "measurements.csv",
        },
    )

    assert reasoning.tier == "strong"
    assert uploaded.tier == "strong"
    assert combined["model_tier"] == "strong"
    assert combined["model_router_outcome"] == "override_reasoning_attachment"
    assert combined["model_router_classifier_path"] == "policy_override"


def test_auto_passes_the_configured_policy_and_bedrock_ids(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(model_router, "COMPLEXITY_TIERS", '{"MEDIUM":"cheap"}')
    monkeypatch.setattr(model_router, "COMPLEXITY_TIER_BOUNDARIES", '{"simple_medium":0.2}')
    monkeypatch.setattr(
        litellm_complexity_router,
        "route_model_group",
        lambda *args: calls.append(args) or _decision("cheap"),
    )

    route_model("Hello", {"model_routing": "auto"})

    assert calls == [
        (
            "Hello",
            '{"MEDIUM":"cheap"}',
            '{"simple_medium":0.2}',
            BEDROCK_CHEAP_MODEL_ID,
            BEDROCK_STRONG_MODEL_ID,
            model_router.MODEL_ROUTER_CLASSIFIER_MODEL_ID,
            model_router.MODEL_ROUTER_CLASSIFIER_REGION,
            model_router.MODEL_ROUTER_CLASSIFIER_TIMEOUT_MS,
            model_router.MODEL_ROUTER_CLASSIFIER_INPUT_PER_MTOK,
            model_router.MODEL_ROUTER_CLASSIFIER_OUTPUT_PER_MTOK,
        )
    ]


def test_routed_options_passes_context_only_to_the_classifier(monkeypatch) -> None:
    calls: list[tuple[tuple, dict]] = []

    def record(*args, **kwargs):
        calls.append((args, kwargs))
        return _decision("strong", tier="COMPLEX")

    monkeypatch.setattr(litellm_complexity_router, "route_model_group", record)
    messages = [
        {"role": "user", "content": "Design a distributed system"},
        {"role": "user", "content": "What about retries?"},
    ]

    options = routed_options(
        "What about retries?",
        {"model_routing": "auto", "_model_router_messages": messages},
    )

    assert calls[0][1] == {"messages": messages}
    assert "_model_router_messages" not in options


def test_explicit_tier_overrides_router() -> None:
    cheap = route_model("Implement and debug a distributed system", {"model_routing": "cheap"})
    strong = route_model("Hello", {"model_routing": "strong"})

    assert cheap.tier == "cheap"
    assert strong.tier == "strong"


def test_routed_options_preserves_flags_and_records_decision(monkeypatch) -> None:
    monkeypatch.setattr(
        litellm_complexity_router,
        "route_model_group",
        lambda *_args: _decision("strong", tier="COMPLEX"),
    )
    monkeypatch.setattr(model_router, "MODEL_ROUTER_VERSION", "litellm-1.96.2")

    options = routed_options(
        "Refactor this application",
        {"model_routing": "auto", "enable_caching": True},
    )

    assert options["enable_caching"] is True
    assert options["model_tier"] == "strong"
    assert options["model_id"] == BEDROCK_STRONG_MODEL_ID
    assert options["model_route_reason"] == (
        "LiteLLM Ministral classifier (mistral.ministral-3-3b-instruct) "
        "scored COMPLEX (n/a) and selected strong"
    )
    assert options["model_router_mode"] == "auto"
    assert options["model_router_name"] == "litellm-complexity-router"
    assert options["model_router_version"] == "litellm-1.96.2"
    assert options["model_router_candidate"] == "strong"
    assert options["model_router_outcome"] == "selected"
    assert options["model_router_fallback"] is False
    assert options["model_router_latency_ms"] >= 0
    assert options["model_router_classifier_model"] == "mistral.ministral-3-3b-instruct"
    assert options["model_router_classifier_path"] == "llm"
    assert options["model_router_classifier_tier"] == "COMPLEX"


def test_router_version_reports_the_installed_litellm_when_unpinned(monkeypatch) -> None:
    monkeypatch.setattr(
        litellm_complexity_router,
        "route_model_group",
        lambda *_args: _decision("cheap"),
    )
    monkeypatch.setattr(model_router, "MODEL_ROUTER_VERSION", "")

    route = route_model("Hello", {"model_routing": "auto"})

    assert route.router_version.startswith("litellm-")


def test_auto_fails_safely_when_the_complexity_router_is_unavailable(monkeypatch) -> None:
    def fail(*_args):
        raise litellm_complexity_router.ComplexityRouterError("not installed")

    monkeypatch.setattr(litellm_complexity_router, "route_model_group", fail)

    route = route_model("Hello", {"model_routing": "auto"})

    assert route.tier == "strong"
    assert route.model_id == BEDROCK_STRONG_MODEL_ID
    assert route.reason == "LiteLLM Auto Router unavailable; safe fallback"
    assert route.outcome == "fallback_unavailable"
    assert route.fallback is True


def test_auto_records_litellm_heuristic_classifier_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        litellm_complexity_router,
        "route_model_group",
        lambda *_args: _decision(
            "cheap",
            tier="SIMPLE",
            score=-0.2,
            cause="heuristic_scorer",
        ),
    )

    route = route_model("Hello", {"model_routing": "auto"})

    assert route.tier == "strong"
    assert route.model_id == BEDROCK_STRONG_MODEL_ID
    assert route.outcome == "fallback_heuristic_classifier"
    assert route.fallback is True
    assert route.reason == (
        "Ministral classifier failed; LiteLLM heuristic returned SIMPLE (-0.200), "
        "safe fallback selected strong"
    )


def test_routed_options_labels_heuristic_classifier_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        litellm_complexity_router,
        "route_model_group",
        lambda *_args: _decision(
            "cheap",
            tier="SIMPLE",
            score=-0.2,
            cause="heuristic_scorer",
        ),
    )

    options = routed_options("Hello", {"model_routing": "auto"})

    assert options["model_tier"] == "strong"
    assert options["model_id"] == BEDROCK_STRONG_MODEL_ID
    assert options["model_router_classifier_model"] == "mistral.ministral-3-3b-instruct"
    assert options["model_router_classifier_path"] == "heuristic_fallback"
    assert options["model_router_classifier_tier"] == "SIMPLE"


def test_auto_rejects_an_unallowlisted_model_group(monkeypatch) -> None:
    monkeypatch.setattr(
        litellm_complexity_router,
        "route_model_group",
        lambda *_args: _decision("arbitrary-model"),
    )

    route = route_model("Hello", {"model_routing": "auto"})

    assert route.tier == "strong"
    assert route.reason == (
        "LiteLLM Auto Router returned an unallowlisted model group; safe fallback"
    )
    assert route.candidate == "arbitrary-model"
    assert route.outcome == "fallback_unallowlisted"
    assert route.fallback is True


def test_auto_accepts_a_configured_bedrock_model_id_as_the_group(monkeypatch) -> None:
    monkeypatch.setattr(
        litellm_complexity_router,
        "route_model_group",
        lambda *_args: _decision(BEDROCK_CHEAP_MODEL_ID),
    )

    route = route_model("Hello", {"model_routing": "auto"})

    assert route.tier == "cheap"
    assert route.model_id == BEDROCK_CHEAP_MODEL_ID


def test_explicit_route_records_manual_router_telemetry() -> None:
    route = route_model("Hello", {"model_routing": "cheap"})

    assert route.mode == "cheap"
    assert route.router_name == "manual"
    assert route.router_version == "policy-v1"
    assert route.candidate == "cheap"
    assert route.outcome == "override"
    assert route.fallback is False
