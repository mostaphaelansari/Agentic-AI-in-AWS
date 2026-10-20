"""Regression tests for custom-agent loop safeguards."""

from __future__ import annotations

import importlib.util
from pathlib import Path

AGENT_PATH = Path(__file__).parents[2] / "agents" / "custom" / "agent.py"
SPEC = importlib.util.spec_from_file_location("custom_agent_for_limit_test", AGENT_PATH)
custom_agent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(custom_agent)


def test_model_output_limit_applies_without_extended_thinking() -> None:
    kwargs = custom_agent._bedrock_model_kwargs(
        object,
        enable_caching=False,
        enable_thinking=False,
    )

    assert kwargs["max_tokens"] == 4096


def test_selected_model_is_forwarded_to_strands() -> None:
    kwargs = custom_agent._bedrock_model_kwargs(
        object,
        model_id="eu.example.cheap-model",
        enable_caching=False,
        enable_thinking=False,
    )

    assert kwargs["model_id"] == "eu.example.cheap-model"


def test_sonnet_46_uses_adaptive_reasoning_effort() -> None:
    kwargs = custom_agent._bedrock_model_kwargs(
        object,
        model_id="eu.anthropic.claude-sonnet-4-6",
        enable_caching=False,
        reasoning_effort="medium",
    )

    assert kwargs["additional_request_fields"] == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }


def test_max_reasoning_effort_requires_opus_46() -> None:
    try:
        custom_agent._bedrock_model_kwargs(
            object,
            model_id="eu.anthropic.claude-sonnet-4-6",
            enable_caching=False,
            reasoning_effort="max",
        )
    except ValueError as exc:
        assert str(exc) == "reasoning_effort=max requires Claude Opus 4.6"
    else:
        raise AssertionError("Sonnet 4.6 must reject max effort")


def test_max_tokens_exception_has_a_user_facing_message() -> None:
    max_tokens_error = type("MaxTokensReachedException", (Exception,), {})()

    message = custom_agent._public_agent_error(max_tokens_error)

    assert "4,096-token output limit" in message
    assert "strandsagents.com" not in message


def test_skill_document_exposes_semantic_description() -> None:
    document = """---
name: forecast-lookup
description: Retrieve current or forecast weather for a location
keywords: weather, forecast
---
Follow the weather workflow.
"""

    skill = custom_agent._parse_skill_document(
        "harness-skills/forecast-lookup/SKILL.md",
        document,
    )

    assert skill == {
        "name": "forecast-lookup",
        "description": "Retrieve current or forecast weather for a location",
        "body": "Follow the weather workflow.",
    }


def test_custom_runtime_builds_native_semantic_skills(monkeypatch) -> None:
    monkeypatch.setattr(
        custom_agent,
        "_scan_s3_skills",
        lambda: [
            {
                "name": "forecast-lookup",
                "description": "Retrieve weather for a location",
                "body": "Call the weather tools.",
            }
        ],
    )

    plugin = custom_agent._build_skills_plugin()

    assert plugin is not None
    available = plugin.get_available_skills()
    assert [(skill.name, skill.description) for skill in available] == [
        ("forecast-lookup", "Retrieve weather for a location")
    ]


def test_configured_semantic_skill_catalog_must_not_be_empty(monkeypatch) -> None:
    monkeypatch.setattr(custom_agent, "SKILLS_S3_BUCKET", "configured-skills")
    monkeypatch.setattr(custom_agent, "_scan_s3_skills", lambda: [])

    try:
        custom_agent._build_skills_plugin()
    except RuntimeError as exc:
        assert "contains no valid skills" in str(exc)
    else:
        raise AssertionError("configured empty skill catalog should fail the invocation")


def test_legacy_prompt_skills_are_removed_without_losing_tool_budget() -> None:
    prompt = """Base instructions.

## Open-source skills
### Skill: forecast-lookup
Legacy instructions.

## Tool-use budget
Stop after enough evidence.

## How to answer
Lead with the answer.
"""

    cleaned = custom_agent._LEGACY_EMBEDDED_SKILLS.sub("", prompt)

    assert "Legacy instructions" not in cleaned
    assert "## Tool-use budget" in cleaned
