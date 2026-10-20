"""Code-defined tool-using agent hosted on AgentCore Runtime.

Strands agent with the shared Gateway tools and AgentCore Code Interpreter.
AgentCore Browser is optional and disabled in parity benchmarks because the
open-source architecture uses the shared MCP ``webSearch`` tool instead.
Streams typed events via a sync generator so BedrockAgentCoreApp wraps them
as SSE automatically.

Heavy imports (strands, strands_tools) are deferred to first invocation
to stay under the 30s cold-start init limit.

Event contract (each yield is one SSE `data:` line):
    {"type": "text",       "content": "..."}          — model text delta
    {"type": "tool_start", "name": "...", "input": {}} — tool call begins
    {"type": "tool_end",   "name": "...", "result": "..."} — tool returns
    {"type": "error",      "content": "..."}           — handler-level error
    {"type": "metrics",    "usage": {...}, "tool_calls": N, "cycles": N}
"""

import contextvars
import hashlib
import logging
import os
import queue
import re
import threading
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("runtime")

ENABLE_BROWSER = os.environ.get("ENABLE_BROWSER", "false").lower() in {
    "1",
    "true",
    "yes",
}


def _configure_mlflow_trace_exporter() -> None:
    """Mirror in-process OTEL spans to MLflow without replacing AWS ADOT."""
    endpoint = os.environ.get("MLFLOW_OTLP_TRACES_ENDPOINT")
    if not endpoint:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = trace.get_tracer_provider()
        add_span_processor = getattr(provider, "add_span_processor", None)
        if not callable(add_span_processor):
            logger.warning(
                "MLflow trace export is configured, but no SDK tracer provider is active; "
                "start the agent with opentelemetry-instrument"
            )
            return

        experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID", "0")
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers={"x-mlflow-experiment-id": experiment_id},
        )
        add_span_processor(BatchSpanProcessor(exporter))
        logger.info(
            "MLflow trace mirror enabled experiment_id=%s",
            experiment_id,
        )
    except Exception:
        # Observability must not make the agent unavailable. The AWS ADOT
        # exporter remains active even if the optional MLflow mirror is bad.
        logger.exception("failed to configure optional MLflow trace mirror")


_configure_mlflow_trace_exporter()

# ---------------------------------------------------------------------------
# Monkey-patch: tolerate unescaped control characters in the request body.
# The runtime parses the body with strict JSON (starlette Request.json ->
# json.loads, strict=True), which rejects raw newlines/tabs inside strings and
# returns HTTP 400 "Invalid JSON: Invalid control character" *before* the
# handler runs. The runtime playground sends prompts verbatim, so a multi-line
# prompt 400s. Parse leniently (strict=False) so freeform prompt text is
# accepted; the control char is preserved as-is in the parsed string.
# ---------------------------------------------------------------------------
import json as _json

from bedrock_agentcore import BedrockAgentCoreApp
from starlette.requests import Request as _Request


async def _lenient_request_json(self):
    if not hasattr(self, "_json"):
        body = await self.body()
        self._json = _json.loads(body, strict=False)
        _annotate_current_request_span(self._json)
    return self._json


_Request.json = _lenient_request_json


def _mlflow_request_preview(payload: Any) -> str | None:
    """Return the chat-shaped request preview MLflow uses in its trace table."""
    if not isinstance(payload, dict):
        return None

    prompt = payload.get("prompt") or payload.get("message") or payload.get("goal")
    if not prompt:
        return None

    return _json.dumps(
        [{"role": "user", "content": [{"text": str(prompt)}]}],
        ensure_ascii=False,
        default=str,
    )


def _annotate_current_request_span(payload: Any) -> None:
    """Attach MLflow request preview to the HTTP root span for trace list rendering."""
    request_json = _mlflow_request_preview(payload)
    if request_json is None:
        return

    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if not span.is_recording():
            return

        span.set_attribute("mlflow.spanType", "AGENT")
        span.set_attribute("mlflow.spanInputs", request_json)
        span.add_event("gen_ai.user.message", {"content": request_json})
    except Exception:
        logger.debug("failed to annotate request span for MLflow", exc_info=True)


