"""Route-only adapter over LiteLLM's LLM-backed complexity classifier.

The comparison application decides one model before dispatching a turn to both
AgentCore architectures, and Bedrock — not LiteLLM — performs the invocation.
LiteLLM's ``auto_router/complexity_router`` therefore runs here in classify-only
mode: Ministral 3B classifies the request, LiteLLM maps the resulting tier to one
of the two logical model groups, and this adapter returns that decision. The
selected application model is still invoked by Bedrock, not LiteLLM.

If the classifier call fails, times out, or returns an invalid response,
LiteLLM falls back to its local heuristic scorer and reports that path in the
decision metadata.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
from typing import Any

# The comparison app owns its regional pricing configuration. Avoid delaying
# startup on LiteLLM's optional GitHub cost-map refresh; operators can set this
# to ``false`` before startup if they explicitly want the remote map.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

CHEAP_MODEL_GROUP = "cheap"
STRONG_MODEL_GROUP = "strong"
ROUTER_MODEL_NAME = "agentcore-auto-router"
CLASSIFIER_MODEL_GROUP = "classifier"
CLASSIFIER_CONTEXT_WINDOW_SIZE = 4
CLASSIFIER_CONTEXT_PER_TURN_CHARS = 500

# Only genuinely simple requests are worth the economy model. Every tier that
# implies synthesis, tool planning, or reasoning goes to the strong model.
DEFAULT_TIERS: dict[str, str] = {
    "SIMPLE": CHEAP_MODEL_GROUP,
    "MEDIUM": STRONG_MODEL_GROUP,
    "COMPLEX": STRONG_MODEL_GROUP,
    "REASONING": STRONG_MODEL_GROUP,
}

_VALID_TIER_KEYS = frozenset(DEFAULT_TIERS)
_VALID_MODEL_GROUPS = frozenset({CHEAP_MODEL_GROUP, STRONG_MODEL_GROUP})
_VALID_BOUNDARY_KEYS = frozenset({"simple_medium", "medium_complex", "complex_reasoning"})

_LOAD_LOCK = Lock()


class ComplexityRouterError(RuntimeError):
    """Raised when LiteLLM cannot produce a usable complexity decision."""


@dataclass(frozen=True)
class ComplexityDecision:
    model_group: str
    tier: str
    score: float | None
    signals: tuple[str, ...]
    cause: str


def parse_tiers(tiers_json: str) -> dict[str, str]:
    """Validate an operator-supplied tier policy against the two model groups."""
    if not (tiers_json or "").strip():
        return dict(DEFAULT_TIERS)
    try:
        configured = json.loads(tiers_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ComplexityRouterError("Complexity tier policy is not valid JSON") from exc
    if not isinstance(configured, dict) or not configured:
        raise ComplexityRouterError("Complexity tier policy must be a non-empty object")

    tiers = dict(DEFAULT_TIERS)
    for tier, model_group in configured.items():
        tier_key = str(tier).strip().upper()
        group = str(model_group).strip().lower()
        if tier_key not in _VALID_TIER_KEYS:
            raise ComplexityRouterError(f"Unknown complexity tier {tier!r}")
        if group not in _VALID_MODEL_GROUPS:
            raise ComplexityRouterError(f"Tier {tier_key} maps to unknown model group {group!r}")
        tiers[tier_key] = group
    return tiers


def parse_tier_boundaries(boundaries_json: str) -> dict[str, float]:
    """Validate score-boundary overrides, leaving LiteLLM's defaults in place."""
    if not (boundaries_json or "").strip():
        return {}
    try:
        configured = json.loads(boundaries_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ComplexityRouterError("Complexity tier boundaries are not valid JSON") from exc
    if not isinstance(configured, dict):
        raise ComplexityRouterError("Complexity tier boundaries must be an object")

    boundaries: dict[str, float] = {}
    for name, value in configured.items():
        key = str(name).strip().lower()
        if key not in _VALID_BOUNDARY_KEYS:
            raise ComplexityRouterError(f"Unknown complexity tier boundary {name!r}")
        try:
            boundaries[key] = float(value)
        except (TypeError, ValueError) as exc:
            raise ComplexityRouterError(f"Boundary {key} is not a number") from exc
    return boundaries


def build_model_list(
    classifier_model_id: str,
    classifier_region: str,
    classifier_input_per_mtok: float,
    classifier_output_per_mtok: float,
) -> list[dict[str, Any]]:
    """Describe the Bedrock classifier deployment and its regional rates.

    LiteLLM's ``ComplexityRouter`` requires a ``litellm.Router`` instance. The
    classifier is the only deployment invoked by LiteLLM. The returned ``cheap``
    and ``strong`` names are logical tier values; the two AgentCore architectures
    invoke those models after routing.
    """
    return [
        {
            "model_name": CLASSIFIER_MODEL_GROUP,
            "litellm_params": {
                "model": f"bedrock/{classifier_model_id}",
                "aws_region_name": classifier_region,
            },
            "model_info": {
                "base_model": f"bedrock/{classifier_model_id}",
                "input_cost_per_token": classifier_input_per_mtok / 1_000_000,
                "output_cost_per_token": classifier_output_per_mtok / 1_000_000,
                # Ministral does not use prompt caching for this classifier call.
                # Explicit zeros keep LiteLLM from treating its generated
                # deployment ID as an unknown cache-priced model.
                "cache_creation_input_token_cost": 0.0,
                "cache_read_input_token_cost": 0.0,
            },
        },
    ]


@lru_cache(maxsize=4)
def _load_router(
    tiers_json: str,
    boundaries_json: str,
    cheap_model_id: str,
    strong_model_id: str,
    classifier_model_id: str,
    classifier_region: str,
    classifier_timeout_ms: int,
    classifier_input_per_mtok: float,
    classifier_output_per_mtok: float,
) -> Any:
    """Build one LLM-backed ComplexityRouter and retain it across requests."""
    tiers = parse_tiers(tiers_json)
    boundaries = parse_tier_boundaries(boundaries_json)
    try:
        from litellm import Router
        from litellm.router_strategy.complexity_router.complexity_router import ComplexityRouter
    except (ImportError, ModuleNotFoundError) as exc:
        raise ComplexityRouterError("LiteLLM is not installed; run `uv sync`") from exc

    if not classifier_model_id.strip():
        raise ComplexityRouterError("Classifier model ID must not be empty")
    if not classifier_region.strip():
        raise ComplexityRouterError("Classifier Region must not be empty")
    if classifier_timeout_ms <= 0:
        raise ComplexityRouterError("Classifier timeout must be greater than zero")
    if classifier_input_per_mtok < 0 or classifier_output_per_mtok < 0:
        raise ComplexityRouterError("Classifier token prices must not be negative")

    config: dict[str, Any] = {
        "tiers": tiers,
        "classifier_type": "llm",
        "classifier_llm_config": {
            "model": CLASSIFIER_MODEL_GROUP,
            "timeout_ms": classifier_timeout_ms,
        },
        # Short follow-ups need recent user asks to be classified correctly,
        # but the bounded window keeps classifier latency and spend predictable.
        "classifier_context_window_size": CLASSIFIER_CONTEXT_WINDOW_SIZE,
        "classifier_context_per_turn_chars": CLASSIFIER_CONTEXT_PER_TURN_CHARS,
        "classifier_context_include_assistant_turns": False,
    }
    if boundaries:
        config["tier_boundaries"] = boundaries
    try:
        return ComplexityRouter(
            model_name=ROUTER_MODEL_NAME,
            litellm_router_instance=Router(
                model_list=build_model_list(
                    classifier_model_id,
                    classifier_region,
                    classifier_input_per_mtok,
                    classifier_output_per_mtok,
                ),
                # The classifier already has a strict latency budget and a
                # deterministic local fallback. Retrying would multiply a
                # 750 ms timeout into several seconds of routing latency.
                num_retries=0,
            ),
            complexity_router_config=config,
            # A tier missing from the policy must still resolve to a model, and
            # the safe direction for an unclassified request is the strong one.
            default_model=STRONG_MODEL_GROUP,
        )
    except Exception as exc:
        raise ComplexityRouterError("LiteLLM could not build its complexity router") from exc


async def aroute_model_group(
    prompt: str,
    tiers_json: str,
    boundaries_json: str,
    cheap_model_id: str,
    strong_model_id: str,
    classifier_model_id: str,
    classifier_region: str,
    classifier_timeout_ms: int,
    classifier_input_per_mtok: float,
    classifier_output_per_mtok: float,
    messages: Sequence[Mapping[str, object]] | None = None,
) -> ComplexityDecision:
    """Async convenience boundary for callers that already own an event loop."""
    return await asyncio.to_thread(
        route_model_group,
        prompt,
        tiers_json,
        boundaries_json,
        cheap_model_id,
        strong_model_id,
        classifier_model_id,
        classifier_region,
        classifier_timeout_ms,
        classifier_input_per_mtok,
        classifier_output_per_mtok,
        messages,
    )


def route_model_group(
    prompt: str,
    tiers_json: str,
    boundaries_json: str,
    cheap_model_id: str,
    strong_model_id: str,
    classifier_model_id: str,
    classifier_region: str,
    classifier_timeout_ms: int,
    classifier_input_per_mtok: float,
    classifier_output_per_mtok: float,
    messages: Sequence[Mapping[str, object]] | None = None,
) -> ComplexityDecision:
    """Classify synchronously in the comparison controller's worker thread."""
    # functools.lru_cache may invoke the wrapped function more than once during
    # concurrent first use. Router construction is not free, so serialize misses.
    with _LOAD_LOCK:
        router = _load_router(
            tiers_json,
            boundaries_json,
            cheap_model_id,
            strong_model_id,
            classifier_model_id,
            classifier_region,
            classifier_timeout_ms,
            classifier_input_per_mtok,
            classifier_output_per_mtok,
        )

    try:
        from litellm.router_strategy.complexity_router.complexity_router import (
            ComplexityRouter,
            TierClassification,
            _classification_system_prompt,
            _extract_prior_turns,
        )

        context_messages = list(messages or ())
        prior_turns = _extract_prior_turns(
            context_messages,
            current_ask=prompt,
            window_size=CLASSIFIER_CONTEXT_WINDOW_SIZE,
            per_turn_chars=CLASSIFIER_CONTEXT_PER_TURN_CHARS,
            include_assistant=False,
        )
        classifier_payload = ComplexityRouter._build_classifier_user_payload(
            prompt=prompt,
            prior_turns=prior_turns,
            messages=context_messages,
            has_prior_conversation=bool(prior_turns),
        )

        response = router.litellm_router_instance.completion(
            model=CLASSIFIER_MODEL_GROUP,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": _classification_system_prompt(CLASSIFIER_CONTEXT_WINDOW_SIZE),
                },
                {"role": "user", "content": classifier_payload},
            ],
            response_format=TierClassification,
            timeout=classifier_timeout_ms / 1000,
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("LLM classifier returned empty content")
        tier_name = TierClassification.model_validate_json(content).tier
        score = None
        signals = (f"llm-classifier:{tier_name}",)
        cause = "llm_classifier"
    except Exception:
        # The local score is retained for diagnosis, but model_router treats
        # this path as a safe strong-model fallback rather than trusting it to
        # downgrade a failed LLM classification to the cheap model.
        tier, score, local_signals = router.classify(prompt)
        tier_name = str(getattr(tier, "value", tier))
        signals = tuple(str(signal) for signal in local_signals)
        cause = "heuristic_scorer"

    model_group = router.get_model_for_tier(tier_name)
    if not isinstance(model_group, str) or not model_group.strip():
        raise ComplexityRouterError("LiteLLM did not return a model group")
    return ComplexityDecision(
        model_group=model_group.strip(),
        tier=tier_name,
        score=float(score) if score is not None else None,
        signals=signals,
        cause=cause,
    )


def router_version() -> str:
    """Return a stable identifier for the deployed routing implementation."""
    try:
        from importlib.metadata import version

        return f"litellm-{version('litellm')}"
    except Exception:
        return "litellm-unknown"
