"""Managed AgentCore Harness invocation and event normalization."""

from __future__ import annotations

import json
import time
from functools import lru_cache, partial
from pathlib import Path

import config as app_config
from botocore.awsrequest import AWSRequest
from config import (
    ACTOR_ID,
    HARNESS_MAX_TOKENS,
    HARNESS_MODEL_ID,
    HARNESS_THINKING_BUDGET_TOKENS,
)
from formatting.browser import format_browser_result, is_browser_tool

from backends.common import get_client, with_current_date

HARNESS_CHEAP_MODEL_ID = getattr(
    app_config,
    "HARNESS_CHEAP_MODEL_ID",
    getattr(
        app_config,
        "BEDROCK_CHEAP_MODEL_ID",
        "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    ),
)

_MANAGED_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parents[3] / "agents" / "managed" / "system-prompt.md"
)


@lru_cache(maxsize=1)
def _managed_system_prompt() -> str:
    return _MANAGED_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()


def _thinking_params(model_id: str, reasoning_effort: str | None = None):
    effort = str(reasoning_effort or "").strip().lower()
    if effort:
        if effort not in {"low", "medium", "high", "max"}:
            raise ValueError("reasoning_effort must be one of: low, medium, high, max")
        normalized_model_id = model_id.casefold()
        if not any(
            family in normalized_model_id for family in ("claude-sonnet-4-6", "claude-opus-4-6")
        ):
            raise ValueError("adaptive reasoning requires Claude Sonnet 4.6 or Opus 4.6")
        if effort == "max" and "claude-opus-4-6" not in normalized_model_id:
            raise ValueError("reasoning_effort=max requires Claude Opus 4.6")
        request_fields = {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
    else:
        _validate_thinking_config()
        request_fields = {
            "thinking": {
                "type": "enabled",
                "budget_tokens": HARNESS_THINKING_BUDGET_TOKENS,
            }
        }
    return {
        "additionalModelRequestFields": request_fields,
    }


def _caching_params():
    """Add a Bedrock cache checkpoint after the stable Managed prompt prefix.

    InvokeHarness does not currently accept ``cachePoint`` in ``systemPrompt``.
    Its Bedrock model ``additionalParams`` are passed to ConverseStream, which
    does accept structured system blocks. Supplying the local deployed prompt
    here lets us append the provider-native checkpoint for comparison runs.
    """
    return {
        "system": [
            {"text": _managed_system_prompt()},
            {"cachePoint": {"type": "default"}},
        ]
    }


def _validate_thinking_config():
    if HARNESS_THINKING_BUDGET_TOKENS < 1024:
        raise ValueError("HARNESS_THINKING_BUDGET_TOKENS must be at least 1024")
    if HARNESS_MAX_TOKENS <= HARNESS_THINKING_BUDGET_TOKENS:
        raise ValueError("HARNESS_MAX_TOKENS must be greater than HARNESS_THINKING_BUDGET_TOKENS")


def _supports_additional_params(client):
    operation = client.meta.service_model.operation_model("InvokeHarness")
    model_shape = operation.input_shape.members["model"]
    bedrock_shape = model_shape.members["bedrockModelConfig"]
    return "additionalParams" in bedrock_shape.members


def _selected_model_id(options: dict) -> str:
    tier = options.get("model_tier")
    if tier == "cheap":
        return HARNESS_CHEAP_MODEL_ID
    if tier == "strong":
        return HARNESS_MODEL_ID
    return options.get("model_id") or HARNESS_MODEL_ID


def _invocation_overrides(client, options):
    options = options or {}
    model_config = {
        "modelId": _selected_model_id(options),
        "maxTokens": HARNESS_MAX_TOKENS,
    }
    additional_params = {}
    if bool(options.get("enable_caching")):
        if not _supports_additional_params(client):
            raise RuntimeError(
                "Managed prompt caching requires an AWS SDK with "
                "InvokeHarness model.additionalParams support"
            )
        additional_params.update(_caching_params())
    if bool(options.get("enable_thinking") or options.get("reasoning_effort")):
        if _supports_additional_params(client):
            additional_params.update(
                _thinking_params(model_config["modelId"], options.get("reasoning_effort"))
            )
    if additional_params:
        model_config["additionalParams"] = additional_params
    return {"model": {"bedrockModelConfig": model_config}}


def _inject_thinking_body(request: AWSRequest, *, options: dict | None = None, **_):
    raw_body = request.body or b"{}"
    body = json.loads(raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body)
    model_config = body.setdefault("model", {}).setdefault("bedrockModelConfig", {})
    model_config.setdefault("modelId", HARNESS_MODEL_ID)
    model_config.setdefault("maxTokens", HARNESS_MAX_TOKENS)
    additional_params = model_config.setdefault("additionalParams", {})
    additional_params.update(
        _thinking_params(
            model_config["modelId"],
            (options or {}).get("reasoning_effort"),
        )
    )
    request.data = json.dumps(body).encode("utf-8")
    request.headers["Content-Length"] = str(len(request.data))


def _register_thinking_injector(client, options):
    if not bool((options or {}).get("enable_thinking") or (options or {}).get("reasoning_effort")):
        return False
    if _supports_additional_params(client):
        return False
    handler = partial(_inject_thinking_body, options=options or {})
    client.meta.events.register(
        "before-sign.bedrock-agentcore.InvokeHarness",
        handler,
    )
    return handler


def _unregister_thinking_injector(client, handler):
    client.meta.events.unregister(
        "before-sign.bedrock-agentcore.InvokeHarness",
        handler,
    )


def _parse_nested_json(raw):
    value = raw
    for _ in range(3):
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return text
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def _reasoning_text(reasoning_content):
    if not isinstance(reasoning_content, dict):
        return ""
    if reasoning_content.get("text"):
        return reasoning_content["text"]
    if "redactedContent" in reasoning_content:
        return "[redacted reasoning]\n"
    return ""


def _normalized_tool_input(raw_input):
    parsed = _parse_nested_json(raw_input)
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed or ""}


