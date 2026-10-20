"""Application and AgentCore configuration."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(override=True)

RUNTIME_USAGE_LOG_GROUP = os.environ.get(
    "RUNTIME_USAGE_LOG_GROUP",
    "bedrock-custom-agent-runtime",
)
RUNTIME_TOKEN_USAGE_LOG_GROUP = os.environ.get(
    "RUNTIME_TOKEN_USAGE_LOG_GROUP", RUNTIME_USAGE_LOG_GROUP
)
HARNESS_USAGE_LOG_GROUP = os.environ.get("HARNESS_USAGE_LOG_GROUP", "")
HARNESS_BUILTIN_USAGE_LOG_GROUP = os.environ.get("HARNESS_BUILTIN_USAGE_LOG_GROUP", "")
RUNTIME_BUILTIN_USAGE_LOG_GROUP = os.environ.get(
    "RUNTIME_BUILTIN_USAGE_LOG_GROUP", RUNTIME_USAGE_LOG_GROUP
)

ARCHITECTURES = {
    "Managed agent": {
        "id": "Managed",
        "service": "harness",
        "region": os.environ.get("HARNESS_REGION", "eu-central-1"),
        "arn": os.environ.get(
            "HARNESS_ARN",
            "arn:aws:bedrock-agentcore:eu-central-1:554025156005:harness/managed_agent_harness-oVFMi1D2Bn",
        ),
        "usage_log_group": HARNESS_USAGE_LOG_GROUP,
        "builtin_usage_log_group": HARNESS_BUILTIN_USAGE_LOG_GROUP,
    },
    "Custom agent": {
        "id": "Custom",
        "service": "runtime",
        "region": os.environ.get("RUNTIME_REGION", "eu-west-1"),
        "arn": os.environ.get(
            "RUNTIME_ARN",
            "arn:aws:bedrock-agentcore:eu-west-1:554025156005:runtime/custom_agent_runtime-6eyFDU73yN",
        ),
        "streaming": True,
        "usage_log_group": RUNTIME_USAGE_LOG_GROUP,
        "token_usage_log_group": RUNTIME_TOKEN_USAGE_LOG_GROUP,
        "builtin_usage_log_group": RUNTIME_BUILTIN_USAGE_LOG_GROUP,
    },
}

# --- Bedrock token pricing (the dominant cost driver) ---
# Per-1M-token list rates for the comparison model (Claude Sonnet 4.6:
# $3 in / $15 out; cache read 0.1x input, cache write 1.25x input, 5-min TTL).
# Bedrock mirrors Anthropic list pricing for Claude; override per region/model
# via env vars. Usage is measured per run - only the rates are list prices.
BEDROCK_TOKEN_PRICING = {
    "input_per_mtok": float(os.environ.get("BEDROCK_INPUT_PER_MTOK", "3.00")),
    "output_per_mtok": float(os.environ.get("BEDROCK_OUTPUT_PER_MTOK", "15.00")),
    "cache_read_per_mtok": float(os.environ.get("BEDROCK_CACHE_READ_PER_MTOK", "0.30")),
    "cache_write_per_mtok": float(os.environ.get("BEDROCK_CACHE_WRITE_PER_MTOK", "3.75")),
}

# Claude Haiku 4.5 standard on-demand rates. Keep these independently
# overridable because routed runs must not be priced at Sonnet rates.
BEDROCK_CHEAP_TOKEN_PRICING = {
    "input_per_mtok": float(os.environ.get("BEDROCK_CHEAP_INPUT_PER_MTOK", "1.00")),
    "output_per_mtok": float(os.environ.get("BEDROCK_CHEAP_OUTPUT_PER_MTOK", "5.00")),
    "cache_read_per_mtok": float(os.environ.get("BEDROCK_CHEAP_CACHE_READ_PER_MTOK", "0.10")),
    "cache_write_per_mtok": float(os.environ.get("BEDROCK_CHEAP_CACHE_WRITE_PER_MTOK", "1.25")),
}

# --- AgentCore Gateway pricing (per operation, not per token) ---
# MCP operations (ListTools, InvokeTool, Ping) and the Search API are metered
# per 1,000 invocations. The app measures tool invocations per run; list/ping
# overhead is not counted, so the per-run figure is a lower bound.
AGENTCORE_GATEWAY_PRICING = {
    "mcp_per_1k_invocations": float(os.environ.get("AGENTCORE_GATEWAY_MCP_PER_1K", "0.005")),
    "search_per_1k_invocations": float(os.environ.get("AGENTCORE_GATEWAY_SEARCH_PER_1K", "0.025")),
}

# --- AgentCore Runtime pricing and usage-log destination ---
# Runtime compute usage is delivered asynchronously through AgentCore USAGE_LOGS.
# The UI fetches these records by exact runtimeSessionId after a run completes.
AGENTCORE_RUNTIME_PRICING = {
    "vcpu_hour": float(os.environ.get("AGENTCORE_RUNTIME_VCPU_HOUR_USD", "0.0895")),
    "gb_hour": float(os.environ.get("AGENTCORE_RUNTIME_GB_HOUR_USD", "0.00945")),
}

# Browser and Code Interpreter use the same active CPU and memory rates as
# Runtime, but their usage is delivered through their own USAGE_LOGS streams.
AGENTCORE_BUILTIN_TOOL_PRICING = {
    "vcpu_hour": float(os.environ.get("AGENTCORE_BUILTIN_VCPU_HOUR_USD", "0.0895")),
    "gb_hour": float(os.environ.get("AGENTCORE_BUILTIN_GB_HOUR_USD", "0.00945")),
}

ACTOR_ID = os.environ.get("ACTOR_ID", "mostapha")
BEDROCK_STRONG_MODEL_ID = os.environ.get(
    "BEDROCK_STRONG_MODEL_ID",
    os.environ.get("BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-4-6"),
)
BEDROCK_CHEAP_MODEL_ID = os.environ.get(
    "BEDROCK_CHEAP_MODEL_ID",
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
)
MODEL_ROUTING_MODE = os.environ.get("MODEL_ROUTING_MODE", "auto").strip().lower()
# Auto mode uses LiteLLM's LLM complexity classifier in route-only mode.
MODEL_ROUTER_CLASSIFIER_MODEL_ID = os.environ.get(
    "MODEL_ROUTER_CLASSIFIER_MODEL_ID",
    "mistral.ministral-3-3b-instruct",
).strip()
MODEL_ROUTER_CLASSIFIER_REGION = os.environ.get(
    "MODEL_ROUTER_CLASSIFIER_REGION",
    os.environ.get("RUNTIME_REGION", "eu-west-1"),
).strip()
MODEL_ROUTER_CLASSIFIER_TIMEOUT_MS = int(
    os.environ.get("MODEL_ROUTER_CLASSIFIER_TIMEOUT_MS", "750")
)
MODEL_ROUTER_CLASSIFIER_INPUT_PER_MTOK = float(
    os.environ.get("MODEL_ROUTER_CLASSIFIER_INPUT_PER_MTOK", "0.12")
)
MODEL_ROUTER_CLASSIFIER_OUTPUT_PER_MTOK = float(
    os.environ.get("MODEL_ROUTER_CLASSIFIER_OUTPUT_PER_MTOK", "0.12")
)
MODEL_ROUTER_WARMUP_ENABLED = os.environ.get(
    "MODEL_ROUTER_WARMUP_ENABLED", "true"
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
# Both are optional JSON overrides; empty keeps the policy defaults documented in
# services/litellm_complexity_router.py and LiteLLM's own score boundaries.
COMPLEXITY_TIERS = os.environ.get("COMPLEXITY_TIERS", "").strip()
COMPLEXITY_TIER_BOUNDARIES = os.environ.get("COMPLEXITY_TIER_BOUNDARIES", "").strip()
# Optional pin so routing decisions stay attributable across LiteLLM upgrades.
MODEL_ROUTER_VERSION = os.environ.get("MODEL_ROUTER_VERSION", "").strip()
HARNESS_MODEL_ID = os.environ.get("HARNESS_MODEL_ID", BEDROCK_STRONG_MODEL_ID)
HARNESS_CHEAP_MODEL_ID = os.environ.get("HARNESS_CHEAP_MODEL_ID", BEDROCK_CHEAP_MODEL_ID)
HARNESS_MAX_TOKENS = int(
    os.environ.get("HARNESS_MAX_TOKENS", os.environ.get("BEDROCK_MAX_TOKENS", "4096"))
)
HARNESS_THINKING_BUDGET_TOKENS = int(
    os.environ.get(
        "HARNESS_THINKING_BUDGET_TOKENS",
        os.environ.get("THINKING_BUDGET_TOKENS", "1024"),
    )
)