def _annotate_current_tool_span(*, arguments: Any | None = None, result: Any | None = None) -> None:
    """Add MLflow-compatible metadata to Strands' existing execute_tool span."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if not span.is_recording():
            return

        # Strands already emits gen_ai.operation.name=execute_tool. Supplying
        # the MLflow type explicitly also populates MLflow's SQL span type,
        # which powers the Tool Calls dashboard.
        span.set_attribute("mlflow.spanType", "TOOL")
        if arguments is not None:
            span.set_attribute(
                "gen_ai.tool.call.arguments",
                _json.dumps(arguments, ensure_ascii=False, default=str),
            )
        if result is not None:
            span.set_attribute(
                "gen_ai.tool.call.result",
                _json.dumps(result, ensure_ascii=False, default=str),
            )
    except Exception:
        # Telemetry enrichment must never interrupt an actual tool call.
        logger.debug("failed to annotate tool span for MLflow", exc_info=True)


def _annotate_current_invocation_span(
    *,
    prompt: str,
    mode: str,
    weather_only: bool,
    model_id: str | None = None,
    model_tier: str = "strong",
    model_route_reason: str = "default model",
    model_router_mode: str = "unknown",
    model_router_name: str = "unknown",
    model_router_version: str = "unknown",
    model_router_candidate: str = "",
    model_router_outcome: str = "unknown",
    model_router_fallback: bool = False,
    model_router_latency_ms: float = 0.0,
) -> None:
    """Add MLflow-compatible request metadata to the root invocation span."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if not span.is_recording():
            return

        request_json = _json.dumps(
            [{"role": "user", "content": [{"text": prompt}]}],
            ensure_ascii=False,
            default=str,
        )

        # MLflow's trace table renders the Request column from the root span's
        # mlflow.spanInputs attribute. The raw OTLP mirror does not create that
        # for us, so attach it explicitly while preserving the AWS trace path.
        span.set_attribute("mlflow.spanType", "AGENT")
        span.set_attribute("mlflow.spanInputs", request_json)
        span.set_attribute("gen_ai.request.model", model_id or MODEL_ID)
        span.set_attribute("agent.model.tier", model_tier)
        span.set_attribute("agent.model.route_reason", model_route_reason)
        routing_attributes = {
            "agent.model.router.mode": model_router_mode,
            "agent.model.router.name": model_router_name,
            "agent.model.router.version": model_router_version,
            "agent.model.router.candidate": model_router_candidate,
            "agent.model.router.outcome": model_router_outcome,
            "agent.model.router.fallback": model_router_fallback,
            "agent.model.router.latency_ms": model_router_latency_ms,
            "agent.model.selected_tier": model_tier,
            "agent.model.selected_id": model_id or MODEL_ID,
        }
        for key, value in routing_attributes.items():
            span.set_attribute(key, value)
        span.add_event("gen_ai.user.message", {"content": request_json})
    except Exception:
        # Observability enrichment must never interrupt an actual invocation.
        logger.debug("failed to annotate invocation span for MLflow", exc_info=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REGION = os.environ.get("AWS_REGION", "eu-west-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-4-6")
CHEAP_MODEL_ID = os.environ.get(
    "BEDROCK_CHEAP_MODEL_ID",
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
)
BEDROCK_PROMPT_ID = os.environ.get("BEDROCK_PROMPT_ID")
BEDROCK_PROMPT_VERSION = os.environ.get("BEDROCK_PROMPT_VERSION")
BEDROCK_PROMPT_SHA256 = os.environ.get("BEDROCK_PROMPT_SHA256")
MAX_TOOL_CALLS = int(os.environ.get("MAX_TOOL_CALLS", "12"))
if MAX_TOOL_CALLS < 1:
    raise ValueError("MAX_TOOL_CALLS must be at least 1")
OFFLOAD_BUCKET = os.environ.get("OFFLOAD_BUCKET", "bedrock-agentcore-d-artifacts-554025156005")
OFFLOAD_MAX_TOKENS = int(os.environ.get("OFFLOAD_MAX_TOKENS", "2500"))
OFFLOAD_PREVIEW_TOKENS = int(os.environ.get("OFFLOAD_PREVIEW_TOKENS", "800"))
WEATHER_ONLY_MODES = {"weather", "weather_only", "api", "api_only"}
ENABLE_CODE_INTERPRETER = os.environ.get("ENABLE_CODE_INTERPRETER", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ENABLE_THINKING = os.environ.get("ENABLE_THINKING", "").lower() in {"1", "true", "yes", "on"}
ENABLE_PROMPT_CACHING = os.environ.get("ENABLE_PROMPT_CACHING", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
THINKING_BUDGET_TOKENS = int(os.environ.get("THINKING_BUDGET_TOKENS", "1024"))
BEDROCK_MAX_TOKENS = int(os.environ.get("BEDROCK_MAX_TOKENS", "4096"))
ENABLE_INTERLEAVED_THINKING = os.environ.get("ENABLE_INTERLEAVED_THINKING", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SKILLS_S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", "").strip()
SKILLS_S3_PREFIX = os.environ.get("SKILLS_S3_PREFIX", "harness-skills").strip("/")
_SKILL_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
_LEGACY_EMBEDDED_SKILLS = re.compile(
    r"\n## Open-source skills\n.*?(?=\n## (?:Tool-use budget|How to answer)\n)",
    re.DOTALL,
)


def _extract_prompt_management_system_prompt(response: dict[str, Any]) -> str:
    """Extract the first system text block from a Prompt Management response."""
    variants = response.get("variants") or []
    if not variants:
        raise ValueError("Prompt Management response has no variants")

    chat = variants[0].get("templateConfiguration", {}).get("chat", {})
    for block in chat.get("system") or []:
        text = block.get("text")
        if isinstance(text, str) and text:
            return text
    raise ValueError("Prompt Management response has no system text block")


@lru_cache(maxsize=1)
def _load_prompt_management_system_prompt(prompt_id: str, prompt_version: str) -> str:
    """Load the pinned immutable system prompt from Bedrock Prompt Management."""
    import boto3

    client = boto3.client("bedrock-agent", region_name=REGION)
    response = client.get_prompt(
        promptIdentifier=prompt_id,
        promptVersion=prompt_version,
    )
    system_prompt = _extract_prompt_management_system_prompt(response)
    actual_sha256 = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    if BEDROCK_PROMPT_SHA256 and actual_sha256 != BEDROCK_PROMPT_SHA256:
        raise ValueError(
            "Bedrock Prompt Management SHA-256 mismatch: "
            f"expected={BEDROCK_PROMPT_SHA256} actual={actual_sha256}"
        )
    logger.info(
        "loaded_prompt_management_prompt prompt_id=%s prompt_version=%s prompt_sha256=%s",
        prompt_id,
        prompt_version,
        actual_sha256,
    )
    return system_prompt


def _load_system_prompt() -> str:
    from datetime import date

    if BEDROCK_PROMPT_ID and BEDROCK_PROMPT_VERSION:
        return _LEGACY_EMBEDDED_SKILLS.sub(
            "",
            _load_prompt_management_system_prompt(
                BEDROCK_PROMPT_ID,
                BEDROCK_PROMPT_VERSION,
            ),
        )

    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "system.md")
    try:
        with open(prompt_path) as f:
            template = f.read().strip()
    except FileNotFoundError:
        logger.warning("prompts/system.md not found, using fallback prompt")
        template = (
            "You are an assistant with Open-Meteo weather tools and web browsing. "
            "Use the weather tools for weather questions; browse only when a question "
            "requires a page, URL, or current information. Today's date is {today}."
        )
    return _LEGACY_EMBEDDED_SKILLS.sub(
        "",
        template.format(today=date.today().isoformat()),
    ).strip()


def _parse_skill_document(key: str, document: str) -> dict[str, Any] | None:
    """Parse one SKILL.md fetched from the shared S3 skill registry."""
    match = _SKILL_FRONTMATTER.match(document)
    if not match:
        logger.warning("S3 skill without frontmatter skipped: s3://%s/%s", SKILLS_S3_BUCKET, key)
        return None

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            meta[name.strip()] = value.strip()
    fallback_name = key.rstrip("/").split("/")[-2]
    description = meta.get("description", "").strip()
    if not description:
        logger.warning("S3 skill without description skipped: s3://%s/%s", SKILLS_S3_BUCKET, key)
        return None
    return {
        "name": meta.get("name", fallback_name),
        "description": description,
        "body": match.group(2).strip(),
    }


def _scan_s3_skills() -> list[dict[str, Any]]:
    """Load all <prefix>/<name>/SKILL.md objects from the shared S3 bucket."""
    if not SKILLS_S3_BUCKET:
        return []

    import boto3

    client = boto3.client("s3", region_name=REGION)
    prefix = f"{SKILLS_S3_PREFIX}/" if SKILLS_S3_PREFIX else ""
    skills: list[dict[str, Any]] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=SKILLS_S3_BUCKET, Prefix=prefix)
        keys = sorted(
            item["Key"]
            for page in pages
            for item in page.get("Contents", [])
            if item["Key"].endswith("/SKILL.md")
        )
        for key in keys:
            response = client.get_object(Bucket=SKILLS_S3_BUCKET, Key=key)
            document = response["Body"].read().decode("utf-8")
            skill = _parse_skill_document(key, document)
            if skill is not None:
                skills.append(skill)
    except Exception as exc:
        logger.exception(
            "failed to load skills from s3://%s/%s",
            SKILLS_S3_BUCKET,
            SKILLS_S3_PREFIX,
        )
        raise RuntimeError("Failed to load the configured semantic skill catalog") from exc
    return skills


def _build_skills_plugin():
    """Expose shared S3 skills through Strands' semantic activation tool."""
    from strands import AgentSkills, Skill

    skills = [
        Skill(
            name=skill["name"],
            description=skill["description"],
            instructions=skill["body"],
        )
        for skill in _scan_s3_skills()
    ]
    if not skills:
        if SKILLS_S3_BUCKET:
            raise RuntimeError("The configured semantic skill catalog contains no valid skills")
        return None
    return AgentSkills(skills=skills, strict=True)


# ---------------------------------------------------------------------------
# Streaming callback handler — pushes events to a queue
# ---------------------------------------------------------------------------
class StreamingCallbackHandler:
    """Callback handler that pushes text deltas to a queue for SSE streaming."""

    # That callback receives small model text chunks from Strands, then pushes them into our SSE queue
    def __init__(self, event_queue: queue.Queue):
        self.q = event_queue
        self.saw_reasoning = False

    def __call__(self, **kwargs: Any) -> None:
        reasoning = kwargs.get("reasoningText", "")
        if reasoning:
            if not self.saw_reasoning:
                logger.info("thinking_status source=custom_runtime used=true")
                self.saw_reasoning = True
            self.q.put({"type": "reasoning", "content": reasoning})

        data = kwargs.get("data", "")
        if data:
            self.q.put({"type": "text", "content": data})


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _bedrock_model_kwargs(
    cache_config_cls,
    *,
    model_id: str | None = None,
    enable_caching: bool | None = None,
    enable_thinking: bool | None = None,
    reasoning_effort: str | None = None,
    thinking_budget_tokens: int | None = None,
    interleaved_thinking: bool | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model_id": model_id or MODEL_ID,
        "region_name": REGION,
        "max_tokens": BEDROCK_MAX_TOKENS,
    }

    caching_enabled = ENABLE_PROMPT_CACHING if enable_caching is None else enable_caching
    if caching_enabled:
        kwargs.update(
            {
                "cache_config": cache_config_cls(strategy="auto"),
                "cache_prompt": "default",
                "cache_tools": "default",
            }
        )

    thinking_enabled = bool(reasoning_effort) or (
        ENABLE_THINKING if enable_thinking is None else enable_thinking
    )
    if thinking_enabled:
        selected_model_id = str(kwargs["model_id"]).casefold()
        effort = str(reasoning_effort or "").strip().lower()
        valid_efforts = {"low", "medium", "high", "max"}
        if effort:
            if effort not in valid_efforts:
                raise ValueError("reasoning_effort must be one of: low, medium, high, max")
            if not any(
                family in selected_model_id for family in ("claude-sonnet-4-6", "claude-opus-4-6")
            ):
                raise ValueError("adaptive reasoning requires Claude Sonnet 4.6 or Opus 4.6")
            if effort == "max" and "claude-opus-4-6" not in selected_model_id:
                raise ValueError("reasoning_effort=max requires Claude Opus 4.6")
            additional_request_fields: dict[str, Any] = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": effort},
            }
        else:
            budget_tokens = thinking_budget_tokens or THINKING_BUDGET_TOKENS
            if budget_tokens < 1024:
                raise ValueError("THINKING_BUDGET_TOKENS must be at least 1024")
            if BEDROCK_MAX_TOKENS <= budget_tokens:
                raise ValueError("BEDROCK_MAX_TOKENS must be greater than THINKING_BUDGET_TOKENS")
            additional_request_fields = {
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": budget_tokens,
                }
            }
            use_interleaved = (
                ENABLE_INTERLEAVED_THINKING
                if interleaved_thinking is None
                else interleaved_thinking
            )
            if use_interleaved:
                additional_request_fields["anthropic_beta"] = ["interleaved-thinking-2025-05-14"]
        kwargs["additional_request_fields"] = additional_request_fields
    return kwargs


