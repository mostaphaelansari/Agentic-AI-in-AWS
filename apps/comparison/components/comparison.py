"""Run and render Managed-versus-Custom comparisons."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable

import altair as alt
import streamlit as st
from backends.common import ensure_aws_credentials
from backends.errors import AWSAuthenticationError, public_error_message
from formatting.metrics import normalized_usage
from molecule_viewer import response_without_view_payloads
from services.builtin_usage import (
    BUILTIN_USAGE_AVAILABLE,
    BUILTIN_USAGE_ERROR,
    BUILTIN_USAGE_PENDING,
    fetch_builtin_usage_for_session,
)
from services.charts import chart_specs_for_state, response_without_chart_payloads
from services.cost import (
    builtin_tool_calls,
    builtin_tool_kinds,
    cache_savings,
    gateway_cost,
    gateway_operations,
    inference_cost,
    uncached_inference_cost,
)
from services.feedback import (
    FeedbackStoreError,
    feedback_default,
    feedback_rating,
    get_feedback,
    save_feedback,
)
from services.run_controller import (
    ACTIVE_RUN_KEY,
    RunController,
    drop_run,
    get_run,
    register_run,
)
from services.runtime_usage import (
    RUNTIME_USAGE_AVAILABLE,
    RUNTIME_USAGE_ERROR,
    RUNTIME_USAGE_PENDING,
    fetch_compute_usage_for_session,
)
from services.session_cost import current_ledger_id, record_run, update_runtime_usage

from components.history import (
    EDITING_PROMPT_KEY,
    add_history_entry,
    begin_prompt_edit,
    cancel_prompt_edit,
    prompt_edit_key,
    queue_prompt_resubmit,
)
from components.metrics import (
    classifier_display,
    cloudwatch_session_url,
    cycle_count,
    header_chips,
    metric_value,
    router_latency_display,
    session_block,
    token_block,
    tool_call_count,
)

METRIC_COMPARISONS = {
    "Latency": {
        "unit": "seconds",
        "value": lambda state: float(state.get("elapsed") or 0),
    },
    "Tools": {
        "unit": "calls",
        "value": lambda state: float(tool_call_count(state)),
    },
    "Cycles": {
        "unit": "cycles",
        "value": lambda state: float(cycle_count(state)),
    },
    "Input tokens": {
        "unit": "tokens",
        "value": lambda state: float(
            normalized_usage(state.get("metrics", {}).get("usage", {}))["fresh_input"]
        ),
    },
    "Output tokens": {
        "unit": "tokens",
        "value": lambda state: float(
            normalized_usage(state.get("metrics", {}).get("usage", {}))["output"]
        ),
    },
    "Total tokens": {
        "unit": "tokens",
        "value": lambda state: float(
            normalized_usage(state.get("metrics", {}).get("usage", {}))["total"]
        ),
    },
    "Token cost": {
        "unit": "USD",
        "value": lambda state: inference_cost(
            normalized_usage(state.get("metrics", {}).get("usage", {})),
            state.get("metrics", {}).get("model_id"),
        )["total_cost"],
    },
}
AGENT_DISPLAY_ORDER = ("Managed agent", "Custom agent")
TOOL_FRIENDLY_SUMMARY = {
    "searchLocation": "Looked up a location",
    "getForecast": "Checked the weather forecast",
    "search_flights": "Searched for flights",
    "searchFlights": "Searched for flights",
    "search_hotels": "Compared accommodation options",
    "searchHotels": "Compared accommodation options",
    "search_booking": "Compared accommodation options",
    "compare_cities": "Compared destinations",
    "compareCities": "Compared destinations",
    "web_research": "Researched the web",
    "webSearch": "Researched the web",
    "map_researcher": "Explored the map",
    "searchMapPlaces": "Explored the map",
    "findNearbyPlaces": "Explored nearby places",
    "code_interpreter": "Analyzed the data",
    "python": "Analyzed the data",
}
TOOL_ICON = "\U0001f6e0️"
CURRENCY_DOLLAR = re.compile(r"(?<!\\)\$(?=\s*\d)")


def _active_run_key(page_scope: str = "comparison") -> str:
    """Avoid importing reload-sensitive helpers from the controller module."""
    return ACTIVE_RUN_KEY if page_scope == "comparison" else f"{ACTIVE_RUN_KEY}:{page_scope}"


def _ordered_agent_items(states: dict[str, dict]) -> list[tuple[str, dict]]:
    ordered = [(name, states[name]) for name in AGENT_DISPLAY_ORDER if name in states]
    ordered.extend(
        (name, state) for name, state in states.items() if name not in AGENT_DISPLAY_ORDER
    )
    return ordered


def _format_chart_value(value: float, unit: str) -> str:
    if unit == "seconds":
        return f"{value:.2f}s" if value else "0"
    if unit == "USD":
        return f"${value:.4f}" if value else "$0.0000"
    return f"{value:,.0f}" if value else "0"


def _metric_chart(rows: list[tuple[str, float]], selected_metric: str, unit: str) -> alt.Chart:
    chart_rows = [
        {
            "Agent": arch_name,
            selected_metric: value,
            "Value": _format_chart_value(value, unit),
        }
        for arch_name, value in rows
    ]
    return (
        alt.Chart(alt.Data(values=chart_rows))
        .mark_bar()
        .encode(
            x=alt.X("Agent:N", sort=[arch_name for arch_name, _ in rows], title="Agent"),
            y=alt.Y(f"{selected_metric}:Q", title=unit),
            tooltip=[
                alt.Tooltip("Agent:N", title="Agent"),
                alt.Tooltip("Value:N", title=selected_metric),
            ],
        )
    )


def render_metric_comparison(states: dict[str, dict], key_suffix: str = "current") -> None:
    if len(states) < 2:
        return

    st.markdown("### Metrics comparison")
    selected_metric = st.selectbox(
        "Metric",
        list(METRIC_COMPARISONS),
        index=0,
        key=f"metric_comparison_{key_suffix}",
    )
    metric = METRIC_COMPARISONS[selected_metric]
    unit = metric["unit"]
    rows = [
        (arch_name, metric["value"](state)) for arch_name, state in _ordered_agent_items(states)
    ]
    st.altair_chart(
        _metric_chart(rows, selected_metric, unit),
        width="stretch",
    )
    st.table(
        {
            "Agent": [arch_name for arch_name, _ in rows],
            selected_metric: [_format_chart_value(value, unit) for _, value in rows],
        }
    )


def _status_class(state: dict) -> str:
    if state["errors"]:
        return "error"
    if state["done"]:
        return "done"
    return "waiting" if state["status"] == "Waiting" else "running"


def _safe(value) -> str:
    return html.escape(str(value), quote=True)


def _json_preview(value, limit: int = 520) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        rendered = str(value)
    return rendered if len(rendered) <= limit else rendered[:limit].rstrip() + "\n..."


def response_markdown(answer: str, fallback: str) -> str:
    """Prepare agent prose for Streamlit without triggering inline LaTeX."""
    prose = response_without_view_payloads(response_without_chart_payloads(answer))
    return CURRENCY_DOLLAR.sub(r"\\$", prose) if prose else fallback


def response_fallback(state: dict) -> str:
    if state.get("status") == "Stopped":
        return "Stopped before a response was received."
    if state.get("errors"):
        return "No response was received."
    return "Waiting for response..."


def _friendly_tool_summary(tool_name: str, *, running: bool = False) -> str:
    normalized_name = tool_name.rsplit("___", 1)[-1]
    summary = TOOL_FRIENDLY_SUMMARY.get(normalized_name)
    if summary:
        return summary
    readable_name = normalized_name.replace("_", " ")
    return f"Running {readable_name}…" if running else f"Ran {readable_name}"


def _tool_failed(tool: dict) -> bool:
    result = str(tool.get("result") or "").strip().lower()
    return result.startswith("error") or any(
        marker in result for marker in ('"status": "error"', "'status': 'error'")
    )


def _render_tools(tools: list[dict]) -> None:
    if not tools:
        st.markdown(
            '<div class="compare-empty-state">No tool calls yet.</div>',
            unsafe_allow_html=True,
        )
        return
    for index, tool in enumerate(tools, start=1):
        done = tool.get("result") is not None
        failed = done and _tool_failed(tool)
        status_class = "failed" if failed else ("done" if done else "")
        pill_class = "error" if failed else ("done" if done else "running")
        status_label = "Failed" if failed else ("Completed" if done else "Running")
        st.markdown(
            f"""
            <div class="compare-tool {status_class}">
              <div class="compare-tool-head">
                <span class="compare-tool-summary">
                  <span class="compare-tool-icon">{TOOL_ICON}</span>
                  {index}. {_safe(_friendly_tool_summary(tool.get("name", "tool")))}
                </span>
                <span class="compare-tool-pill {pill_class}">{status_label}</span>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        with st.expander("Technical details", expanded=False):
            st.caption(f"Tool name: `{tool.get('name', 'tool')}`")
            st.markdown("**Arguments**")
            st.code(_json_preview(tool.get("input") or {}, limit=420), language="json")
            if done:
                result_text = _json_preview(tool.get("result") or "", limit=900)
                st.markdown("**Result**")
                st.code(result_text, language="json")
                if result_text.endswith("..."):
                    st.caption("Output truncated for display.")


