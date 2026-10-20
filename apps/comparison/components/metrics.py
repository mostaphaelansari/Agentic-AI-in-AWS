"""Streamlit metric and session presentation."""

from __future__ import annotations

import html
from urllib.parse import quote

from formatting.metrics import normalized_usage
from services.cost import inference_cost


def metric_value(value: int) -> str:
    return f"{value:,}" if value else "0"


def tool_call_count(state: dict) -> int:
    metrics = state.get("metrics", {})
    return int(metrics.get("browse_calls", 0) or metrics.get("tool_calls", 0) or 0)


def cycle_count(state: dict) -> int:
    metrics = state.get("metrics", {})
    return int(metrics.get("cycles", 0) or metrics.get("tool_calls", 0) or 0)


def cloudwatch_session_url(region: str, session_id: str) -> str:
    safe_region = quote(region, safe="")
    safe_session_id = quote(session_id, safe="")
    return (
        f"https://{safe_region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={safe_region}#gen-ai-observability/agent-core/session/{safe_session_id}"
    )


def cloudwatch_session_query(session_id: str) -> str:
    escaped_session_id = session_id.replace("/", r"\/")
    return (
        'SOURCE logGroups(namePrefix: ["/aws/bedrock-agentcore"], class: "STANDARD") START=-3600s END=0s\n'
        "| fields @timestamp, @log, @message\n"
        f"| filter @message like /{escaped_session_id}/\n"
        "| sort @timestamp desc\n"
        "| limit 100"
    )


def _chips_html(metrics: list[tuple[str, str]]) -> str:
    return "".join(
        f"<span><strong>{html.escape(label)}</strong>{html.escape(str(value))}</span>"
        for label, value in metrics
    )


def model_display_name(metrics: dict) -> str:
    """Return a concise model name while keeping tier/status visible."""
    model_id = str(metrics.get("model_id") or "")
    normalized = model_id.lower()
    if "claude-haiku-4-5" in normalized:
        name = "Claude Haiku 4.5"
    elif "claude-sonnet-4-6" in normalized:
        name = "Claude Sonnet 4.6"
    elif model_id:
        name = model_id
    else:
        return "Pending"

    tier = str(metrics.get("model_tier") or "").title()
    if metrics.get("model_metadata_status") == "legacy-runtime":
        tier = "Legacy Runtime"
    return f"{name} · {tier}" if tier else name


def router_latency_display(metrics: dict) -> str:
    """Format the shared pre-dispatch routing overhead."""
    value = metrics.get("model_router_latency_ms")
    if value is None:
        return "-"
    try:
        latency_ms = max(0.0, float(value))
    except (TypeError, ValueError):
        return "-"
    return f"{latency_ms:.3f} ms"


def classifier_display(metrics: dict) -> str:
    """Name the mechanism that produced the routing decision."""
    path = str(metrics.get("model_router_classifier_path") or "").strip().lower()
    model_id = str(metrics.get("model_router_classifier_model") or "").strip()
    classifier_tier = str(metrics.get("model_router_classifier_tier") or "").strip().upper()
    tier_suffix = (
        f" · {classifier_tier}"
        if classifier_tier in {"SIMPLE", "MEDIUM", "COMPLEX", "REASONING"}
        else ""
    )
    if path == "llm":
        name = "Ministral 3B" if "ministral-3-3b" in model_id.lower() else model_id
        return f"{name or 'LLM'} · LLM{tier_suffix}"
    if path == "heuristic_fallback":
        return f"Heuristic fallback{tier_suffix}"
    if path == "router_fallback":
        return "Router fallback"
    if path == "manual":
        return "Manual override"
    if path == "policy_override":
        return "Strong-model policy"

    # Compatibility for runs created before explicit classifier-path metadata.
    outcome = str(metrics.get("model_router_outcome") or "").strip().lower()
    if outcome == "fallback_heuristic_classifier":
        return f"Heuristic fallback{tier_suffix}"
    if outcome.startswith("fallback_"):
        return "Router fallback"
    if outcome == "override":
        return "Manual override"
    return "-"


def header_chips(state: dict) -> str:
    elapsed = state.get("elapsed")
    metrics = state.get("metrics", {})
    return _chips_html(
        [
            ("Model", model_display_name(metrics)),
            ("Region", state["config"]["region"]),
            ("Latency", f"{float(elapsed):.2f}s" if elapsed else "-"),
            ("Router latency", router_latency_display(metrics)),
            ("Classifier", classifier_display(metrics)),
            (
                "Tools",
                metric_value(tool_call_count(state)),
            ),
            (
                "Cycles",
                metric_value(cycle_count(state)),
            ),
        ]
    )


def token_block(state: dict) -> str:
    metrics = state.get("metrics", {})
    usage = normalized_usage(metrics.get("usage", {}))
    cost = inference_cost(usage, metrics.get("model_id"))
    tokens_available = state.get("tokens_available")
    if tokens_available is None:
        tokens_available = bool(state.get("metrics", {}).get("usage"))
    missing = "pending" if state.get("done") else "-"
    details = _chips_html(
        [
            ("In", metric_value(usage["fresh_input"]) if tokens_available else missing),
            ("Cache read", metric_value(usage["cache_read"]) if tokens_available else missing),
            ("Cache write", metric_value(usage["cache_write"]) if tokens_available else missing),
            ("Prompt", metric_value(usage["prompt_total"]) if tokens_available else missing),
            ("Out", metric_value(usage["output"]) if tokens_available else missing),
        ]
    )
    total = f"{usage['total']:,}" if tokens_available else missing
    cost_total = f"${cost['total_cost']:.6f}" if tokens_available else missing
    return (
        '<div class="token-block"><div class="token-total">'
        '<span class="token-total-group">'
        '<span class="token-total-label">Total tokens:</span>'
        f'<span class="token-total-num">{total}</span></span>'
        '<span class="token-total-group">'
        '<span class="token-total-label">Token cost:</span>'
        f'<span class="token-total-cost">{cost_total}</span></span></div>'
        f'<div class="token-detail">{details}</div></div>'
    )


def _query_details_html(queries: dict[str, str]) -> str:
    return "".join(
        '<details class="session-query">'
        f"<summary>{html.escape(label)}</summary>"
        f"<pre><code>{html.escape(query)}</code></pre>"
        "</details>"
        for label, query in queries.items()
    )


def session_block(state: dict) -> str:
    region = state["config"]["region"]
    session_id = state["session_id"]
    return (
        '<div class="session-id-block"><strong>Session ID</strong><br>'
        f"<code>{html.escape(session_id)}</code>"
        '<div class="session-links">'
        f'<a href="{html.escape(cloudwatch_session_url(region, session_id))}" '
        'target="_blank" rel="noopener noreferrer">'
        f"Open CloudWatch {html.escape(region)}</a></div>"
        f"{_query_details_html({'CloudWatch query': cloudwatch_session_query(session_id)})}"
        "</div>"
    )