def _public_agent_error(exc: Exception) -> str:
    if type(exc).__name__ == "MaxTokensReachedException":
        return (
            f"The response exceeded the {BEDROCK_MAX_TOKENS:,}-token output limit. "
            "Retry with a narrower request or fewer requested details."
        )
    return str(exc)


# ---------------------------------------------------------------------------
# Hook provider — captures tool_start / tool_end via queue
# ---------------------------------------------------------------------------
class ToolEventHook:
    """Strands hook that pushes tool_start / tool_end events to a queue."""

    def __init__(self, event_queue: queue.Queue, max_calls: int = MAX_TOOL_CALLS):
        self.q = event_queue
        self.max_calls = max_calls
        self.count = 0
        self.tool_names: list[str] = []

    def register_hooks(self, registry, **kwargs) -> None:
        from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent

        registry.add_callback(BeforeToolCallEvent, self._on_before)
        registry.add_callback(AfterToolCallEvent, self._on_after)

    def _on_before(self, event) -> None:
        self.count += 1
        tool_name = event.tool_use.get("name", "unknown")
        self.tool_names.append(tool_name)
        tool_input = event.tool_use.get("input", {})

        # Truncate input for the SSE event
        input_summary = {}
        for k, v in (tool_input if isinstance(tool_input, dict) else {}).items():
            s = str(v)
            input_summary[k] = s[:200] if len(s) > 200 else s

        _annotate_current_tool_span(arguments=input_summary)
        self.q.put({"type": "tool_start", "name": tool_name, "input": input_summary})

        if self.count > self.max_calls:
            event.cancel_tool = (
                f"Tool call cap reached ({self.max_calls}). Stop calling tools "
                "and produce your final answer now."
            )
            logger.warning("tool_call_cap count=%d max=%d", self.count, self.max_calls)

    def _on_after(self, event) -> None:
        tool_name = event.tool_use.get("name", "unknown")
        result_limit = 12000 if tool_name.rsplit("___", 1)[-1] == "code_interpreter" else 2000
        result_str = (
            str(event.result.get("content", ""))[:result_limit]
            if isinstance(event.result, dict)
            else str(event.result)[:result_limit]
        )
        _annotate_current_tool_span(result=result_str)
        self.q.put({"type": "tool_end", "name": tool_name, "result": result_str})