def _current_activity(state: dict) -> str | None:
    if state["done"]:
        return None
    running_tool = next(
        (tool for tool in reversed(state.get("tools", [])) if tool.get("result") is None),
        None,
    )
    if running_tool:
        return _friendly_tool_summary(running_tool.get("name", "tool"), running=True)
    messages = {
        "Routing": "Selecting the best-value model…",
        "Waiting": "Waiting to start…",
        "Thinking": "Reasoning through the request…",
        "Browsing": "Using tools…",
        "Responding": "Preparing the response…",
    }
    return messages.get(state["status"], "Working on the request…")


def _render_answer_feedback(entry: dict, state: dict, key_suffix: str) -> None:
    answer = response_without_view_payloads(
        response_without_chart_payloads(state.get("answer", ""))
    ).strip()
    if not answer:
        return
    ledger_id = entry.get("ledger_id") or current_ledger_id()
    try:
        existing = get_feedback(ledger_id, entry["id"], state["arch_name"])
    except FeedbackStoreError as exc:
        st.caption(str(exc))
        return

    st.caption("Was this answer useful?")
    selected = st.feedback(
        "thumbs",
        key=f"answer_feedback_{key_suffix}_{state['arch_name']}",
        default=feedback_default(existing),
    )
    if selected is None:
        return
    rating = feedback_rating(selected)
    if not existing or existing["rating"] != rating:
        try:
            save_feedback(
                ledger_id=ledger_id,
                run_id=entry["id"],
                architecture=state["arch_name"],
                rating=rating,
                prompt=entry["prompt"],
                answer=answer,
                session_id=state.get("session_id", ""),
                parent_id=entry.get("parent_id"),
                branch_source_id=entry.get("branch_source_id"),
            )
        except FeedbackStoreError as exc:
            st.error(str(exc))
            return
    st.caption("Feedback recorded.")


