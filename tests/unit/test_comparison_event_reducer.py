"""Tests for normalized comparison event reduction."""

from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from backends import harness  # noqa: E402
from backends.harness import _normalized_tool_input  # noqa: E402
from models.state import new_comparison_state  # noqa: E402
from services.event_reducer import apply_event  # noqa: E402


def test_reasoning_is_kept_separate_from_tool_trace() -> None:
    state = new_comparison_state("Managed agent", {"region": "eu-west-1"})

    apply_event(state, ("event", {"type": "reasoning", "content": "First thought. "}))
    apply_event(state, ("event", {"type": "reasoning", "content": "Second thought."}))
    apply_event(
        state,
        (
            "event",
            {
                "type": "tool_start",
                "tool_use_id": "tool-1",
                "name": "webSearch",
                "input": {"query": "x"},
            },
        ),
    )
    apply_event(
        state,
        (
            "event",
            {
                "type": "tool_end",
                "tool_use_id": "tool-1",
                "name": "webSearch",
                "result": "one result",
            },
        ),
    )

    assert state["reasoning"] == "First thought. Second thought."
    assert "First thought" not in state["trace"]
    assert "webSearch" not in state["reasoning"]
    assert state["tools"] == [
        {
            "id": "tool-1",
            "name": "webSearch",
            "input": {"query": "x"},
            "result": "one result",
            "summary": None,
        }
    ]
    assert state["status"] == "Responding"


def test_intermediate_retry_narration_stays_out_of_final_answer() -> None:
    state = new_comparison_state("Managed agent", {"region": "eu-west-1"})

    apply_event(state, ("event", {"type": "text", "content": "Let me parse the file."}))
    apply_event(
        state,
        (
            "event",
            {
                "type": "tool_start",
                "tool_use_id": "tool-1",
                "name": "code_interpreter",
                "input": {"code": "first attempt"},
            },
        ),
    )
    apply_event(
        state,
        (
            "event",
            {
                "type": "tool_end",
                "tool_use_id": "tool-1",
                "name": "code_interpreter",
                "result": "parse error",
            },
        ),
    )
    apply_event(
        state,
        ("event", {"type": "text", "content": "Let me try a different approach."}),
    )
    apply_event(
        state,
        (
            "event",
            {
                "type": "tool_start",
                "tool_use_id": "tool-2",
                "name": "code_interpreter",
                "input": {"code": "second attempt"},
            },
        ),
    )
    apply_event(
        state,
        (
            "event",
            {
                "type": "tool_end",
                "tool_use_id": "tool-2",
                "name": "code_interpreter",
                "result": "ok",
            },
        ),
    )
    apply_event(state, ("event", {"type": "text", "content": "Final supported answer."}))
    apply_event(state, ("done", None))

    assert state["answer"] == "Final supported answer."
    assert "Let me parse the file." in state["trace"]
    assert "Let me try a different approach." in state["trace"]
    assert state["pending_text"] == ""


def test_custom_model_selection_on_first_text_is_visible_before_final_metrics() -> None:
    state = new_comparison_state("Custom agent", {"region": "eu-west-1"})

    apply_event(
        state,
        (
            "event",
            {
                "type": "text",
                "content": "Starting",
                "model_id": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
                "model_tier": "cheap",
                "model_route_reason": "routine request",
                "model_metadata_status": "resolved",
            },
        ),
    )

    assert state["metrics"]["model_id"] == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert state["metrics"]["model_tier"] == "cheap"
    assert state["tokens_available"] is False


def test_managed_tool_arguments_keep_their_structure() -> None:
    assert _normalized_tool_input('{"query":"Berlin","max_results":5}') == {
        "query": "Berlin",
        "max_results": 5,
    }
    assert _normalized_tool_input('"plain value"') == {"value": "plain value"}


def test_managed_output_limit_applies_without_extended_thinking(monkeypatch) -> None:
    monkeypatch.setattr(harness, "_supports_additional_params", lambda client: True)

    overrides = harness._invocation_overrides(object(), {"enable_thinking": False})

    assert overrides["model"]["bedrockModelConfig"]["maxTokens"] == 4096
    assert "additionalParams" not in overrides["model"]["bedrockModelConfig"]


def test_managed_model_can_be_selected_per_invocation(monkeypatch) -> None:
    monkeypatch.setattr(harness, "_supports_additional_params", lambda client: True)

    overrides = harness._invocation_overrides(object(), {"model_id": "eu.example.cheap-model"})

    assert overrides["model"]["bedrockModelConfig"]["modelId"] == "eu.example.cheap-model"


def test_managed_cheap_tier_uses_harness_specific_model(monkeypatch) -> None:
    monkeypatch.setattr(harness, "_supports_additional_params", lambda client: True)
    monkeypatch.setattr(harness, "HARNESS_CHEAP_MODEL_ID", "eu.example.harness-cheap")

    overrides = harness._invocation_overrides(
        object(),
        {"model_tier": "cheap", "model_id": "eu.example.shared-cheap"},
    )

    assert overrides["model"]["bedrockModelConfig"]["modelId"] == "eu.example.harness-cheap"


def test_managed_prompt_caching_adds_bedrock_system_checkpoint(monkeypatch) -> None:
    monkeypatch.setattr(harness, "_supports_additional_params", lambda client: True)
    monkeypatch.setattr(harness, "_managed_system_prompt", lambda: "stable managed prompt")

    overrides = harness._invocation_overrides(object(), {"enable_caching": True})

    assert overrides["model"]["bedrockModelConfig"]["additionalParams"]["system"] == [
        {"text": "stable managed prompt"},
        {"cachePoint": {"type": "default"}},
    ]


def test_managed_caching_and_thinking_share_additional_params(monkeypatch) -> None:
    monkeypatch.setattr(harness, "_supports_additional_params", lambda client: True)
    monkeypatch.setattr(harness, "_managed_system_prompt", lambda: "stable managed prompt")

    overrides = harness._invocation_overrides(
        object(), {"enable_caching": True, "enable_thinking": True}
    )
    additional = overrides["model"]["bedrockModelConfig"]["additionalParams"]

    assert additional["system"][-1] == {"cachePoint": {"type": "default"}}
    assert additional["additionalModelRequestFields"]["thinking"] == {
        "type": "enabled",
        "budget_tokens": 1024,
    }


def test_managed_sonnet_46_uses_adaptive_reasoning_effort(monkeypatch) -> None:
    monkeypatch.setattr(harness, "_supports_additional_params", lambda client: True)
    monkeypatch.setattr(harness, "HARNESS_MODEL_ID", "eu.anthropic.claude-sonnet-4-6")

    overrides = harness._invocation_overrides(
        object(),
        {"model_tier": "strong", "reasoning_effort": "low"},
    )

    request_fields = overrides["model"]["bedrockModelConfig"]["additionalParams"][
        "additionalModelRequestFields"
    ]
    assert request_fields == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    }