# ---------------------------------------------------------------------------
# Agent factory — lazy imports on first call
# ---------------------------------------------------------------------------
def _patch_browser_timeouts() -> None:
    """Override two Strands browser methods that block for 30s on modern SPAs.

    Upstream defects:
      1. _async_get_text calls page.text_content() with no timeout (30s default).
      2. _async_navigate calls wait_for_load_state("networkidle"), which never
         resolves on sites with persistent background traffic (analytics, chat,
         ads). Switch to "domcontentloaded" and cap goto at 15s.
    """
    from strands_tools.browser.browser import Browser

    async def _async_get_text(self, action):
        err = self.validate_session(action.session_name)
        if err:
            return err
        page = self.get_session_page(action.session_name)
        if not page:
            return {"status": "error", "content": [{"text": "Error: No active page for session"}]}
        try:
            text = await page.text_content(action.selector, timeout=5000)
            return {"status": "success", "content": [{"text": f"Text content: {text}"}]}
        except Exception as e:
            return {"status": "error", "content": [{"text": f"Error: {e}"}]}

    async def _async_navigate(self, action):
        err = self.validate_session(action.session_name)
        if err:
            return err
        page = self.get_session_page(action.session_name)
        if not page:
            return {"status": "error", "content": [{"text": "Error: No active page for session"}]}
        try:
            await page.goto(action.url, timeout=15000, wait_until="domcontentloaded")
            return {"status": "success", "content": [{"text": f"Navigated to {action.url}"}]}
        except Exception as e:
            es = str(e)
            if "ERR_NAME_NOT_RESOLVED" in es:
                msg = f"Could not resolve domain '{action.url}'."
            elif "ERR_CONNECTION_REFUSED" in es:
                msg = f"Connection refused for '{action.url}'."
            elif "ERR_CONNECTION_TIMED_OUT" in es:
                msg = f"Connection timed out for '{action.url}'."
            else:
                msg = es
            return {"status": "error", "content": [{"text": f"Error: {msg}"}]}

    Browser._async_get_text = _async_get_text
    Browser._async_navigate = _async_navigate