def render_column(
    placeholders: dict,
    state: dict,
    *,
    feedback_entry: dict | None = None,
    feedback_key: str = "",
) -> None:
    status = "Error" if state["errors"] else state["status"]
    placeholders["status"].markdown(
        f"""
        <div class="agent-title">
          <h2 class="agent-name">
            <span class="agent-dot {"custom" if state["arch_name"] == "Custom agent" else ""}"></span>
            {html.escape(state["arch_name"])}
          </h2>
          <span class="status-pill {_status_class(state)}">{html.escape(status)}</span>
        </div>
        <div class="metric-chips">{header_chips(state)}</div>
        {token_block(state)}
        """,
        unsafe_allow_html=True,
    )
    if state["errors"]:
        placeholders["error"].error("\n".join(state["errors"]))
    else:
        placeholders["error"].empty()

    activity = _current_activity(state)
    if activity:
        placeholders["activity"].markdown(
            f"""
            <div class="compare-activity" role="status" aria-live="polite">
              <span class="compare-spinner" aria-hidden="true"></span>
              <span>{_safe(activity)}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        placeholders["activity"].empty()

    with placeholders["answer"].container():
        st.markdown('<p class="agent-answer-note">Response</p>', unsafe_allow_html=True)
        with st.container(border=True, height=420):
            st.markdown(response_markdown(state["answer"], response_fallback(state)))
    if feedback_entry is not None:
        with placeholders["feedback"].container():
            _render_answer_feedback(feedback_entry, state, feedback_key)
    else:
        placeholders["feedback"].empty()
    chart_specs = chart_specs_for_state(state)
    if chart_specs:
        with placeholders["visuals"].container():
            st.markdown("**Interactive chart**")
            for chart_spec in chart_specs:
                st.vega_lite_chart(chart_spec, width="stretch")
    else:
        placeholders["visuals"].empty()
    reasoning = state.get("reasoning", "").strip()
    if reasoning:
        with placeholders["reasoning"].expander("Reasoning", expanded=False):
            st.code(reasoning, language="text")
    else:
        placeholders["reasoning"].empty()
    has_activity = bool(state.get("tools") or state["trace"].strip()) or not state["done"]
    with placeholders["trace"].expander("Tool activity", expanded=has_activity):
        _render_tools(state.get("tools", []))
        if state["trace"].strip():
            with st.expander("Technical trace", expanded=False):
                st.code(state["trace"].strip(), language="text")

    placeholders["session"].markdown(
        session_block(state),
        unsafe_allow_html=True,
    )


def _completed_at(state: dict) -> float | None:
    started_at = state.get("started_at")
    elapsed = state.get("elapsed")
    if started_at and elapsed:
        return float(started_at) + float(elapsed)
    return None


def _refresh_compute_usage(state: dict) -> None:
    state["runtime_usage"] = fetch_compute_usage_for_session(
        state["config"],
        state["session_id"],
        state.get("started_at"),
        _completed_at(state),
    )
    state["builtin_usage"] = fetch_builtin_usage_for_session(
        state["config"],
        state["session_id"],
        builtin_tool_kinds(state.get("metrics", {})),
        state.get("started_at"),
        _completed_at(state),
    )
    update_runtime_usage(state)


def render_run_cost(state: dict, key_suffix: str) -> None:
    """Per-run cost from measured usage only - tokens and tool calls measured
    by the app."""
    st.markdown("**Run cost** (measured usage x list rates)")
    is_runtime = state.get("config", {}).get("service") in {"runtime", "harness"}
    if is_runtime and st.button(
        "Refresh compute cost",
        key=f"refresh_runtime_cost_{key_suffix}",
        help="Fetch delayed Runtime, Browser, and Code Interpreter USAGE_LOGS.",
    ):
        _refresh_compute_usage(state)

    usage = normalized_usage(state.get("metrics", {}).get("usage", {}))
    metrics = state.get("metrics", {})
    tokens_available = state.get("tokens_available")
    if tokens_available is None:
        tokens_available = bool(metrics.get("usage"))
    model_id = metrics.get("model_id")
    inference = inference_cost(usage, model_id)
    uncached_inference = uncached_inference_cost(usage, model_id)
    savings = cache_savings(usage, model_id)
    cache_delta = f"-${savings:.6f}" if savings >= 0 else f"+${abs(savings):.6f}"
    operations = gateway_operations(metrics)
    builtin_calls = builtin_tool_calls(metrics)
    gateway = gateway_cost(operations)
    meters = [
        "Token - input",
        "Token - cache read",
        "Token - cache write",
        "Token - output",
        "Token total with cache",
        "Token total without cache",
        "Prompt caching delta",
        "Gateway - MCP operations",
    ]
    usage_values = [
        f"{usage['fresh_input']:,} tokens",
        f"{usage['cache_read']:,} tokens",
        f"{usage['cache_write']:,} tokens",
        f"{usage['output']:,} tokens",
        "—",
        f"{usage['prompt_total']:,} input-side tokens + {usage['output']:,} output tokens",
        "saved" if savings >= 0 else "extra cost",
        f"{operations} ops",
    ]
    costs = [
        f"${inference['input_cost']:.6f}",
        f"${inference['cache_read_cost']:.6f}",
        f"${inference['cache_write_cost']:.6f}",
        f"${inference['output_cost']:.6f}",
        f"${inference['total_cost']:.6f}",
        f"${uncached_inference:.6f}",
        cache_delta,
        f"${gateway:.6f}",
    ]
    if not tokens_available:
        usage_values[:7] = ["pending"] * 7
        costs[:7] = ["pending"] * 7
    token_gateway_total = (inference["total_cost"] if tokens_available else 0.0) + gateway
    builtin_usage = state.get("builtin_usage", {})
    builtin_status = builtin_usage.get("status")
    builtin_total = 0.0
    if builtin_calls:
        if builtin_status == BUILTIN_USAGE_AVAILABLE:
            builtin_total = float(builtin_usage.get("builtin_cost", 0.0) or 0.0)
            meters.extend(
                [
                    "Browser / Code Interpreter - vCPU",
                    "Browser / Code Interpreter - memory",
                    "Browser / Code Interpreter total",
                ]
            )
            usage_values.extend(
                [
                    f"{builtin_usage['vcpu_hours']:.12f} vCPU-hours",
                    f"{builtin_usage['memory_gb_hours']:.12f} GB-hours",
                    f"{builtin_calls} calls",
                ]
            )
            costs.extend(
                [
                    f"${builtin_usage['cpu_cost']:.9f}",
                    f"${builtin_usage['memory_cost']:.9f}",
                    f"${builtin_total:.9f}",
                ]
            )
        else:
            status = (
                BUILTIN_USAGE_ERROR
                if builtin_status == BUILTIN_USAGE_ERROR
                else BUILTIN_USAGE_PENDING
            )
            meters.append("Browser / Code Interpreter total")
            usage_values.append(f"{builtin_calls} calls; {status}")
            costs.append("pending")
    runtime_usage = state.get("runtime_usage", {})
    runtime_status = runtime_usage.get("status")
    if is_runtime:
        if runtime_status == RUNTIME_USAGE_AVAILABLE:
            meters.extend(["Runtime - vCPU", "Runtime - memory", "Runtime total"])
            usage_values.extend(
                [
                    f"{runtime_usage['vcpu_hours']:.12f} vCPU-hours",
                    f"{runtime_usage['memory_gb_hours']:.12f} GB-hours",
                    f"{runtime_usage['elapsed_seconds']:.3f}s elapsed",
                ]
            )
            costs.extend(
                [
                    f"${runtime_usage['cpu_cost']:.9f}",
                    f"${runtime_usage['memory_cost']:.9f}",
                    f"${runtime_usage['runtime_cost']:.9f}",
                ]
            )
            meters.append(
                "Total (token + gateway + compute)"
                if tokens_available
                else "Current measured subtotal"
            )
            usage_values.append("—")
            costs.append(
                f"${token_gateway_total + builtin_total + runtime_usage['runtime_cost']:.6f}"
            )
        else:
            status = "error" if runtime_status == RUNTIME_USAGE_ERROR else RUNTIME_USAGE_PENDING
            meters.extend(["Runtime total", "Current measured subtotal"])
            usage_values.extend([status, "—"])
            costs.extend(["pending", f"${token_gateway_total + builtin_total:.6f}"])
    else:
        meters.extend(["Runtime total", "Current measured subtotal"])
        usage_values.extend(["not applicable", "—"])
        costs.extend(["—", f"${token_gateway_total + builtin_total:.6f}"])
    st.table(
        {
            "Meter": meters,
            "Usage": usage_values,
            "Cost": costs,
        }
    )
    if usage["cache_read"] or usage["cache_write"]:
        verdict = "saved" if savings >= 0 else "cost an extra"
        st.caption(
            f"Prompt caching {verdict} ${abs(savings):.4f} this run vs sending all input fresh."
        )
    if not tokens_available:
        st.caption("Token usage is pending and excluded from the measured subtotal.")
    if is_runtime and runtime_status == RUNTIME_USAGE_ERROR:
        st.caption(f"Runtime usage lookup failed: {runtime_usage.get('error', 'unknown error')}")
    if builtin_calls and builtin_status == BUILTIN_USAGE_ERROR:
        st.caption(
            "Browser / Code Interpreter usage lookup failed: "
            f"{builtin_usage.get('error', 'USAGE_LOGS delivery is unavailable')}"
        )


def _queue_edited_prompt(entry: dict) -> None:
    queue_prompt_resubmit(
        entry,
        st.session_state.get(prompt_edit_key(entry["id"]), entry["prompt"]),
    )


def _render_editable_prompt(entry: dict, key_suffix: str) -> None:
    page_scope = str(entry.get("options", {}).get("_page_scope") or "comparison")
    editing_key = (
        EDITING_PROMPT_KEY
        if page_scope == "comparison"
        else f"{EDITING_PROMPT_KEY}:{page_scope}"
    )
    editing = st.session_state.get(editing_key) == entry["id"]
    with st.container(border=True):
        st.markdown("**Prompt**")
        if editing:
            revised_prompt = st.text_area(
                "Edit prompt",
                key=prompt_edit_key(entry["id"]),
                height=110,
                help=(
                    "Resubmitting creates a sibling branch. Each agent receives only "
                    "the bounded memory from turns before this one."
                ),
            )
            resubmit_column, cancel_column = st.columns([1, 1])
            with resubmit_column:
                st.button(
                    "Resubmit",
                    key=f"resubmit_{key_suffix}",
                    type="primary",
                    disabled=not revised_prompt.strip(),
                    on_click=_queue_edited_prompt,
                    args=(entry,),
                    width="stretch",
                )
            with cancel_column:
                st.button(
                    "Cancel",
                    key=f"cancel_edit_{key_suffix}",
                    on_click=cancel_prompt_edit,
                    args=(entry["id"], page_scope),
                    width="stretch",
                )
            if entry.get("options", {}).get("uploaded_file"):
                st.caption(
                    f"Reattach `{entry['options']['uploaded_file']}` in the prompt area "
                    "before the revision can run."
                )
        else:
            st.markdown(entry["prompt"])
            if source_id := entry.get("options", {}).get("resubmitted_from"):
                st.caption(f"Branched by editing run `{source_id}`.")
            st.button(
                "Edit and resubmit",
                key=f"edit_prompt_{key_suffix}",
                icon=":material/edit:",
                on_click=begin_prompt_edit,
                args=(entry,),
            )


def _render_comparison_entry(entry: dict, heading: str, metric_key_suffix: str) -> None:
    st.markdown(f"## {heading}")
    reasoning = entry["options"].get("reasoning_effort") or (
        "legacy" if entry["options"].get("enable_thinking") else "off"
    )
    st.caption(
        f"{entry['created_at']} | "
        f"Reasoning: {reasoning} | "
        f"Prompt caching: {'on' if entry['options'].get('enable_caching') else 'off'} | "
        f"Model: {entry['options'].get('model_tier', 'strong')} "
        f"({entry['options'].get('model_route_reason', 'default model')})"
    )
    _render_editable_prompt(entry, key_suffix=metric_key_suffix)

    ordered_states = _ordered_agent_items(entry["states"])
    tabs = st.tabs([arch_name for arch_name, _ in ordered_states])
    for tab, (arch_name, state) in zip(tabs, ordered_states):
        usage = normalized_usage(state.get("metrics", {}).get("usage", {}))
        metrics = state.get("metrics", {})
        with tab:
            st.markdown(f"### {arch_name}")
            st.markdown(token_block(state), unsafe_allow_html=True)
            st.table(
                {
                    "Metric": [
                        "Status",
                        "Model",
                        "Route reason",
                        "Classifier",
                        "Router latency",
                        "Region",
                        "Latency",
                        "Tools",
                        "Cycles",
                        "Input tokens",
                        "Cache read tokens",
                        "Cache write tokens",
                        "Prompt tokens",
                        "Output tokens",
                        "Total tokens",
                        "Session ID",
                    ],
                    "Value": [
                        state["status"],
                        metrics.get("model_id", entry["options"].get("model_id", "-")),
                        metrics.get(
                            "model_route_reason",
                            entry["options"].get("model_route_reason", "-"),
                        ),
                        classifier_display(
                            {
                                "model_router_classifier_model": metrics.get(
                                    "model_router_classifier_model",
                                    entry["options"].get("model_router_classifier_model"),
                                ),
                                "model_router_classifier_path": metrics.get(
                                    "model_router_classifier_path",
                                    entry["options"].get("model_router_classifier_path"),
                                ),
                                "model_router_classifier_tier": metrics.get(
                                    "model_router_classifier_tier",
                                    entry["options"].get("model_router_classifier_tier"),
                                ),
                                "model_router_outcome": metrics.get(
                                    "model_router_outcome",
                                    entry["options"].get("model_router_outcome"),
                                ),
                            }
                        ),
                        router_latency_display(
                            {
                                "model_router_latency_ms": metrics.get(
                                    "model_router_latency_ms",
                                    entry["options"].get("model_router_latency_ms"),
                                )
                            }
                        ),
                        state["config"]["region"],
                        f"{state['elapsed']}s" if state.get("elapsed") else "-",
                        metric_value(tool_call_count(state)),
                        metric_value(cycle_count(state)),
                        metric_value(usage["fresh_input"]),
                        metric_value(usage["cache_read"]),
                        metric_value(usage["cache_write"]),
                        metric_value(usage["prompt_total"]),
                        metric_value(usage["output"]),
                        metric_value(usage["total"]),
                        state["session_id"],
                    ],
                }
            )
            if metrics.get("usage"):
                with st.expander("Raw usage payload", expanded=False):
                    st.json(metrics["usage"])
            render_run_cost(state, key_suffix=f"{entry['id']}_{state['arch_name']}")
            with st.container(border=True):
                st.markdown("**Response**")
                st.markdown(response_markdown(state["answer"], "No response captured."))
            _render_answer_feedback(entry, state, f"saved_{metric_key_suffix}")
            chart_specs = chart_specs_for_state(state)
            if chart_specs:
                st.markdown("**Interactive chart**")
                for chart_spec in chart_specs:
                    st.vega_lite_chart(chart_spec, width="stretch")
            reasoning = state.get("reasoning", "").strip()
            if reasoning:
                with st.expander("Reasoning", expanded=False):
                    st.code(reasoning, language="text")
            with st.expander("Tool activity", expanded=True):
                _render_tools(state.get("tools", []))
                if state["trace"].strip():
                    with st.expander("Technical trace", expanded=False):
                        st.code(state["trace"].strip(), language="text")
            st.link_button(
                f"Open CloudWatch {state['config']['region']}",
                cloudwatch_session_url(state["config"]["region"], state["session_id"]),
            )
    render_metric_comparison(entry["states"], key_suffix=metric_key_suffix)


def render_current_comparison(entry: dict) -> None:
    states = entry["states"]
    _render_editable_prompt(entry, key_suffix=f"current_{entry['id']}")
    ordered_states = _ordered_agent_items(states)
    columns = st.columns(len(ordered_states), gap="medium")

    for column, (arch_name, state) in zip(columns, ordered_states):
        with column:
            with st.container(border=True):
                placeholders = {
                    "status": st.empty(),
                    "activity": st.empty(),
                    "error": st.empty(),
                    "answer": st.empty(),
                    "feedback": st.empty(),
                    "visuals": st.empty(),
                    "reasoning": st.empty(),
                    "trace": st.empty(),
                    "session": st.empty(),
                }
            render_column(
                placeholders,
                state,
                feedback_entry=entry,
                feedback_key=f"current_{entry['id']}",
            )

    st.markdown("### Run cost")
    cost_tabs = st.tabs([arch_name for arch_name, _ in ordered_states])
    for tab, (arch_name, state) in zip(cost_tabs, ordered_states):
        with tab:
            render_run_cost(state, key_suffix=f"current_{entry['id']}_{arch_name}")

    render_metric_comparison(states)


def render_saved_comparison(entry: dict) -> None:
    _render_comparison_entry(entry, "Branch details", entry["id"])


def run_comparison(
    prompt: str,
    options: dict,
    *,
    display_prompt: str | None = None,
    agent_prompts: dict[str, str] | None = None,
    parent_id: str | None = None,
    branch_source_id: str | None = None,
    cleanup: Callable[[], None] | None = None,
    architectures: tuple[str, ...] | None = None,
    page_scope: str = "comparison",
) -> RunController | None:
    try:
        ensure_aws_credentials()
    except AWSAuthenticationError as exc:
        if cleanup is not None:
            cleanup()
        st.error(public_error_message(exc))
        return None

    try:
        controller = RunController(
            prompt,
            options,
            display_prompt=display_prompt,
            agent_prompts=agent_prompts,
            parent_id=parent_id,
            branch_source_id=branch_source_id,
            ledger_id=current_ledger_id(),
            cleanup=cleanup,
            architectures=architectures,
            page_scope=page_scope,
        )
    except TypeError as exc:
        # The app entrypoint can reload before this service module when Streamlit
        # is already running. Turn that one-process version mismatch into a clear
        # restart instruction instead of an import/constructor traceback.
        if "architectures" not in str(exc) and "page_scope" not in str(exc):
            raise
        if cleanup is not None:
            cleanup()
        st.error(
            "The UI code was updated while Streamlit was running. Restart the "
            "Streamlit process once, then retry this run."
        )
        return None
    controller = register_run(controller)
    try:
        controller.start()
    except Exception:
        drop_run(controller.run_id)
        controller.cleanup()
        raise
    st.session_state[_active_run_key(page_scope)] = controller.run_id
    return controller


def _stop_run(run_id: str) -> None:
    if controller := get_run(run_id):
        controller.stop()


def _render_live_states(states: dict[str, dict]) -> None:
    ordered_states = _ordered_agent_items(states)
    columns = st.columns(len(ordered_states), gap="medium")
    for column, (arch_name, state) in zip(columns, ordered_states):
        with column:
            with st.container(border=True):
                placeholders = {
                    "status": st.empty(),
                    "activity": st.empty(),
                    "error": st.empty(),
                    "answer": st.empty(),
                    "feedback": st.empty(),
                    "visuals": st.empty(),
                    "reasoning": st.empty(),
                    "trace": st.empty(),
                    "session": st.empty(),
                }
            render_column(placeholders, state)


@st.fragment(run_every=0.3)
def render_live_comparison(run_id: str) -> None:
    """Render one short snapshot so Stop callbacks are never queued behind AWS."""
    controller = get_run(run_id)
    if controller is None:
        return

    controller.drain()
    if controller.ready_to_finalize():
        states = controller.finalize()
    else:
        states = controller.snapshot()

    running = not controller.finalized
    st.button(
        "Stop comparison" if len(states) > 1 else "Stop run",
        key=f"stop_{run_id}",
        type="secondary",
        disabled=not running,
        on_click=_stop_run,
        args=(run_id,),
        help=(
            "Terminates the Custom Runtime session. Managed Harness cancellation "
            "closes its client stream only."
            if len(states) > 1
            else "Terminates the Custom agent Runtime session."
        ),
    )
    _render_live_states(states)

    if running:
        return

    def _commit(frozen_states: dict[str, dict]) -> None:
        add_history_entry(
            controller.prompt,
            controller.options,
            frozen_states,
            entry_id=controller.run_id,
            parent_id=controller.parent_id,
            branch_source_id=controller.branch_source_id,
            ledger_id=controller.ledger_id,
        )
        record_run(frozen_states, run_id=controller.run_id)

    if controller.commit_once(_commit):
        run_key = _active_run_key(getattr(controller, "page_scope", "comparison"))
        if st.session_state.get(run_key) == run_id:
            st.session_state.pop(run_key, None)
        drop_run(run_id)
        st.rerun()
