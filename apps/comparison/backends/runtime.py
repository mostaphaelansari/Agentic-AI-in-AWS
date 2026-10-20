"""Custom AgentCore Runtime invocation."""

from __future__ import annotations

import json
import time

import config as app_config

from backends.common import get_client, parse_sse_events, with_current_date

STRONG_MODEL_ID = getattr(
    app_config,
    "BEDROCK_STRONG_MODEL_ID",
    getattr(app_config, "HARNESS_MODEL_ID", "eu.anthropic.claude-sonnet-4-6"),
)


def _with_legacy_model_metadata(event: dict, options: dict) -> dict:
    """Label metrics from a pre-router Runtime image without guessing cheap.

    Older images ignore ``model_tier`` and always use their configured
    ``BEDROCK_MODEL_ID`` (the strong model). Their metrics omit model metadata,
    so the adapter marks that compatibility behavior explicitly.
    """
    if event.get("type") != "metrics" or event.get("model_id"):
        return event
    return {
        **event,
        "model_id": STRONG_MODEL_ID,
        "model_tier": "strong",
        "model_route_reason": "legacy Runtime image ignored model routing; redeploy required",
        "model_metadata_status": "legacy-runtime",
        "requested_model_tier": options.get("model_tier", "strong"),
    }


def invoke_runtime(
    config,
    session_id,
    user_msg,
    event_queue,
    options=None,
    *,
    controller=None,
    arch_name=None,
):
    client = get_client(config["region"])
    started_at = time.time()
    options = options or {}
    payload = {
        "prompt": with_current_date(user_msg),
        "enable_caching": bool(options.get("enable_caching")),
        "enable_thinking": bool(options.get("enable_thinking") or options.get("reasoning_effort")),
        "model_tier": options.get("model_tier", "strong"),
        "model_route_reason": options.get("model_route_reason", "default model"),
        "model_router_mode": options.get("model_router_mode", "unknown"),
        "model_router_name": options.get("model_router_name", "unknown"),
        "model_router_version": options.get("model_router_version", "unknown"),
        "model_router_candidate": options.get("model_router_candidate", ""),
        "model_router_outcome": options.get("model_router_outcome", "unknown"),
        "model_router_fallback": bool(options.get("model_router_fallback", False)),
        "model_router_latency_ms": options.get("model_router_latency_ms", 0.0),
    }
    if options.get("reasoning_effort"):
        payload["reasoning_effort"] = options["reasoning_effort"]
    body = None
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=config["arn"],
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode("utf-8"),
        )
        body = response.get("response")
        if controller is not None and body is not None:
            controller.register_stream(arch_name, body)
        cancel = controller.cancelled if controller is not None else None
        if "event-stream" in response.get("contentType", ""):
            for event in parse_sse_events(body, cancel=cancel):
                event_queue.put_nowait(("event", _with_legacy_model_metadata(event, options)))
        else:
            raw = body.read() if body else b""
            result = json.loads(raw) if raw else {}
            if isinstance(result, list):
                for event in result:
                    if isinstance(event, dict) and "type" in event:
                        event_queue.put_nowait(
                            ("event", _with_legacy_model_metadata(event, options))
                        )
            elif isinstance(result, dict):
                event_queue.put_nowait(
                    (
                        "event",
                        {"type": "text", "content": result.get("result", str(result))},
                    )
                )
    except Exception:
        if controller is None or not controller.cancelled.is_set():
            raise
    finally:
        if body is not None:
            body.close()
        event_queue.put_nowait(("elapsed", round(time.time() - started_at, 2)))
        event_queue.put_nowait(("done", None))