def _build_agent(
    event_queue: queue.Queue,
    extra_tools: list | None = None,
    weather_only: bool = False,
    model_id: str | None = None,
    enable_caching: bool | None = None,
    enable_thinking: bool | None = None,
    reasoning_effort: str | None = None,
    thinking_budget_tokens: int | None = None,
    interleaved_thinking: bool | None = None,
    code_session_name: str | None = None,
):
    from strands import Agent
    from strands.models import BedrockModel, CacheConfig
    from strands.tools.executors import SequentialToolExecutor

    tool_hook = ToolEventHook(event_queue, max_calls=MAX_TOOL_CALLS)
    callback = StreamingCallbackHandler(event_queue)

    tools = []
    plugins = []
    skills_plugin = _build_skills_plugin()
    if skills_plugin is not None:
        plugins.append(skills_plugin)
    if not weather_only:
        from strands.vended_plugins.context_offloader import ContextOffloader, S3Storage

        if ENABLE_BROWSER:
            from strands_tools.browser import AgentCoreBrowser

            _patch_browser_timeouts()
            browser_tool = AgentCoreBrowser(region=REGION)
            tools.append(browser_tool.browser)
        if ENABLE_CODE_INTERPRETER:
            from strands_tools.code_interpreter import AgentCoreCodeInterpreter

            # session_name = AgentCore runtime session id, so the module-level
            # cache reconnects the same sandbox across turns of a conversation.
            interpreter = AgentCoreCodeInterpreter(
                region=REGION,
                session_name=code_session_name,
            )
            tools.append(interpreter.code_interpreter)
        plugins.append(
            ContextOffloader(
                storage=S3Storage(bucket=OFFLOAD_BUCKET, prefix="tool-results/"),
                max_result_tokens=OFFLOAD_MAX_TOKENS,
                preview_tokens=OFFLOAD_PREVIEW_TOKENS,
            )
        )
    if extra_tools:
        tools.extend(extra_tools)

    agent = Agent(
        name="custom_runtime_agent",
        model=BedrockModel(
            **_bedrock_model_kwargs(
                CacheConfig,
                model_id=model_id,
                enable_caching=enable_caching,
                enable_thinking=enable_thinking,
                reasoning_effort=reasoning_effort,
                thinking_budget_tokens=thinking_budget_tokens,
                interleaved_thinking=interleaved_thinking,
            )
        ),
        tools=tools,
        tool_executor=SequentialToolExecutor(),
        system_prompt=_load_system_prompt(),
        callback_handler=callback,
        hooks=[tool_hook],
        plugins=plugins,
    )
    return agent, tool_hook, callback


