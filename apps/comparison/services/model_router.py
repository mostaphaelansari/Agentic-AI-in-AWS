"""Request-level routing between economical and strong Bedrock models.

Auto mode delegates every model-selection decision to LiteLLM's Auto Router
complexity scorer, used in route-only mode. Both comparison architectures — the
managed Harness and the custom Runtime — consume the same allowlisted decision
so their benchmark remains like-for-like. Explicit user-selected modes remain
available.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace

import config as app_config

from services import litellm_complexity_router

BEDROCK_STRONG_MODEL_ID = getattr(
    app_config,
    "BEDROCK_STRONG_MODEL_ID",
    getattr(app_config, "HARNESS_MODEL_ID", "eu.anthropic.claude-sonnet-4-6"),
)
BEDROCK_CHEAP_MODEL_ID = getattr(
    app_config,
    "BEDROCK_CHEAP_MODEL_ID",
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
)
MODEL_ROUTING_MODE = getattr(app_config, "MODEL_ROUTING_MODE", "auto")
MODEL_ROUTER_NAME = "litellm-complexity-router"
MODEL_ROUTER_CLASSIFIER_MODEL_ID = getattr(
    app_config,
    "MODEL_ROUTER_CLASSIFIER_MODEL_ID",
    "mistral.ministral-3-3b-instruct",
)
MODEL_ROUTER_CLASSIFIER_REGION = getattr(
    app_config,
    "MODEL_ROUTER_CLASSIFIER_REGION",
    "eu-west-1",
)
MODEL_ROUTER_CLASSIFIER_TIMEOUT_MS = getattr(
    app_config,
    "MODEL_ROUTER_CLASSIFIER_TIMEOUT_MS",
    750,
)
MODEL_ROUTER_CLASSIFIER_INPUT_PER_MTOK = getattr(
    app_config,
    "MODEL_ROUTER_CLASSIFIER_INPUT_PER_MTOK",
    0.12,
)
MODEL_ROUTER_CLASSIFIER_OUTPUT_PER_MTOK = getattr(
    app_config,
    "MODEL_ROUTER_CLASSIFIER_OUTPUT_PER_MTOK",
    0.12,
)
MODEL_ROUTER_WARMUP_ENABLED = getattr(app_config, "MODEL_ROUTER_WARMUP_ENABLED", True)
COMPLEXITY_TIERS = getattr(app_config, "COMPLEXITY_TIERS", "")
COMPLEXITY_TIER_BOUNDARIES = getattr(app_config, "COMPLEXITY_TIER_BOUNDARIES", "")
MODEL_ROUTER_VERSION = getattr(app_config, "MODEL_ROUTER_VERSION", "")

_VALID_MODES = {"auto", "cheap", "strong"}
_VALID_REASONING_EFFORTS = {"low", "medium", "high", "max"}
_WARMUP_LOCK = threading.Lock()
_WARMUP_THREAD: threading.Thread | None = None
_WARMUP_PROMPT = "Hello"
logger = logging.getLogger("unified-ui.model-router")


@dataclass(frozen=True)
class ModelRoute:
    tier: str
    model_id: str
    reason: str
    mode: str = "auto"
    router_name: str = MODEL_ROUTER_NAME
    router_version: str = "unknown"
    candidate: str = ""
    outcome: str = "selected"
    fallback: bool = False
    classifier_tier: str = ""
    latency_ms: float = 0.0


def _option_enabled(value: object) -> bool:
    """Interpret request flags without treating the string ``false`` as enabled."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _router_version() -> str:
    """Prefer an operator-pinned version so decisions stay attributable."""
    return MODEL_ROUTER_VERSION or litellm_complexity_router.router_version()


def _model_tier_map() -> dict[str, str]:
    """Build the exact-name allowlist for routed model-group names."""
    return {
        litellm_complexity_router.CHEAP_MODEL_GROUP: "cheap",
        litellm_complexity_router.STRONG_MODEL_GROUP: "strong",
        BEDROCK_CHEAP_MODEL_ID.casefold(): "cheap",
        BEDROCK_STRONG_MODEL_ID.casefold(): "strong",
    }


