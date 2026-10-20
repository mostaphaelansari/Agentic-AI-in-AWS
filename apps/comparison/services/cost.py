"""Convert measured per-run usage into dollars using published list rates.

Every input here is measured by the app (token usage from the model stream,
tool invocations from the event stream); only the rates are list prices.
"""

from __future__ import annotations

import config as app_config
from config import AGENTCORE_GATEWAY_PRICING, BEDROCK_TOKEN_PRICING
from formatting.browser import is_browser_tool

BEDROCK_CHEAP_MODEL_ID = getattr(
    app_config,
    "BEDROCK_CHEAP_MODEL_ID",
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
)
BEDROCK_CHEAP_TOKEN_PRICING = getattr(
    app_config,
    "BEDROCK_CHEAP_TOKEN_PRICING",
    {
        "input_per_mtok": 1.0,
        "output_per_mtok": 5.0,
        "cache_read_per_mtok": 0.1,
        "cache_write_per_mtok": 1.25,
    },
)


def is_metered_builtin_tool(tool_name: str) -> bool:
    """Browser/Code Interpreter are compute-metered, not Gateway operations."""
    normalized = (tool_name or "").lower()
    return is_browser_tool(normalized) or "code_interpreter" in normalized


def token_rates(model_id: str | None = None) -> dict:
    """Return configured list rates for the model that served the run."""
    normalized_model_id = (model_id or "").lower()
    if model_id == BEDROCK_CHEAP_MODEL_ID or "claude-haiku-4-5" in normalized_model_id:
        return BEDROCK_CHEAP_TOKEN_PRICING
    return BEDROCK_TOKEN_PRICING


def inference_cost(usage: dict, model_id: str | None = None) -> dict:
    """Token cost for one run; `usage` is the dict from normalized_usage()."""
    rates = token_rates(model_id)
    input_cost = usage["fresh_input"] / 1e6 * rates["input_per_mtok"]
    cache_read_cost = usage["cache_read"] / 1e6 * rates["cache_read_per_mtok"]
    cache_write_cost = usage["cache_write"] / 1e6 * rates["cache_write_per_mtok"]
    output_cost = usage["output"] / 1e6 * rates["output_per_mtok"]
    return {
        "input_cost": input_cost,
        "cache_read_cost": cache_read_cost,
        "cache_write_cost": cache_write_cost,
        "output_cost": output_cost,
        "total_cost": input_cost + cache_read_cost + cache_write_cost + output_cost,
    }


def uncached_inference_cost(usage: dict, model_id: str | None = None) -> float:
    """Token cost if all input-side tokens had been billed at the fresh input rate."""
    rates = token_rates(model_id)
    input_side_tokens = usage["fresh_input"] + usage["cache_read"] + usage["cache_write"]
    input_cost = input_side_tokens / 1e6 * rates["input_per_mtok"]
    output_cost = usage["output"] / 1e6 * rates["output_per_mtok"]
    return input_cost + output_cost


def cache_savings(usage: dict, model_id: str | None = None) -> float:
    """Net dollar effect of prompt caching this run vs sending all input fresh.

    Cache reads are billed at the cache-read rate instead of the input rate;
    cache writes carry a premium over the input rate. Positive means caching
    saved money on this run.
    """
    return uncached_inference_cost(usage, model_id) - inference_cost(usage, model_id)["total_cost"]


def gateway_operations(metrics: dict) -> int:
    """Measured Gateway MCP operations for one run.

    Browser actions are billed by browser compute, not Gateway, so they are
    excluded. Without per-tool names, browser-only runs (browse_calls set)
    count zero and other runs fall back to the raw tool-call count.
    """
    tool_names = metrics.get("tool_names") or []
    if tool_names:
        return sum(1 for name in tool_names if not is_metered_builtin_tool(name))
    if int(metrics.get("browse_calls", 0) or 0):
        return 0
    return int(metrics.get("tool_calls", 0) or 0)


def builtin_tool_calls(metrics: dict) -> int:
    """Count calls whose compute cost needs separate Browser/CI usage logs."""
    tool_names = metrics.get("tool_names") or []
    if tool_names:
        return sum(1 for name in tool_names if is_metered_builtin_tool(name))
    return int(metrics.get("browse_calls", 0) or 0)


def builtin_tool_kinds(metrics: dict) -> list[str]:
    """Return the distinct AgentCore compute meters exercised by a run."""
    kinds: set[str] = set()
    for name in metrics.get("tool_names") or []:
        normalized = str(name).lower()
        if is_browser_tool(normalized):
            kinds.add("browser")
        if "code_interpreter" in normalized:
            kinds.add("code_interpreter")
    if int(metrics.get("browse_calls", 0) or 0):
        kinds.add("browser")
    return sorted(kinds)


def gateway_cost(operations: int) -> float:
    return operations / 1000.0 * AGENTCORE_GATEWAY_PRICING["mcp_per_1k_invocations"]
