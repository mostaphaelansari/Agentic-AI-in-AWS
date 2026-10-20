"""Tests for the route-only LiteLLM Auto Router adapter."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from services import litellm_complexity_router as adapter  # noqa: E402

CHEAP_ID = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
STRONG_ID = "eu.anthropic.claude-sonnet-4-6"
CLASSIFIER_ID = "mistral.ministral-3-3b-instruct"
CLASSIFIER_REGION = "eu-west-1"
CLASSIFIER_TIMEOUT_MS = 750
CLASSIFIER_INPUT_PER_MTOK = 0.12
CLASSIFIER_OUTPUT_PER_MTOK = 0.12


@pytest.fixture(autouse=True)
def clear_router_cache():
    adapter._load_router.cache_clear()
    yield
    adapter._load_router.cache_clear()


def _route(prompt: str, tiers: str = "", boundaries: str = "", messages=None):
    return adapter.route_model_group(
        prompt,
        tiers,
        boundaries,
        CHEAP_ID,
        STRONG_ID,
        CLASSIFIER_ID,
        CLASSIFIER_REGION,
        CLASSIFIER_TIMEOUT_MS,
        CLASSIFIER_INPUT_PER_MTOK,
        CLASSIFIER_OUTPUT_PER_MTOK,
        messages,
    )


def _fake_router(monkeypatch, *, tier: str, score=None, cause: str = "llm_classifier"):
    call_kwargs = {}

    class FakeDeploymentRouter:
        def completion(self, **kwargs):
            call_kwargs.update(kwargs)
            if cause != "llm_classifier":
                raise TimeoutError("classifier timed out")
            message = SimpleNamespace(content=f'{{"tier":"{tier}"}}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeRouter:
        litellm_router_instance = FakeDeploymentRouter()

        def classify(self, _prompt):
            return SimpleNamespace(value=tier), score, [f"{cause}:{tier}"]

        def get_model_for_tier(self, selected_tier):
            value = getattr(selected_tier, "value", selected_tier)
            return "cheap" if value == "SIMPLE" else "strong"

    monkeypatch.setattr(adapter, "_load_router", lambda *_args: FakeRouter())
    return call_kwargs


def test_llm_classifier_sends_a_simple_ask_to_cheap(monkeypatch) -> None:
    call_kwargs = _fake_router(monkeypatch, tier="SIMPLE")

    decision = _route("What is the capital of France?")

    assert decision.tier == "SIMPLE"
    assert decision.model_group == "cheap"
    assert decision.score is None
    assert decision.cause == "llm_classifier"
    assert decision.signals
    assert call_kwargs["temperature"] == 0


def test_llm_classifier_sends_a_complex_ask_to_strong(monkeypatch) -> None:
    _fake_router(monkeypatch, tier="COMPLEX")

    decision = _route("Refactor this application and debug the distributed system")

    assert decision.tier == "COMPLEX"
    assert decision.model_group == "strong"


def test_llm_classifier_includes_bounded_recent_user_context(monkeypatch) -> None:
    call_kwargs = _fake_router(monkeypatch, tier="COMPLEX")
    old_context = "x" * (adapter.CLASSIFIER_CONTEXT_PER_TURN_CHARS + 50)

    _route(
        "What about day 3?",
        messages=[
            {"role": "user", "content": "Plan a detailed two-week itinerary"},
            {"role": "assistant", "content": "Architecture-specific answer"},
            {"role": "user", "content": old_context},
            {"role": "user", "content": "What about day 3?"},
        ],
    )

    classifier_prompt = call_kwargs["messages"][1]["content"]
    assert "Plan a detailed two-week itinerary" in classifier_prompt
    assert old_context[: adapter.CLASSIFIER_CONTEXT_PER_TURN_CHARS] in classifier_prompt
    assert old_context not in classifier_prompt
    assert "Architecture-specific answer" not in classifier_prompt
    assert classifier_prompt.endswith("Classify this message:\nWhat about day 3?")


def test_heuristic_fallback_cause_is_preserved(monkeypatch) -> None:
    _fake_router(monkeypatch, tier="REASONING", score=0.9, cause="heuristic_scorer")

    decision = _route("Think step by step and reason through the trade-offs carefully")

    assert decision.tier == "REASONING"
    assert decision.model_group == "strong"
    assert decision.score == 0.9
    assert decision.cause == "heuristic_scorer"


def test_router_is_built_once_per_configuration() -> None:
    args = (
        "",
        "",
        CHEAP_ID,
        STRONG_ID,
        CLASSIFIER_ID,
        CLASSIFIER_REGION,
        CLASSIFIER_TIMEOUT_MS,
        CLASSIFIER_INPUT_PER_MTOK,
        CLASSIFIER_OUTPUT_PER_MTOK,
    )

    first = adapter._load_router(*args)
    second = adapter._load_router(*args)

    assert first is second
    assert first.config.classifier_type == "llm"
    assert first.config.classifier_llm_config.model == "classifier"
    assert first.config.classifier_llm_config.timeout_ms == CLASSIFIER_TIMEOUT_MS
    assert first.config.classifier_context_window_size == 4
    assert first.config.classifier_context_per_turn_chars == 500
    assert first.config.classifier_context_include_assistant_turns is False
    assert first.litellm_router_instance.num_retries == 0


def test_parse_tiers_defaults_reserve_cheap_for_simple() -> None:
    assert adapter.parse_tiers("") == {
        "SIMPLE": "cheap",
        "MEDIUM": "strong",
        "COMPLEX": "strong",
        "REASONING": "strong",
    }


def test_parse_tiers_rejects_unknown_tiers_and_groups() -> None:
    with pytest.raises(adapter.ComplexityRouterError):
        adapter.parse_tiers('{"TRIVIAL":"cheap"}')
    with pytest.raises(adapter.ComplexityRouterError):
        adapter.parse_tiers('{"SIMPLE":"gpt-4o"}')
    with pytest.raises(adapter.ComplexityRouterError):
        adapter.parse_tiers("not json")


def test_parse_tier_boundaries_rejects_unknown_keys_and_non_numbers() -> None:
    assert adapter.parse_tier_boundaries("") == {}
    assert adapter.parse_tier_boundaries('{"simple_medium":0.2}') == {"simple_medium": 0.2}
    with pytest.raises(adapter.ComplexityRouterError):
        adapter.parse_tier_boundaries('{"simple_hard":0.2}')
    with pytest.raises(adapter.ComplexityRouterError):
        adapter.parse_tier_boundaries('{"simple_medium":"low"}')


def test_model_list_declares_only_the_mistral_classifier_deployment() -> None:
    model_list = adapter.build_model_list(
        CLASSIFIER_ID,
        CLASSIFIER_REGION,
        CLASSIFIER_INPUT_PER_MTOK,
        CLASSIFIER_OUTPUT_PER_MTOK,
    )

    assert [entry["model_name"] for entry in model_list] == ["classifier"]
    assert model_list[0]["litellm_params"] == {
        "model": f"bedrock/{CLASSIFIER_ID}",
        "aws_region_name": CLASSIFIER_REGION,
    }
    assert model_list[0]["model_info"]["input_cost_per_token"] == pytest.approx(1.2e-7)
    assert model_list[0]["model_info"]["output_cost_per_token"] == pytest.approx(1.2e-7)
    assert model_list[0]["model_info"]["cache_creation_input_token_cost"] == 0.0
    assert model_list[0]["model_info"]["cache_read_input_token_cost"] == 0.0


def test_missing_litellm_raises_a_router_error(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "litellm", None)

    with pytest.raises(adapter.ComplexityRouterError):
        _route("Hello")


def test_router_version_reports_the_installed_litellm() -> None:
    assert adapter.router_version().startswith("litellm-")
