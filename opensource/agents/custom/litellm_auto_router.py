"""LiteLLM Auto Router model adapter for the open-source Strands agent.

LiteLLM owns both the complexity decision and the provider invocation.  This
adapter only translates LiteLLM's routed streaming response through Strands'
existing ``LiteLLMModel`` event formatter.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from typing import Any, Callable

from strands.models.litellm import LiteLLMModel
from strands.types.streaming import StreamEvent

logger = logging.getLogger(__name__)

OLLAMA_MODEL_GROUP = "opensource-ollama"
KIMI_MODEL_GROUP = "opensource-kimi"
DEFAULT_ROUTER_MODEL_NAME = "opensource-auto-router"


def build_router_model_list(
    *,
    router_model_name: str,
    ollama_host: str,
    ollama_model_id: str,
    kimi_base_url: str,
    kimi_api_key: str,
    kimi_model_id: str,
    kimi_temperature: float,
    kimi_timeout: float,
    kimi_max_tokens: int | None,
    session_affinity_ttl_seconds: int,
) -> list[dict[str, Any]]:
    """Return the LiteLLM SDK configuration for Ollama/Kimi auto-routing."""
    kimi_params: dict[str, Any] = {
        "model": f"openai/{kimi_model_id}",
        "api_base": kimi_base_url,
        "api_key": kimi_api_key,
        "temperature": kimi_temperature,
        "timeout": kimi_timeout,
    }
    if kimi_max_tokens is not None:
        kimi_params["max_tokens"] = kimi_max_tokens

    return [
        {
            "model_name": OLLAMA_MODEL_GROUP,
            "litellm_params": {
                "model": f"ollama_chat/{ollama_model_id}",
                "api_base": ollama_host,
            },
            "model_info": {"supports_function_calling": True},
        },
        {
            "model_name": KIMI_MODEL_GROUP,
            "litellm_params": kimi_params,
            "model_info": {"supports_function_calling": True},
        },
        {
            "model_name": router_model_name,
            "litellm_params": {
                "model": "auto_router/complexity_router",
                "drop_params": True,
                "complexity_router_config": {
                    # qwen3:1.7b is intentionally limited to genuinely simple
                    # requests. Kimi handles every tier that requires material
                    # synthesis, tool planning, or reasoning.
                    "tiers": {
                        "SIMPLE": OLLAMA_MODEL_GROUP,
                        "MEDIUM": KIMI_MODEL_GROUP,
                        "COMPLEX": KIMI_MODEL_GROUP,
                        "REASONING": KIMI_MODEL_GROUP,
                    },
                    # LiteLLM intentionally scores the agent system prompt as
                    # context. This agent's tool-heavy prompt adds a small,
                    # fixed technical baseline (about 0.165) even for "hello".
                    # Lift only the SIMPLE/MEDIUM boundary enough to keep those
                    # genuinely short requests on Ollama; all stronger tiers
                    # retain LiteLLM's defaults.
                    "tier_boundaries": {
                        "simple_medium": 0.20,
                        "medium_complex": 0.35,
                        "complex_reasoning": 0.60,
                    },
                    # The service supplies a unique id for each user turn. This
                    # keeps all model/tool cycles on one provider without pinning
                    # the user's next turn to an earlier decision.
                    "session_affinity": True,
                    "session_affinity_ttl_seconds": session_affinity_ttl_seconds,
                },
                "complexity_router_default_model": KIMI_MODEL_GROUP,
            },
        },
    ]


def create_router(
    *,
    router_model_name: str = DEFAULT_ROUTER_MODEL_NAME,
    ollama_host: str,
    ollama_model_id: str,
    kimi_base_url: str,
    kimi_api_key: str,
    kimi_model_id: str,
    kimi_temperature: float,
    kimi_timeout: float,
    kimi_max_tokens: int | None,
    session_affinity_ttl_seconds: int = 600,
    router_cls: type | None = None,
):
    """Build the LiteLLM Router with bidirectional provider failover."""
    if not kimi_api_key:
        raise RuntimeError("LiteLLM auto-routing requires KIMI_API_KEY or MOONSHOT_API_KEY")
    if router_cls is None:
        from litellm import Router

        router_cls = Router

    model_list = build_router_model_list(
        router_model_name=router_model_name,
        ollama_host=ollama_host,
        ollama_model_id=ollama_model_id,
        kimi_base_url=kimi_base_url,
        kimi_api_key=kimi_api_key,
        kimi_model_id=kimi_model_id,
        kimi_temperature=kimi_temperature,
        kimi_timeout=kimi_timeout,
        kimi_max_tokens=kimi_max_tokens,
        session_affinity_ttl_seconds=session_affinity_ttl_seconds,
    )
    return router_cls(
        model_list=model_list,
        num_retries=0,
        max_fallbacks=1,
        fallbacks=[
            {OLLAMA_MODEL_GROUP: [KIMI_MODEL_GROUP]},
            {KIMI_MODEL_GROUP: [OLLAMA_MODEL_GROUP]},
        ],
        allowed_fails=2,
        cooldown_time=30,
    )


class LiteLLMAutoRouterModel(LiteLLMModel):
    """Strands model that invokes a configured LiteLLM ``Router`` instance."""

    def __init__(
        self,
        *,
        router: Any,
        router_model_name: str,
        routing_session_id: str,
        on_model_call: Callable[[], None] | None = None,
        on_route: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(model_id=router_model_name, params={"stream": True})
        self.router = router
        self.router_model_name = router_model_name
        self.routing_session_id = routing_session_id
        self.on_model_call = on_model_call
        self.on_route = on_route
        self.selected_model_id: str | None = None
        self._route_recorded = False

    def _record_route(
        self,
        *,
        started_at_ns: int,
        started_at: float,
        status: str,
        metadata: dict[str, Any],
    ) -> None:
        """Report the first per-turn routing/dispatch operation to telemetry."""
        if self.on_route is None:
            return
        try:
            self.on_route(
                {
                    "router_model_name": self.router_model_name,
                    "routing_session_id": self.routing_session_id,
                    "routing_decision": metadata.get("routing_decision"),
                    "status": status,
                    "start_time_ns": started_at_ns,
                    "end_time_ns": time.time_ns(),
                    "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                }
            )
        except Exception:
            # Observability must never prevent the provider call from running.
            logger.debug("failed to record LiteLLM router telemetry", exc_info=True)

    async def _router_completion(self, request: dict[str, Any]):
        routed_request = dict(request)
        metadata = dict(routed_request.get("metadata") or {})
        metadata["session_id"] = self.routing_session_id
        routed_request["metadata"] = metadata
        if self.on_model_call is not None:
            self.on_model_call()
        record_route = not self._route_recorded
        if record_route:
            # Later model calls in the same tool loop reuse LiteLLM session
            # affinity. Show one routing node per user turn, not one per cycle.
            self._route_recorded = True
        started_at_ns = time.time_ns()
        started_at = time.perf_counter()
        try:
            response = await self.router.acompletion(**routed_request)
        except Exception:
            if record_route:
                self._record_route(
                    started_at_ns=started_at_ns,
                    started_at=started_at,
                    status="ERROR",
                    metadata=metadata,
                )
            raise
        if record_route:
            self._record_route(
                started_at_ns=started_at_ns,
                started_at=started_at,
                status="OK",
                metadata=metadata,
            )
        return response

    def _capture_selected_model(self, event: Any) -> None:
        model = getattr(event, "model", None)
        if model:
            self.selected_model_id = str(model)

    async def _handle_streaming_response(
        self, litellm_request: dict[str, Any]
    ) -> AsyncGenerator[StreamEvent, None]:
        """Route and translate a streaming completion using Strands helpers."""
        response = await self._router_completion(litellm_request)
        logger.debug("LiteLLM Auto Router returned a streaming response")
        yield self.format_chunk({"chunk_type": "message_start"})

        tool_calls: dict[int, list[Any]] = {}
        data_type: str | None = None
        finish_reason: str | None = None

        async for event in response:
            self._capture_selected_model(event)
            if not getattr(event, "choices", None):
                continue
            choice = event.choices[0]
            async for updated_data_type, chunk in self._process_choice_content(
                choice, data_type, tool_calls, is_streaming=True
            ):
                data_type = updated_data_type
                yield chunk

            if choice.finish_reason:
                finish_reason = choice.finish_reason
                if data_type:
                    yield self.format_chunk({"chunk_type": "content_stop", "data_type": data_type})
                break

        async for chunk in self._process_tool_calls(tool_calls):
            yield chunk

        yield self.format_chunk({"chunk_type": "message_stop", "data": finish_reason})

        async for event in response:
            self._capture_selected_model(event)
            if usage := getattr(event, "usage", None):
                yield self.format_chunk({"chunk_type": "metadata", "data": usage})
