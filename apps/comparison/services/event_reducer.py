"""Apply normalized backend events to comparison state."""

from __future__ import annotations

import json

from formatting.browser import (
    format_browser_action,
    format_browser_result,
    is_browser_tool,
    should_hide_trace_text,
)


def _append_tool_start(state: dict, event: dict) -> None:
    tool_name = event.get("name", "tool")
    tool_id = event.get("tool_use_id") or event.get("toolUseId")
    tool_input = event.get("input", {})
    label = format_browser_action(tool_input) if is_browser_tool(tool_name) else None
    if not label and isinstance(tool_input, dict):
        label = tool_input.get("summary")
    if not label:
        label = json.dumps(tool_input, default=str)[:120]

    state["status"] = "Browsing"
    state["phase"] = "in_tools"
    metrics = state.setdefault("metrics", {})
    metrics["tool_calls"] = int(metrics.get("tool_calls", 0) or 0) + 1
    tool_names = metrics.setdefault("tool_names", [])
    if isinstance(tool_names, list):
        tool_names.append(tool_name)
    if state["pending_text"]:
        state["trace"] += state["pending_text"]
        state["pending_text"] = ""
    if not should_hide_trace_text(label, tool_name):
        state["trace"] += f"\n-> {label}\n"
    tools = state.setdefault("tools", [])
    for tool in tools:
        if tool_id and tool.get("id") == tool_id:
            tool["input"] = tool_input or tool.get("input", {})
            return
    tools.append(
        {
            "id": tool_id,
            "name": tool_name,
            "input": tool_input,
            "result": None,
            "summary": event.get("summary"),
        }
    )


def _append_tool_end(state: dict, event: dict) -> None:
    tool_name = event.get("name", "tool")
    tool_id = event.get("tool_use_id") or event.get("toolUseId")
    raw_result = event.get("result", "")
    result = event.get("summary")
    if not result:
        result = (
            format_browser_result(raw_result) if is_browser_tool(tool_name) else raw_result[:300]
        )
    if should_hide_trace_text(result, tool_name):
        result = ""
    if result:
        state["trace"] += f"   {result}\n"
    tools = state.setdefault("tools", [])
    matched = False
    for tool in reversed(tools):
        same_id = bool(tool_id and tool.get("id") == tool_id)
        same_running_name = (
            not tool_id and tool.get("name") == tool_name and tool.get("result") is None
        )
        if same_id or same_running_name:
            tool["result"] = raw_result
            tool["summary"] = event.get("summary") or tool.get("summary")
            matched = True
            break
    if not matched:
        tools.append(
            {
                "id": tool_id,
                "name": tool_name,
                "input": {},
                "result": raw_result,
                "summary": event.get("summary"),
            }
        )
    state["status"] = "Responding"
    state["phase"] = "post_tool"


def finish_state(state: dict, status: str | None = None) -> None:
    """Move the last streamed text into the answer and make the state terminal."""
    if state["pending_text"]:
        state["answer"] += state["pending_text"]
        state["pending_text"] = ""
    state["done"] = True
    if status is not None:
        state["status"] = status
    elif not state["errors"]:
        state["status"] = "Done"


def apply_event(state: dict, item: tuple) -> None:
    item_type, data = item
    if item_type == "elapsed":
        state["elapsed"] = data
        return
    if item_type == "done":
        finish_state(state)
        return

    event = data
    event_type = event.get("type", "")
    if event.get("model_id"):
        model_metadata = {
            key: event[key]
            for key in (
                "model_id",
                "model_tier",
                "model_route_reason",
                "model_metadata_status",
                "requested_model_tier",
            )
            if key in event
        }
        state["metrics"] = {**state.get("metrics", {}), **model_metadata}
    if event_type == "reasoning":
        content = event.get("content", "")
        if content:
            state["status"] = "Thinking"
            state.setdefault("reasoning", "")
            state["reasoning"] += content
    elif event_type == "text":
        content = event.get("content", "")
        if content:
            state["status"] = "Responding"
            # A model may narrate a retry immediately before another tool call.
            # Buffer all text until the next event reveals whether it is an
            # intermediate plan (tool_start) or the final response (done).
            state["pending_text"] += content
    elif event_type == "tool_start":
        _append_tool_start(state, event)
    elif event_type == "tool_end":
        _append_tool_end(state, event)
    elif event_type == "metrics":
        state["metrics"] = {**state.get("metrics", {}), **event}
        state["tokens_available"] = True
    elif event_type == "error":
        state["status"] = "Error"
        state["errors"].append(event.get("content", "Unknown error"))