def _emit_metrics(
    event_queue: queue.Queue,
    agent_result,
    tool_hook: ToolEventHook,
    *,
    session_id: str | None = None,
    model_id: str = MODEL_ID,
    model_tier: str = "strong",
    model_route_reason: str = "default model",
) -> None:
    usage = agent_result.metrics.accumulated_usage
    usage_payload = {
        "inputTokens": usage.get("inputTokens", 0),
        "outputTokens": usage.get("outputTokens", 0),
        "totalTokens": usage.get("totalTokens", 0),
        "cacheReadInputTokens": usage.get("cacheReadInputTokens", 0),
        "cacheWriteInputTokens": usage.get("cacheWriteInputTokens", 0),
    }
    if session_id:
        logger.info(
            "comparison_token_usage=%s",
            _json.dumps(
                {"session_id": session_id, "usage": usage_payload},
                separators=(",", ":"),
            ),
        )
    try:
        event_queue.put_nowait(
            {
                "type": "metrics",
                "usage": usage_payload,
                "tool_calls": tool_hook.count,
                "tool_names": tool_hook.tool_names,
                "cycles": agent_result.metrics.cycle_count,
                "model_id": model_id,
                "model_tier": model_tier,
                "model_route_reason": model_route_reason,
            }
        )
    except queue.Full:
        logger.warning("dropping final metrics event because the client queue is full")


