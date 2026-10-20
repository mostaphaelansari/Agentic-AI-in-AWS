"""Browser tool trace formatting."""

from __future__ import annotations

import ast

NOISY_TRACE_MARKERS = [
    "Raw code",
    "Main menu",
    "Jump to content",
    "move to sidebar",
    "Text content:",
    "Tool result was offloaded",
    "retrieve_offloaded_content",
    "s3://",
    '"reference"',
    "'reference'",
    "enable javascript",
    "detected unusual traffic",
]


def is_browser_tool(tool_name):
    return "browser" in (tool_name or "").lower()


def format_browser_action(tool_input):
    raw = tool_input.get("browser_input") if isinstance(tool_input, dict) else None
    if not raw:
        return None

    if isinstance(raw, str):
        try:
            raw = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return raw[:120]

    action = raw.get("action", {}) if isinstance(raw, dict) else {}
    action_type = action.get("type", "?")
    labels = {
        "init_session": lambda: f"init_session `{action.get('session_name', '?')}`",
        "navigate": lambda: f"navigate {action.get('url', '')}",
        "get_text": lambda: f"get_text `{action.get('selector', '?')}`",
        "get_html": lambda: f"get_html `{action.get('selector', '?')}`",
        "screenshot": lambda: "screenshot",
        "evaluate": lambda: f"evaluate `{action.get('script', '')[:80]}`",
        "click": lambda: f"click `{action.get('selector', '?')}`",
        "type": lambda: f"type into `{action.get('selector', '?')}`",
    }
    formatter = labels.get(action_type)
    return formatter() if formatter else action_type


def format_browser_result(result_str):
    if not result_str:
        return ""
    result_text = result_str.strip()
    try:
        parsed = ast.literal_eval(result_text)
        if isinstance(parsed, list) and parsed:
            item = parsed[0]
            if isinstance(item, dict):
                if "text" in item:
                    return str(item["text"]).strip()[:300]
                if isinstance(item.get("json"), dict):
                    values = list(item["json"].items())[:3]
                    return " ".join(f"{key}={value}" for key, value in values)[:300]
    except (ValueError, SyntaxError):
        pass
    return result_text[:300]


def should_hide_trace_text(text, tool_name):
    if not text:
        return False
    trace_text = str(text).strip()
    has_marker = any(marker.lower() in trace_text.lower() for marker in NOISY_TRACE_MARKERS)
    return has_marker or (is_browser_tool(tool_name) and len(trace_text) > 120)