def _run_router_warmup() -> None:
    """Prime LiteLLM, AWS connection state, and the cached classifier Router."""
    started_at = time.perf_counter()
    try:
        decision = litellm_complexity_router.route_model_group(
            _WARMUP_PROMPT,
            COMPLEXITY_TIERS,
            COMPLEXITY_TIER_BOUNDARIES,
            BEDROCK_CHEAP_MODEL_ID,
            BEDROCK_STRONG_MODEL_ID,
            MODEL_ROUTER_CLASSIFIER_MODEL_ID,
            MODEL_ROUTER_CLASSIFIER_REGION,
            MODEL_ROUTER_CLASSIFIER_TIMEOUT_MS,
            MODEL_ROUTER_CLASSIFIER_INPUT_PER_MTOK,
            MODEL_ROUTER_CLASSIFIER_OUTPUT_PER_MTOK,
        )
        logger.info(
            "model router warm-up completed latency_ms=%.3f classifier_path=%s",
            (time.perf_counter() - started_at) * 1000,
            decision.cause,
        )
    except Exception:
        # Warm-up is an optimization only. The first real request retains the
        # normal strong-model fallback if the classifier is unavailable.
        logger.warning("model router warm-up failed", exc_info=True)


def start_router_warmup() -> bool:
    """Start one non-blocking warm-up per application process."""
    global _WARMUP_THREAD
    if not MODEL_ROUTER_WARMUP_ENABLED or MODEL_ROUTING_MODE != "auto":
        return False
    with _WARMUP_LOCK:
        if _WARMUP_THREAD is not None:
            return False
        _WARMUP_THREAD = threading.Thread(
            target=_run_router_warmup,
            name="model-router-warmup",
            daemon=True,
        )
        _WARMUP_THREAD.start()
    return True


def _auto_route(
    prompt: str,
    messages: list[dict[str, str]] | None = None,
) -> ModelRoute:
    """Resolve an allowlisted LiteLLM decision, failing safely to strong."""
    router_version = _router_version()
    try:
        route_args = (
            prompt,
            COMPLEXITY_TIERS,
            COMPLEXITY_TIER_BOUNDARIES,
            BEDROCK_CHEAP_MODEL_ID,
            BEDROCK_STRONG_MODEL_ID,
            MODEL_ROUTER_CLASSIFIER_MODEL_ID,
            MODEL_ROUTER_CLASSIFIER_REGION,
            MODEL_ROUTER_CLASSIFIER_TIMEOUT_MS,
            MODEL_ROUTER_CLASSIFIER_INPUT_PER_MTOK,
            MODEL_ROUTER_CLASSIFIER_OUTPUT_PER_MTOK,
        )
        if messages:
            decision = litellm_complexity_router.route_model_group(
                *route_args,
                messages=messages,
            )
        else:
            decision = litellm_complexity_router.route_model_group(*route_args)
    except litellm_complexity_router.ComplexityRouterError:
        return ModelRoute(
            "strong",
            BEDROCK_STRONG_MODEL_ID,
            "LiteLLM Auto Router unavailable; safe fallback",
            router_version=router_version,
            outcome="fallback_unavailable",
            fallback=True,
        )

    tier = _model_tier_map().get(decision.model_group.casefold())
    if tier in {"cheap", "strong"}:
        used_fallback = decision.cause != "llm_classifier"
        score = f"{decision.score:.3f}" if decision.score is not None else "n/a"
        if used_fallback:
            return ModelRoute(
                "strong",
                BEDROCK_STRONG_MODEL_ID,
                (
                    "Ministral classifier failed; LiteLLM heuristic returned "
                    f"{decision.tier} ({score}), safe fallback selected strong"
                ),
                router_version=router_version,
                candidate=decision.model_group,
                outcome="fallback_heuristic_classifier",
                fallback=True,
                classifier_tier=decision.tier,
            )
        return ModelRoute(
            tier,
            BEDROCK_CHEAP_MODEL_ID if tier == "cheap" else BEDROCK_STRONG_MODEL_ID,
            (
                f"LiteLLM Ministral classifier ({MODEL_ROUTER_CLASSIFIER_MODEL_ID}) "
                f"scored {decision.tier} "
                f"({score}) and selected {decision.model_group}"
            ),
            router_version=router_version,
            candidate=decision.model_group,
            classifier_tier=decision.tier,
        )
    return ModelRoute(
        "strong",
        BEDROCK_STRONG_MODEL_ID,
        "LiteLLM Auto Router returned an unallowlisted model group; safe fallback",
        router_version=router_version,
        candidate=decision.model_group,
        outcome="fallback_unallowlisted",
        fallback=True,
        classifier_tier=decision.tier,
    )