# ---------------------------------------------------------------------------
# BedrockAgentCoreApp entrypoint — sync generator for SSE streaming
# ---------------------------------------------------------------------------
app = BedrockAgentCoreApp()

_DONE = object()


@app.entrypoint
def invoke(payload, context):
    """Sync generator entrypoint. Each yield becomes an SSE event."""
    prompt = payload.get("prompt") or payload.get("message") or payload.get("goal") or ""
    mode = str(payload.get("mode") or "").lower()
    weather_only = bool(payload.get("weather_only")) or mode in WEATHER_ONLY_MODES
    enable_caching = _as_bool(payload.get("enable_caching"), default=ENABLE_PROMPT_CACHING)
    enable_thinking = _as_bool(payload.get("enable_thinking"), default=ENABLE_THINKING)
    reasoning_effort = str(payload.get("reasoning_effort") or "").strip().lower() or None
    thinking_budget_tokens = int(payload.get("thinking_budget_tokens") or THINKING_BUDGET_TOKENS)
    interleaved_thinking = _as_bool(
        payload.get("interleaved_thinking"),
        default=ENABLE_INTERLEAVED_THINKING,
    )
    requested_model_tier = str(payload.get("model_tier") or "strong").strip().lower()
    model_tier = "cheap" if requested_model_tier == "cheap" else "strong"
    model_id = CHEAP_MODEL_ID if model_tier == "cheap" else MODEL_ID
    model_route_reason = str(payload.get("model_route_reason") or "default model")[:160]
    model_router_mode = str(payload.get("model_router_mode") or "unknown")[:32]
    model_router_name = str(payload.get("model_router_name") or "unknown")[:64]
    model_router_version = str(payload.get("model_router_version") or "unknown")[:128]
    model_router_candidate = str(payload.get("model_router_candidate") or "")[:160]
    model_router_outcome = str(payload.get("model_router_outcome") or "unknown")[:64]
    model_router_fallback = _as_bool(payload.get("model_router_fallback"), default=False)
    try:
        model_router_latency_ms = max(0.0, float(payload.get("model_router_latency_ms") or 0.0))
    except (TypeError, ValueError):
        model_router_latency_ms = 0.0
    code_session_name = getattr(context, "session_id", None)
    if not prompt:
        yield {"type": "error", "content": "No prompt provided"}
        return

    _annotate_current_invocation_span(
        prompt=prompt,
        mode=mode,
        weather_only=weather_only,
        model_id=model_id,
        model_tier=model_tier,
        model_route_reason=model_route_reason,
        model_router_mode=model_router_mode,
        model_router_name=model_router_name,
        model_router_version=model_router_version,
        model_router_candidate=model_router_candidate,
        model_router_outcome=model_router_outcome,
        model_router_fallback=model_router_fallback,
        model_router_latency_ms=model_router_latency_ms,
    )

    event_queue: queue.Queue = queue.Queue(maxsize=1000)
    agent_holder: dict[str, Any] = {}
    model_metadata_pending = True

    def _run_agent():
        from contextlib import nullcontext

        agent_result = None
        tool_hook = None
        try:
            gateway_url = os.environ.get("GATEWAY_URL")
            mcp = None
            if gateway_url:
                try:
                    from mcp_client.gateway import get_gateway_mcp_client

                    mcp = get_gateway_mcp_client(gateway_url)
                except Exception as gw_err:
                    if weather_only:
                        raise RuntimeError(
                            f"gateway client unavailable in weather-only mode: {gw_err}"
                        ) from gw_err
                    logger.warning(
                        "gateway client unavailable, falling back to browser-only: %s", gw_err
                    )
            elif weather_only:
                raise RuntimeError("GATEWAY_URL is required in weather-only mode")

            with mcp if mcp is not None else nullcontext():
                extra_tools = None
                if mcp is not None:
                    try:
                        extra_tools = mcp.list_tools_sync()
                    except Exception as gw_err:
                        if weather_only:
                            raise RuntimeError(
                                f"gateway list_tools failed in weather-only mode: {gw_err}"
                            ) from gw_err
                        logger.warning(
                            "gateway list_tools failed, falling back to browser-only: %s", gw_err
                        )
                agent, tool_hook, callback = _build_agent(
                    event_queue,
                    extra_tools=extra_tools,
                    weather_only=weather_only,
                    model_id=model_id,
                    enable_caching=enable_caching,
                    enable_thinking=enable_thinking,
                    reasoning_effort=reasoning_effort,
                    thinking_budget_tokens=thinking_budget_tokens,
                    interleaved_thinking=interleaved_thinking,
                    code_session_name=code_session_name,
                )
                agent_holder["agent"] = agent
                agent_result = agent(prompt)
                logger.info(
                    "thinking_status source=custom_runtime requested=%s used=%s reasoning_events_seen=%s",
                    enable_thinking,
                    callback.saw_reasoning,
                    int(callback.saw_reasoning),
                )
        except Exception as e:
            logger.exception("agent invocation failed")
            event_queue.put({"type": "error", "content": _public_agent_error(e)})
        finally:
            if agent_result is not None and tool_hook is not None:
                try:
                    _emit_metrics(
                        event_queue,
                        agent_result,
                        tool_hook,
                        session_id=code_session_name,
                        model_id=model_id,
                        model_tier=model_tier,
                        model_route_reason=model_route_reason,
                    )
                except Exception:
                    logger.exception("failed to emit final usage metrics")
            try:
                event_queue.put_nowait(_DONE)
            except queue.Full:
                logger.warning("dropping done marker because the disconnected client queue is full")

    # Propagate OTEL trace context (and any other contextvars) into the agent
    # thread so its spans get linked to the outer AgentCore.Runtime.Invoke span
    # instead of becoming orphaned traces in CloudWatch.
    ctx = contextvars.copy_context()
    thread = threading.Thread(target=ctx.run, args=(_run_agent,), daemon=True)
    thread.start()

    # Yield events as they arrive from the agent thread. A disconnected client
    # closes this generator; Strands' public, thread-safe cancellation then
    # stops at the next model-stream or pre-tool checkpoint.
    try:
        while True:
            try:
                event = event_queue.get(timeout=300)
            except queue.Empty:
                yield {"type": "error", "content": "Agent timed out after 300s"}
                break

            if event is _DONE:
                break

            # Add the resolved model to the first normal stream event instead
            # of inserting a new leading event. Existing direct Runtime clients
            # therefore retain the same text/tool stream contract.
            if model_metadata_pending and isinstance(event, dict):
                event = {
                    **event,
                    "model_id": model_id,
                    "model_tier": model_tier,
                    "model_route_reason": model_route_reason,
                    "model_metadata_status": "resolved",
                }
                model_metadata_pending = False
            yield event
    except GeneratorExit:
        if agent := agent_holder.get("agent"):
            agent.cancel()
        thread.join(timeout=2)
        logger.info("run cancelled by client session_id=%s", code_session_name)
        raise

    thread.join(timeout=10)


if __name__ == "__main__":
    app.run()