def _summarize_tool_result(tool_name, raw_result):
    if is_browser_tool(tool_name):
        return format_browser_result(raw_result)
    parsed = _parse_nested_json(raw_result)
    if isinstance(parsed, dict):
        body = _parse_nested_json(parsed.get("body")) if "body" in parsed else parsed
        return json.dumps(body, ensure_ascii=False)[:300]
    return raw_result[:300] if raw_result else "completed"


def _stream_events(stream, controller=None):
    try:
        for stream_event in stream:
            if controller is not None and controller.cancelled.is_set():
                return
            yield stream_event
    except Exception:
        if controller is None or not controller.cancelled.is_set():
            raise
    finally:
        stream.close()


def invoke_harness(
    config,
    session_id,
    user_msg,
    event_queue,
    options=None,
    *,
    controller=None,
    arch_name=None,
):
    options = options or {}
    client = get_client(config["region"])
    started_at = time.time()
    token_totals = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheWriteInputTokens": 0,
    }
    saw_usage = False
    injected_thinking = _register_thinking_injector(client, options)
    try:
        response = client.invoke_harness(
            harnessArn=config["arn"],
            runtimeSessionId=session_id,
            actorId=ACTOR_ID,
            messages=[
                {
                    "role": "user",
                    "content": [{"text": with_current_date(user_msg)}],
                }
            ],
            **_invocation_overrides(client, options),
        )
    finally:
        if injected_thinking:
            _unregister_thinking_injector(client, injected_thinking)

    active_use_id = None
    active_name = None
    input_parts = []
    active_result_name = None
    active_result_id = None
    result_parts = []
    tool_names = {}
    tool_calls = 0
    called_tool_names = []

    stream = response["stream"]
    if controller is not None:
        controller.register_stream(arch_name, stream)
    for stream_event in _stream_events(stream, controller):
        for key, value in stream_event.items():
            if key == "contentBlockDelta":
                delta = value.get("delta", {})
                if "reasoningContent" in delta:
                    reasoning = _reasoning_text(delta["reasoningContent"])
                    if reasoning:
                        event_queue.put_nowait(
                            ("event", {"type": "reasoning", "content": reasoning})
                        )
                elif "text" in delta:
                    event_queue.put_nowait(
                        (
                            "event",
                            {
                                "type": "text",
                                "source": "managed",
                                "content": delta["text"],
                            },
                        )
                    )
                elif "toolResult" in delta:
                    for item in delta.get("toolResult") or []:
                        if isinstance(item, dict) and "text" in item:
                            result_parts.append(item["text"])
                elif "toolUse" in delta:
                    tool_input = delta.get("toolUse", {}).get("input", "")
                    if tool_input:
                        input_parts.append(tool_input)

            elif key == "contentBlockStart":
                start = value.get("start", {})
                if "toolUse" in start:
                    tool_use = start["toolUse"]
                    active_use_id = tool_use.get("toolUseId")
                    active_name = tool_use.get("name", "tool")
                    input_parts = []
                    if active_use_id:
                        tool_names[active_use_id] = active_name
                elif "toolResult" in start:
                    tool_result = start["toolResult"]
                    active_result_id = tool_result.get("toolUseId")
                    active_result_name = tool_names.get(active_result_id, "tool")
                    result_parts = []

            elif key == "contentBlockStop":
                if active_result_name:
                    raw_result = "".join(result_parts)
                    event_queue.put_nowait(
                        (
                            "event",
                            {
                                "type": "tool_end",
                                "source": "managed",
                                "tool_use_id": active_result_id,
                                "name": active_result_name,
                                "result": raw_result,
                                "summary": _summarize_tool_result(active_result_name, raw_result),
                            },
                        )
                    )
                    active_result_name = None
                    active_result_id = None
                    result_parts = []
                elif active_use_id:
                    raw_input = "".join(input_parts)
                    tool_calls += 1
                    if active_name:
                        called_tool_names.append(active_name)
                    event_queue.put_nowait(
                        (
                            "event",
                            {
                                "type": "tool_start",
                                "source": "managed",
                                "tool_use_id": active_use_id,
                                "name": active_name,
                                "input": _normalized_tool_input(raw_input),
                            },
                        )
                    )
                    active_use_id = None
                    active_name = None
                    input_parts = []

            elif key == "textOutput" and value.get("text"):
                event_queue.put_nowait(
                    (
                        "event",
                        {
                            "type": "text",
                            "source": "managed",
                            "content": value["text"],
                        },
                    )
                )
            elif key == "metadata":
                usage = value.get("usage", {})
                saw_usage = saw_usage or bool(usage)
                for token_name in token_totals:
                    token_totals[token_name] += usage.get(token_name, 0)

    if controller is None or not controller.cancelled.is_set() or saw_usage:
        token_totals["totalTokens"] = sum(token_totals.values())
        event_queue.put_nowait(
            (
                "event",
                {
                    "type": "metrics",
                    "usage": token_totals,
                    "tool_calls": tool_calls,
                    "tool_names": called_tool_names,
                    "model_id": _selected_model_id(options),
                    "model_tier": options.get("model_tier", "strong"),
                    "model_route_reason": options.get("model_route_reason", "default model"),
                },
            )
        )
    event_queue.put_nowait(("elapsed", round(time.time() - started_at, 2)))
    event_queue.put_nowait(("done", None))