def route_model(
    prompt: str,
    options: dict | None = None,
    *,
    messages: list[dict[str, str]] | None = None,
) -> ModelRoute:
    """Choose one model for the complete agent turn, including its tool loop."""
    started_at = time.perf_counter()
    options = options or {}
    requested = str(options.get("model_routing") or MODEL_ROUTING_MODE).strip().lower()
    if requested not in _VALID_MODES:
        requested = MODEL_ROUTING_MODE if MODEL_ROUTING_MODE in _VALID_MODES else "auto"

    reasoning_effort = str(options.get("reasoning_effort") or "").strip().lower()
    if reasoning_effort and reasoning_effort not in _VALID_REASONING_EFFORTS:
        raise ValueError("reasoning_effort must be one of: low, medium, high, max")
    if reasoning_effort == "max" and "claude-opus-4-6" not in BEDROCK_STRONG_MODEL_ID.casefold():
        raise ValueError("reasoning_effort=max requires Claude Opus 4.6")

    reasoning_enabled = bool(reasoning_effort) or _option_enabled(options.get("enable_thinking"))
    has_attachment = bool(options.get("uploaded_file"))
    if reasoning_enabled or has_attachment:
        requirements = []
        if reasoning_enabled:
            requirements.append(
                f"adaptive reasoning ({reasoning_effort} effort)"
                if reasoning_effort
                else "reasoning"
            )
        if has_attachment:
            requirements.append("file analysis")
        if reasoning_enabled and has_attachment:
            outcome = "override_reasoning_attachment"
        elif reasoning_enabled:
            outcome = "override_reasoning"
        else:
            outcome = "override_attachment"
        route = ModelRoute(
            "strong",
            BEDROCK_STRONG_MODEL_ID,
            f"strong model required for {' and '.join(requirements)}",
            mode=requested,
            router_name="strong-model-policy",
            router_version="policy-v1",
            candidate="strong",
            outcome=outcome,
        )
    elif requested == "cheap":
        route = ModelRoute(
            "cheap",
            BEDROCK_CHEAP_MODEL_ID,
            "economy mode selected",
            mode="cheap",
            router_name="manual",
            router_version="policy-v1",
            candidate="cheap",
            outcome="override",
        )
    elif requested == "strong":
        route = ModelRoute(
            "strong",
            BEDROCK_STRONG_MODEL_ID,
            "quality mode selected",
            mode="strong",
            router_name="manual",
            router_version="policy-v1",
            candidate="strong",
            outcome="override",
        )
    else:
        route = _auto_route(prompt, messages)
    return replace(
        route,
        latency_ms=round((time.perf_counter() - started_at) * 1000, 3),
    )


def routed_options(prompt: str, options: dict | None = None) -> dict:
    """Return a copy of invocation options enriched with the routing decision."""
    enriched = dict(options or {})
    router_messages = enriched.pop("_model_router_messages", None)
    route = route_model(prompt, enriched, messages=router_messages)
    if route.outcome.startswith("override_"):
        classifier_path = "policy_override"
        classifier_model = ""
    elif route.mode != "auto":
        classifier_path = "manual"
        classifier_model = ""
    elif route.outcome == "selected":
        classifier_path = "llm"
        classifier_model = MODEL_ROUTER_CLASSIFIER_MODEL_ID
    elif route.outcome == "fallback_heuristic_classifier":
        classifier_path = "heuristic_fallback"
        classifier_model = MODEL_ROUTER_CLASSIFIER_MODEL_ID
    else:
        classifier_path = "router_fallback"
        classifier_model = MODEL_ROUTER_CLASSIFIER_MODEL_ID
    enriched.update(
        {
            "model_tier": route.tier,
            "model_id": route.model_id,
            "model_route_reason": route.reason,
            "model_router_mode": route.mode,
            "model_router_name": route.router_name,
            "model_router_version": route.router_version,
            "model_router_candidate": route.candidate,
            "model_router_outcome": route.outcome,
            "model_router_fallback": route.fallback,
            "model_router_latency_ms": route.latency_ms,
            "model_router_classifier_model": classifier_model,
            "model_router_classifier_path": classifier_path,
            "model_router_classifier_tier": route.classifier_tier,
        }
    )
    return enriched
