"""Multi-workspace Streamlit app for the open-source custom agent.

Travel and protein analysis use the same backend service but keep independent
prompt controls, sessions, uploads, and run history in the UI.

Run:  uv run streamlit run opensource/ui/app.py
"""

from __future__ import annotations

import base64
import concurrent.futures
import html
import json
import os
import queue
import re
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import streamlit as st
import streamlit.components.v1 as components

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
COMPARISON_APP_DIR = ROOT / "apps" / "comparison"
if str(COMPARISON_APP_DIR) not in sys.path:
    sys.path.insert(0, str(COMPARISON_APP_DIR))

from molecule_viewer import (  # noqa: E402
    MOLSTAR_VERSION,
    agent_structure_view,
    atom_record_count,
    compose_view_layers,
    filter_mmcif,
    layers_description,
    response_without_view_payloads,
)
from molstar_component import (  # noqa: E402
    render_molstar_component,
    selection_description,
    selection_prompt_context,
    structure_fingerprint,
)
from structure_analysis import (  # noqa: E402
    StructureAnalysisError,
    measure,
    metric_description,
    metric_specs_for_state,
    response_without_metric_payloads,
)
from structure_notebook import (  # noqa: E402
    DEFAULT_NOTEBOOK_CELL,
    NotebookCellError,
    apply_notebook_command,
    parse_notebook_cell,
)

DOCS_DIR = ROOT / "docs"
STYLE_PATH = Path(__file__).resolve().parent / "assets" / "styles.css"

TRAVEL_WORKSPACE = "Travel Agent"
PROTEIN_WORKSPACE = "Protein Analysis"
CATALOG_VIEW = "Agent Catalog"
DOCUMENTATION_VIEW = "Documentation"
WORKSPACE_KEY = "opensource_workspace"
NAVIGATION_KEY = "opensource_navigation"
PENDING_NAVIGATION_KEY = "opensource_pending_navigation"
SELECTION_STATE_PREFIX = "opensource_structure_selection_"
VIEW_LAYERS_STATE_PREFIX = "opensource_structure_layers_"
NOTEBOOK_CODE_STATE_PREFIX = "opensource_structure_notebook_code_"
NOTEBOOK_OUTPUT_STATE_PREFIX = "opensource_structure_notebook_output_"
MEASUREMENT_STATE_PREFIX = "opensource_structure_measurement_"
AGENT_NAME = "Travel Planner Agent"
AGENT_URL = os.environ.get("OPENSOURCE_AGENT_URL", "http://localhost:8080").rstrip("/")
# Fallback user_id for MLflow traces (mlflow.trace.user) when login is
# disabled; with a users file configured, the signed-in username wins.
USER_ID = os.environ.get("OPENSOURCE_USER_ID", "local-user")
# Shared secret for the agent service; must match the service's API_KEY.
# Empty = service runs without auth.
API_KEY = os.environ.get("API_KEY", "")
AUTH_USER_KEY = "opensource_auth_user"
REQUEST_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
START_COMMAND = "uv run uvicorn service:app --port 8080 --app-dir opensource/agents/custom"
MAX_UPLOAD_BYTES = 1024 * 1024
MMCIF_SUFFIXES = frozenset({".cif", ".mcif", ".mmcif"})
SUPPORTED_UPLOAD_TYPES = {
    ".csv": "text/csv",
    ".cif": "chemical/x-cif",
    ".mcif": "chemical/x-cif",
    ".mmcif": "chemical/x-cif",
}
# The skill asks for `__VEGA_LITE__:`, but models routinely drop the underscores
# or bold them away in markdown, so accept every spelling we have seen.
VEGA_LITE_MARKER_RE = re.compile(r"(?:\*\*|__)?VEGA_LITE(?:\*\*|__)?\s*:")
ALLOWED_CHART_MARKS = {"bar", "line", "point", "area", "arc"}
ALLOWED_CHART_CHANNELS = {"x", "y", "color", "tooltip"}

WORKSPACES = {
    TRAVEL_WORKSPACE: {
        "slug": "travel",
        "category": "TRAVEL",
        "version": "1.0.0",
        "agent_name": AGENT_NAME,
        "title": "AI Travel Agent",
        "subtitle": (
            "Plan and research trips with an agent that can compare destinations, search "
            "flights and stays, check forecasts, explore maps, and analyze travel data."
        ),
        "legend": ("Flights", "Stays", "Destinations", "Weather", "Maps", "Web research"),
        "upload_types": ("csv",),
        "upload_help": (
            "Optionally upload one UTF-8 CSV file up to 1 MB for isolated data analysis."
        ),
        "placeholder": "Ask about destinations, flights, stays, weather, or your trip.",
        "button_label": "Ask travel agent",
        "card_description": (
            "Research destinations, compare live travel options, build itineraries, check "
            "weather, and analyze uploaded travel data."
        ),
        "tags": ("Flights", "Hotels", "Weather", "Maps", "CSV"),
        "primary_prompts": (
            "Compare Lisbon and Barcelona for a five-day trip in September.",
            "Find flight options from Paris to Tokyo for the first week of October.",
            "Plan a seven-day food and culture trip to Japan for two people.",
            "Compare hotels near central Rome under €200 per night.",
        ),
        "more_prompts": (
            "Check the weather forecast and suggest activities for Amsterdam next weekend.",
            "Research the best neighborhoods to stay in Mexico City and show them on a map.",
            "Analyze the uploaded CSV, summarize its numeric metrics, and plot the most useful comparison.",
        ),
    },
    PROTEIN_WORKSPACE: {
        "slug": "protein",
        "category": "SCIENTIFIC",
        "version": "1.0.0",
        "agent_name": "Protein Analysis Agent",
        "title": "Protein Analysis",
        "subtitle": (
            "Upload an mmCIF structure, ask molecular questions, select residues, and review "
            "the agent's analysis beside an interactive Mol* 3D view."
        ),
        "legend": (
            "mmCIF",
            "Chains",
            "Residues",
            "Ligands",
            "Mol* 3D",
            "Notebook",
            "Charts",
        ),
        "upload_types": ("cif", "mcif", "mmcif"),
        "upload_help": ("Upload one UTF-8 mmCIF file (.cif, .mcif, or .mmcif) up to 1 MB."),
        "placeholder": "Ask about chains, residues, ligands, or what the 3D view should show.",
        "button_label": "Analyze protein",
        "card_description": (
            "Inspect mmCIF structures, calculate molecular metrics, focus chains or "
            "residues, and explore agent- or notebook-directed Mol* 3D views."
        ),
        "tags": ("mmCIF", "Mol*", "Notebook", "Residues", "Ligands", "Charts"),
        "primary_prompts": (
            "Analyze this structure and count atoms, chains, residues, and non-water ligands.",
            "Show protein chain A and summarize its residue composition.",
            "Identify the ligands and explain where they occur in the structure.",
            "Plot residue counts by chain and display the corresponding 3D structure.",
        ),
        "more_prompts": (
            "Show residues 10–30 in chain A and color them by residue name.",
            "Compare author and label chain identifiers in this structure.",
            "Summarize secondary-structure-related records available in the uploaded mmCIF.",
        ),
    },
}

# Maps tool names (as reported by the agent service) to a plain-language,
# present-tense summary shown to non-technical users; unknown tools fall
# back to a generic "Ran <name>" label built from the raw name.
TOOL_FRIENDLY_SUMMARY: dict[str, str] = {
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
    "code_interpreter": "Analyzed the data",
    "python": "Analyzed the data",
}
# Icons for the tool-activity cards; a plain "settings gear" fallback covers
# any tool name not listed above.
TOOL_ICON = "\U0001f6e0️"

HISTORY_KEY = "opensource_history"
ACTIVE_HISTORY_KEY = "opensource_active_history_id"
CHAT_COUNTER_KEY = "opensource_chat_counter"
CURRENT_ENTRY_KEY = "opensource_current_entry"
NEW_CHAT_SENTINEL = "__new_chat__"
MAX_HISTORY_ITEMS = 12
MERMAID_BLOCK = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _current_workspace() -> str:
    workspace = st.session_state.get(WORKSPACE_KEY, TRAVEL_WORKSPACE)
    return workspace if workspace in WORKSPACES else TRAVEL_WORKSPACE


def _workspace_config() -> dict[str, Any]:
    return WORKSPACES[_current_workspace()]


def _workspace_state_key(base: str) -> str:
    """Scope protein state while preserving existing travel-session keys."""
    if _current_workspace() == TRAVEL_WORKSPACE:
        return base
    return f"{base}_{_workspace_config()['slug']}"


def _matching_workspaces(query: str) -> list[str]:
    """Return catalog workspaces matching a case-insensitive free-text query."""
    normalized = query.strip().casefold()
    if not normalized:
        return list(WORKSPACES)
    matches = []
    for name, config in WORKSPACES.items():
        searchable = " ".join(
            (
                name,
                config["category"],
                config["card_description"],
                *config["tags"],
            )
        ).casefold()
        if normalized in searchable:
            matches.append(name)
    return matches


def inject_styles() -> None:
    st.markdown(f"<style>{STYLE_PATH.read_text()}</style>", unsafe_allow_html=True)


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY} if API_KEY else {}


def _current_user_id() -> str:
    return st.session_state.get(AUTH_USER_KEY) or USER_ID


def _login_gate() -> bool:
    """Password gate; True when the viewer may see the app.

    Auth is on only when the users file (see auth.py) has entries, so a bare
    local checkout still opens without a login screen.
    """
    if not auth.auth_enabled() or st.session_state.get(AUTH_USER_KEY):
        return True
    st.markdown("## Sign in")
    with st.form("login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        # PBKDF2 verification (600k iterations) takes noticeable time; make
        # that wait visible instead of leaving the form looking frozen.
        with st.spinner("Signing you in…"):
            authenticated = auth.authenticate(username.strip(), password)
        if authenticated:
            st.session_state[AUTH_USER_KEY] = username.strip()
            st.rerun()
        # Same message whether the username or the password was wrong, so
        # the response never reveals which accounts exist.
        st.error("Invalid username or password.")
    return False


def _render_signout() -> None:
    if not st.session_state.get(AUTH_USER_KEY):
        return
    st.caption(f"Signed in as **{st.session_state[AUTH_USER_KEY]}**")
    if st.button("Sign out", use_container_width=True):
        st.session_state.pop(AUTH_USER_KEY, None)
        start_new_chat()
        st.rerun()


def _ping() -> dict[str, Any] | None:
    try:
        response = httpx.get(f"{AGENT_URL}/ping", timeout=3)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def _sse_events(response: httpx.Response) -> Iterable[dict[str, Any]]:
    for line in response.iter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data_str = line[len("data:") :].strip()
        if not data_str:
            continue
        try:
            yield json.loads(data_str)
        except json.JSONDecodeError:
            continue


def _safe(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _json_preview(value: Any, limit: int = 520) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        rendered = str(value)
    return rendered if len(rendered) <= limit else rendered[:limit].rstrip() + "\n..."


def _usage(metrics: dict[str, Any] | None) -> dict[str, int]:
    raw = (metrics or {}).get("usage", {}) or {}
    return {
        "input": int(raw.get("inputTokens", 0) or 0),
        "output": int(raw.get("outputTokens", 0) or 0),
        "total": int(raw.get("totalTokens", 0) or 0),
        "cache_read": int(raw.get("cacheReadInputTokens", 0) or 0),
        "cache_write": int(raw.get("cacheWriteInputTokens", 0) or 0),
    }


def _metric_value(value: Any, suffix: str = "") -> str:
    if value in (None, ""):
        return "-"
    if isinstance(value, float):
        rendered = f"{value:.2f}"
    elif isinstance(value, int):
        rendered = f"{value:,}"
    else:
        rendered = str(value)
    return f"{rendered}{suffix}"


def _response_model(state: dict[str, Any]) -> str:
    """Return the concrete model that produced this response, when known."""
    model_id = state.get("metrics", {}).get("model_id")
    return str(model_id).strip() if model_id else ""


def _model_badge(state: dict[str, Any]) -> str:
    model_id = _response_model(state)
    if not model_id:
        return ""
    return f'<span class="os-model-badge"><strong>Model</strong> {_safe(model_id)}</span>'


def _tool_call_count(state: dict[str, Any]) -> int:
    metrics = state.get("metrics", {})
    return int(metrics.get("tool_calls", 0) or 0)


def _cycle_count(state: dict[str, Any]) -> int:
    metrics = state.get("metrics", {})
    return int(metrics.get("cycles", 0) or metrics.get("tool_calls", 0) or 0)


def _header_chips(state: dict[str, Any]) -> str:
    usage = _usage(state.get("metrics"))
    model_dispatch_ms = state.get("metrics", {}).get("request_to_model_dispatch_ms")
    chips = [
        ("Latency", _metric_value(state.get("elapsed"), "s") if state.get("elapsed") else "-"),
        ("Model dispatch", _metric_value(model_dispatch_ms, "ms")),
        ("Tools", _metric_value(_tool_call_count(state))),
        ("Cycles", _metric_value(_cycle_count(state))),
        ("Tokens", _metric_value(usage["total"])),
    ]
    return "".join(f"<span><strong>{label}</strong> {value}</span>" for label, value in chips)


def _token_block(state: dict[str, Any]) -> str:
    usage = _usage(state.get("metrics"))
    return f"""
    <div class="os-token-block">
      <div class="os-token-total">
        <span class="os-token-total-group">
          <span class="os-token-total-num">{usage["total"]:,}</span>
          <span class="os-token-total-label">total tokens</span>
        </span>
        <span class="os-token-total-group">
          <span class="os-token-total-num">{_tool_call_count(state):,}</span>
          <span class="os-token-total-label">tool calls</span>
        </span>
      </div>
      <div class="os-token-detail">
        <span><strong>Input</strong> {usage["input"]:,}</span>
        <span><strong>Output</strong> {usage["output"]:,}</span>
        <span><strong>Cache read</strong> {usage["cache_read"]:,}</span>
        <span><strong>Cache write</strong> {usage["cache_write"]:,}</span>
      </div>
    </div>
    """


def _status_class(state: dict[str, Any]) -> str:
    if state["errors"]:
        return "error"
    if state["done"]:
        return "done"
    return "waiting" if state["status"] == "Waiting" else "running"


# Plain-language activity messages shown while the agent is working, keyed
# by the internal status set in _apply_agent_event / _append_tool_start.
ACTIVITY_MESSAGE = {
    "Waiting": "Understanding your request…",
    "Invoking": "Understanding your request…",
    "Thinking": "Thinking through your request…",
    "Browsing": "Using tools for your request…",
    "Responding": "Preparing your answer…",
}


def _current_activity(state: dict[str, Any]) -> str | None:
    """A short, friendly description of what the agent is doing right now,
    or None once it has finished (successfully, with an error, or stopped)."""
    if state["done"]:
        return None
    running_tool = next((t for t in reversed(state["tools"]) if t.get("result") is None), None)
    if running_tool:
        name = running_tool.get("name", "tool")
        return TOOL_FRIENDLY_SUMMARY.get(name, f"Running {name}…")
    return ACTIVITY_MESSAGE.get(state["status"], "Working on your request…")


def _last_completed_step(state: dict[str, Any]) -> str | None:
    for tool in reversed(state["tools"]):
        if tool.get("result") is not None:
            name = tool.get("name", "tool")
            return TOOL_FRIENDLY_SUMMARY.get(name, f"Ran {name}")
    return None


def _current_session_id() -> str:
    """One session id per chat, not per run, so follow-up prompts share the
    agent's server-side conversation history and one MLflow session."""
    counter_key = _workspace_state_key(CHAT_COUNTER_KEY)
    counter = st.session_state.get(counter_key, 0)
    session_prefix = (
        "opensource_session_id"
        if _current_workspace() == TRAVEL_WORKSPACE
        else "opensource_protein_session_id"
    )
    key = f"{session_prefix}_{counter}"
    if key not in st.session_state:
        query_key = f"os_session_{_workspace_config()['slug']}"
        candidate = str(st.query_params.get(query_key, ""))
        expected_prefix = f"opensource-{_workspace_config()['slug']}-"
        if (
            not candidate.startswith(expected_prefix)
            or len(candidate) > 100
            or not candidate.replace("-", "").isalnum()
        ):
            candidate = f"{expected_prefix}{time.time_ns()}"
        st.session_state[key] = candidate
        st.query_params[query_key] = candidate
    return st.session_state[key]


def _rotate_session_id() -> str:
    """Create a new chat id while leaving the previous durable ledger intact."""
    counter = st.session_state.get(_workspace_state_key(CHAT_COUNTER_KEY), 0)
    session_prefix = (
        "opensource_session_id"
        if _current_workspace() == TRAVEL_WORKSPACE
        else "opensource_protein_session_id"
    )
    session_id = f"opensource-{_workspace_config()['slug']}-{time.time_ns()}"
    st.session_state[f"{session_prefix}_{counter}"] = session_id
    st.query_params[f"os_session_{_workspace_config()['slug']}"] = session_id
    return session_id


def _new_state(
    session_id: str | None = None,
    attachment_names: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "arch_name": _workspace_config()["agent_name"],
        "status": "Waiting",
        "phase": "pre_tool",
        "answer": "",
        "pending_text": "",
        "trace": "",
        "tools": [],
        "metrics": {},
        "errors": [],
        "done": False,
        "elapsed": None,
        "started_at": None,
        "session_id": session_id or f"opensource-{_workspace_config()['slug']}-{time.time_ns()}",
        "attachments": list(attachment_names or []),
        "mlflow_trace_id": None,
        "feedback": None,
        "feedback_assessment_id": None,
        "feedback_error": None,
    }


def _snapshot_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "arch_name": state["arch_name"],
        "status": state["status"],
        "phase": state["phase"],
        "answer": state["answer"],
        "pending_text": state["pending_text"],
        "trace": state["trace"],
        "tools": [dict(tool) for tool in state["tools"]],
        "metrics": dict(state["metrics"]),
        "errors": list(state["errors"]),
        "done": state["done"],
        "elapsed": state["elapsed"],
        "started_at": state["started_at"],
        "session_id": state["session_id"],
        "attachments": list(state.get("attachments", [])),
        "mlflow_trace_id": state.get("mlflow_trace_id"),
        "feedback": state.get("feedback"),
        "feedback_assessment_id": state.get("feedback_assessment_id"),
        "feedback_error": state.get("feedback_error"),
    }


def _walk_json_values(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_values(child)


def _safe_chart_spec(candidate: Any) -> dict[str, Any] | None:
    """Keep only the small, inline Vega-Lite subset emitted by the interpreter."""
    if not isinstance(candidate, dict):
        return None
    mark = candidate.get("mark")
    mark_name = mark.get("type") if isinstance(mark, dict) else mark
    values = (
        candidate.get("data", {}).get("values") if isinstance(candidate.get("data"), dict) else None
    )
    encoding = candidate.get("encoding")
    if mark_name not in ALLOWED_CHART_MARKS or not isinstance(values, list):
        return None
    if not values or len(values) > 500 or not isinstance(encoding, dict):
        return None
    if any(not isinstance(row, dict) for row in values):
        return None
    if len(json.dumps(values, ensure_ascii=False, default=str)) > 100_000:
        return None
    clean_encoding = {
        channel: definition
        for channel, definition in encoding.items()
        if channel in ALLOWED_CHART_CHANNELS and isinstance(definition, (dict, list, str))
    }
    if not clean_encoding:
        return None
    spec: dict[str, Any] = {
        "data": {"values": values},
        "mark": mark if isinstance(mark, dict) else mark_name,
        "encoding": clean_encoding,
    }
    if isinstance(candidate.get("title"), str):
        spec["title"] = candidate["title"][:160]
    return spec


def _split_chart_markers(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Pull chart specs out of `text`, returning the text without the markers."""
    decoder = json.JSONDecoder()
    specs: list[dict[str, Any]] = []
    kept: list[str] = []
    cursor = 0
    while (match := VEGA_LITE_MARKER_RE.search(text, cursor)) is not None:
        chart_text = text[match.end() :]
        stripped = chart_text.lstrip()
        try:
            candidate, consumed = decoder.raw_decode(stripped)
        except json.JSONDecodeError:
            # Not a spec after all; keep the literal text and move past it.
            kept.append(text[cursor : match.end()])
            cursor = match.end()
            continue
        kept.append(text[cursor : match.start()])
        cursor = match.end() + (len(chart_text) - len(stripped)) + consumed
        spec = _safe_chart_spec(candidate)
        if spec is not None:
            specs.append(spec)
    kept.append(text[cursor:])
    return "".join(kept), specs


def _chart_specs_from_result(result: Any) -> list[dict[str, Any]]:
    """Extract interpreter chart markers from its nested JSON response."""
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            parsed = result
    specs: list[dict[str, Any]] = []
    for value in _walk_json_values(parsed):
        if isinstance(value, str):
            specs.extend(_split_chart_markers(value)[1])
    return specs


def _dedupe_specs(specs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in specs:
        fingerprint = json.dumps(spec, sort_keys=True, ensure_ascii=False, default=str)
        if fingerprint not in seen:
            seen.add(fingerprint)
            unique.append(spec)
    return unique


def _state_answer_and_charts(state: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Clean answer text plus every chart the interpreter or the answer carried."""
    specs: list[dict[str, Any]] = []
    for tool in state.get("tools", []):
        if tool.get("name") == "code_interpreter" and tool.get("result") is not None:
            specs.extend(_chart_specs_from_result(tool["result"]))
    # Models often restate the spec in the final answer instead of leaving it in
    # the tool result, so parse it there too and strip it from what we render.
    answer = state["answer"].strip() or state["pending_text"].strip()
    answer, answer_specs = _split_chart_markers(answer)
    cleaned = response_without_metric_payloads(response_without_view_payloads(answer))
    return cleaned, _dedupe_specs(specs + answer_specs)


def structure_selection_key(structure: bytes) -> str:
    """Session-state key holding the last Mol* selection made on `structure`."""
    return f"{SELECTION_STATE_PREFIX}{structure_fingerprint(structure)}"


def remember_structure_selection(structure: bytes, selection: dict[str, Any] | None) -> None:
    """Keep the answer-side selection so the next prompt can reference it."""
    if selection is not None:
        st.session_state[structure_selection_key(structure)] = selection


def recalled_structure_selection(structure: bytes | None) -> dict[str, Any] | None:
    """The selection a previous answer's viewer recorded for this structure."""
    if not structure:
        return None
    return st.session_state.get(structure_selection_key(structure))


def structure_layers_key(structure: bytes) -> str:
    """Session-state key holding the view stack currently shown for `structure`."""
    return f"{VIEW_LAYERS_STATE_PREFIX}{structure_fingerprint(structure)}"


def remember_view_layers(structure: bytes, layers: list[dict[str, Any]]) -> None:
    st.session_state[structure_layers_key(structure)] = layers


def recalled_view_layers(structure: bytes | None) -> list[dict[str, Any]]:
    """The view stack a previous turn left on screen for this structure."""
    if not structure:
        return []
    return st.session_state.get(structure_layers_key(structure), [])


def view_layers_prompt_context(layers: list[dict[str, Any]]) -> str:
    """Tell the agent what the viewer already shows so it can add to it."""
    if not layers:
        return ""
    return (
        "Application context: the 3D viewer currently shows "
        f"{layers_description(layers)}. To keep this and add to it, send a view "
        'request with "mode":"add". To start a new view, use "mode":"replace".'
    )


def structure_view_caption(view: dict[str, Any]) -> str:
    """Caption naming the coloring actually applied.

    The agent writes its prose and its view request separately, so an answer can
    claim one theme while the request carries another. Showing the theme the
    viewer was given makes that mismatch visible without opening the trace.
    """
    return f"{view['caption']} · colored by {view['color_theme']}"


def render_structure_view(
    structure: bytes,
    state: dict[str, Any],
    *,
    layers: list[dict[str, Any]] | None = None,
    allow_reset: bool = False,
) -> None:
    """Render the validated Mol* view the agent asked for, and keep its selection."""
    view = agent_structure_view(structure, state, layers=layers)
    st.markdown("### 3D view")
    st.caption(structure_view_caption(view))
    if view["warning"]:
        st.warning(view["warning"])
    if allow_reset and layers and st.button("Reset view", key="reset_structure_view"):
        remember_view_layers(structure, [])
        st.rerun()
    selection = render_molstar_component(
        view["structure"],
        # Keyed by the uploaded file, never by the filtered view. A key that
        # changed with the view made Streamlit mount a new component — and so a
        # new WebGL context — on every view change, while the old one was still
        # being torn down; enough of those and the browser tab dies, which
        # surfaces as a Streamlit disconnect. The component already reloads
        # in place when the structure fingerprint in its payload changes.
        key=(f"opensource_agent_dashboard_{structure_fingerprint(structure)}"),
        height=640,
        show_controls=False,
        show_sequence=True,
        color_theme=view["color_theme"],
    )
    remember_structure_selection(structure, selection)
    if selection is not None:
        st.caption(f"Mol* selection: {selection_description(selection)}")
    render_agent_measurements(structure, state)


def render_agent_measurements(structure: bytes, state: dict[str, Any]) -> None:
    """Run the measurements this turn's answer asked for, over the real file.

    The agent only names a metric; the arithmetic happens here, so a number in
    the answer can be checked against the number the structure actually gives.
    """
    for spec in metric_specs_for_state(state):
        with st.expander(f"Measured: {metric_description(spec)}", expanded=True):
            try:
                render_measurement(measure(structure, spec))
            except StructureAnalysisError as exc:
                st.warning(f"{exc}")


def render_structure_placeholder() -> None:
    """Right-pane stand-in shown until an answer carries a structure to display."""
    st.markdown("### 3D view")
    st.caption("The structure appears here once the agent answers.")
    st.info("Upload an mmCIF file and run a prompt to render the structure.")


def _notebook_state_key(prefix: str, structure: bytes) -> str:
    return f"{prefix}{structure_fingerprint(structure)}"


def render_measurement(result: dict[str, Any]) -> None:
    """Show one measurement as a headline plus its supporting table."""
    st.success(result["headline"])
    if result["rows"]:
        st.dataframe(
            result["rows"],
            column_order=result["columns"],
            hide_index=True,
            use_container_width=True,
        )


def run_measurement(structure: bytes, spec: dict[str, Any], *, apply_view: bool = True) -> None:
    """Measure, store the result for this structure, and show what was measured.

    The view layers come from the measurement itself, so the residues on screen
    are exactly the ones behind the numbers rather than a re-derived guess.
    """
    measurement_key = _notebook_state_key(MEASUREMENT_STATE_PREFIX, structure)
    try:
        result = measure(structure, spec)
    except StructureAnalysisError as exc:
        st.session_state[measurement_key] = {"kind": "error", "message": str(exc)}
        return
    st.session_state[measurement_key] = {"kind": "measurement", "result": result}
    if apply_view and result["view_layers"]:
        remember_view_layers(structure, result["view_layers"])


QUICK_METRICS: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("Summary", {"metric": "summary"}, "Chains, residues, ligands, and waters"),
    ("Ligands", {"metric": "ligands"}, "Every non-water ligand copy"),
    ("Binding site", {"metric": "binding_site"}, "Residues lining the first ligand pocket"),
    ("Polar contacts", {"metric": "polar_contacts"}, "Salt bridges, disulfides, polar pairs"),
    ("Geometry", {"metric": "geometry"}, "Radius of gyration, mass, bounding box"),
    ("Model quality", {"metric": "quality"}, "B-factors, occupancy, chain breaks"),
)


def render_structure_analysis(structure: bytes) -> None:
    """One-click quantitative measurements over the answered structure."""
    measurement_key = _notebook_state_key(MEASUREMENT_STATE_PREFIX, structure)
    st.markdown("### Structure analysis")
    st.caption(
        "Measurements are computed in trusted Python from the uploaded coordinates, "
        "and the 3D view follows what was measured."
    )
    with st.container(border=True, key="structure_analysis"):
        columns = st.columns(3)
        for index, (label, spec, help_text) in enumerate(QUICK_METRICS):
            with columns[index % 3]:
                if st.button(
                    label,
                    key=f"metric_{spec['metric']}_{structure_fingerprint(structure)}",
                    help=help_text,
                    use_container_width=True,
                ):
                    run_measurement(structure, spec)
                    st.rerun()

        output = st.session_state.get(measurement_key)
        if not output:
            st.caption("Pick a measurement to see numbers for this structure.")
        elif output["kind"] == "error":
            st.error(output["message"])
        else:
            render_measurement(output["result"])


def render_structure_notebook(structure: bytes) -> None:
    """Render a safe Python-style cell that updates the answer's Mol* view."""
    code_key = _notebook_state_key(NOTEBOOK_CODE_STATE_PREFIX, structure)
    output_key = _notebook_state_key(NOTEBOOK_OUTPUT_STATE_PREFIX, structure)
    if code_key not in st.session_state:
        st.session_state[code_key] = DEFAULT_NOTEBOOK_CELL

    st.markdown("### Live structure notebook")
    st.caption(
        "Run a Jupyter-style Python cell to update the 3D answer. "
        "Cells use a validated view API and never execute arbitrary Python."
    )
    with st.container(border=True, key="structure_notebook"):
        st.markdown(
            '<div class="os-notebook-cell-label">In&nbsp;[&nbsp;]:</div>', unsafe_allow_html=True
        )
        with st.form(
            key=f"structure_notebook_form_{structure_fingerprint(structure)}",
            border=False,
        ):
            source = st.text_area(
                "Python cell",
                key=code_key,
                height=230,
                label_visibility="collapsed",
                help=(
                    "Available calls: view(...), add_view(...), and reset_view(). "
                    "Use Ctrl+Enter to submit the cell."
                ),
            )
            run_cell = st.form_submit_button(
                "▶ Run cell",
                type="primary",
                use_container_width=True,
            )

        if run_cell:
            try:
                command = parse_notebook_cell(source)
                if command["action"] == "measure":
                    run_measurement(structure, command["spec"])
                    st.session_state[output_key] = {
                        "kind": "success",
                        "message": f"Measured {metric_description(command['spec'])}.",
                    }
                    st.rerun()
                layers = apply_notebook_command(recalled_view_layers(structure), command)
                # Validate against the actual structure before replacing a good
                # live view with a cell that selects no atoms.
                if layers:
                    _, matched = filter_mmcif(structure, layers)
                    message = (
                        f"Updated the 3D view: {layers_description(layers)} "
                        f"({matched:,} matching atom records)."
                    )
                else:
                    message = "Reset the 3D view to the full answered structure."
                remember_view_layers(structure, layers)
                st.session_state[output_key] = {"kind": "success", "message": message}
                st.rerun()
            except (NotebookCellError, ValueError) as exc:
                st.session_state[output_key] = {"kind": "error", "message": str(exc)}

        output = st.session_state.get(output_key)
        if output:
            if output["kind"] == "success":
                st.success(output["message"])
            else:
                st.error(output["message"])

        with st.expander("Notebook API", expanded=False):
            st.markdown(
                """
`view(...)` replaces the current view and `add_view(...)` adds another selection.
Use `reset_view()` to return to the full structure.

Supported arguments: `include`, `chains`, `residue_names`, `residue_ranges`,
`exclude_water`, and `color_theme`. Example range:
`residue_ranges=[{"chain": "A", "start": 10, "end": 30}]`.

`measure(...)` computes numbers instead of changing the view, and then shows the
residues it measured. Metrics: `summary`, `ligands`, `binding_site`, `distance`,
`contacts`, `polar_contacts`, `geometry`, `quality`. Examples:

```python
measure(metric="binding_site", ligand="HEM", cutoff=4.5)
measure(metric="contacts", chains=["A", "B"], cutoff=4.0)
measure(metric="distance", residues=[
    {"chain": "A", "seq": 58, "atom": "NE2"},
    {"chain": "A", "seq": 87},
])
```
                """
            )


def _append_tool_start(state: dict[str, Any], event: dict[str, Any]) -> None:
    tool_name = event.get("name", "tool")
    tool_id = event.get("tool_use_id") or event.get("toolUseId")
    tool_input = event.get("input", {})
    state["status"] = "Browsing"
    state["phase"] = "in_tools"

    for tool in state["tools"]:
        if tool_id and tool.get("id") == tool_id:
            tool["input"] = tool_input or tool.get("input", {})
            return

    metrics = state.setdefault("metrics", {})
    metrics["tool_calls"] = max(int(metrics.get("tool_calls", 0) or 0), len(state["tools"]) + 1)
    tool_names = metrics.setdefault("tool_names", [])
    if isinstance(tool_names, list):
        tool_names.append(tool_name)

    if state["pending_text"]:
        state["trace"] += state["pending_text"]
        state["pending_text"] = ""
    state["trace"] += f"\n-> {tool_name}: {_json_preview(tool_input, limit=180)}\n"
    state["tools"].append({"id": tool_id, "name": tool_name, "input": tool_input, "result": None})


def _append_tool_end(state: dict[str, Any], event: dict[str, Any]) -> None:
    tool_name = event.get("name", "tool")
    tool_id = event.get("tool_use_id") or event.get("toolUseId")
    result = event.get("result", "")
    matched = False
    for tool in reversed(state["tools"]):
        same_id = tool_id and tool.get("id") == tool_id
        same_running_name = not tool_id and tool["name"] == tool_name and tool["result"] is None
        if same_id or same_running_name:
            tool["result"] = result
            matched = True
            break
    if not matched:
        state["tools"].append({"id": tool_id, "name": tool_name, "input": {}, "result": result})
    if result:
        state["trace"] += f"   {_json_preview(result, limit=300)}\n"
    state["status"] = "Responding"
    state["phase"] = "post_tool"


def _apply_agent_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    event_type = event.get("type")
    if event_type == "trace":
        state["mlflow_trace_id"] = event.get("trace_id") or None
    elif event_type == "reasoning":
        content = event.get("content", "")
        if content:
            state["status"] = "Thinking"
            state["trace"] += content
    elif event_type == "text":
        content = event.get("content", "")
        if content:
            state["status"] = "Responding"
            target = "answer" if state["phase"] == "post_tool" else "pending_text"
            state[target] += content
    elif event_type == "tool_start":
        _append_tool_start(state, event)
    elif event_type == "tool_end":
        _append_tool_end(state, event)
    elif event_type == "metrics":
        merged_metrics = {**state.get("metrics", {}), **event}
        merged_metrics["tool_calls"] = max(
            int(state.get("metrics", {}).get("tool_calls", 0) or 0),
            int(event.get("tool_calls", 0) or 0),
            len(state["tools"]),
        )
        state["metrics"] = merged_metrics
    elif event_type == "error":
        state["status"] = "Error"
        state["errors"].append(event.get("content", "Unknown error"))


def _finish_state(state: dict[str, Any], elapsed: float) -> None:
    if state["phase"] == "pre_tool" and state["pending_text"] and not state["answer"]:
        state["answer"] = state["pending_text"]
        state["pending_text"] = ""
    state["elapsed"] = round(elapsed, 2)
    state["done"] = True
    if not state["errors"]:
        state["status"] = "Done"


def _apply_queue_item(state: dict[str, Any], item: tuple[str, Any]) -> None:
    item_type, data = item
    if item_type == "elapsed":
        state["elapsed"] = data
        return
    if item_type == "done":
        _finish_state(state, float(state.get("elapsed") or 0))
        return
    _apply_agent_event(state, data)


def _tool_summary_line(tool: dict[str, Any]) -> str:
    name = tool.get("name", "tool")
    return TOOL_FRIENDLY_SUMMARY.get(name, f"Ran {name.replace('_', ' ')}")


def _render_tools(tools: list[dict[str, Any]]) -> None:
    """Friendly, non-technical summary per tool call. Raw arguments/results
    live in a collapsed 'Technical details' expander for debugging."""
    if not tools:
        st.markdown('<div class="os-empty-state">No tool calls yet.</div>', unsafe_allow_html=True)
        return
    for index, tool in enumerate(tools, start=1):
        failed = isinstance(tool.get("result"), str) and tool["result"].lower().startswith("error")
        done = tool.get("result") is not None
        status_class = "failed" if failed else ("done" if done else "")
        status_label = "Failed" if failed else ("Completed" if done else "Running")
        pill_class = "error" if failed else ("done" if done else "running")
        st.markdown(
            f"""
            <div class="os-tool {status_class}">
              <div class="os-tool-head">
                <span class="os-tool-summary">
                  <span class="os-tool-icon">{TOOL_ICON}</span>
                  {index}. {_safe(_tool_summary_line(tool))}
                </span>
                <span class="os-pill {pill_class}">{status_label}</span>
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


def _submit_mlflow_feedback(state: dict[str, Any], rating: str) -> dict[str, str]:
    response = httpx.post(
        f"{AGENT_URL}/feedback",
        json={
            "trace_id": state["mlflow_trace_id"],
            "assessment_id": state.get("feedback_assessment_id"),
            "rating": rating,
            "user_id": _current_user_id(),
            "session_id": state["session_id"],
            "workspace": _current_workspace(),
        },
        headers=_auth_headers(),
        timeout=httpx.Timeout(15.0, connect=5.0),
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.is_error:
        raise RuntimeError(
            payload.get("error") or f"Feedback request failed: HTTP {response.status_code}"
        )
    return payload


def _render_mlflow_feedback(state: dict[str, Any]) -> None:
    trace_id = state.get("mlflow_trace_id")
    answer_text, _ = _state_answer_and_charts(state)
    if not state.get("done") or state.get("errors") or not answer_text or not trace_id:
        return

    st.caption("Was this answer helpful?")
    default = {"down": 0, "up": 1}.get(state.get("feedback"))
    selected = st.feedback(
        "thumbs",
        key=f"opensource_feedback_{trace_id}",
        default=default,
    )
    rating = {0: "down", 1: "up"}.get(selected)
    if rating and rating != state.get("feedback"):
        try:
            saved = _submit_mlflow_feedback(state, rating)
            state["feedback"] = rating
            state["feedback_assessment_id"] = saved["assessment_id"]
            state["feedback_error"] = None
        except (httpx.HTTPError, RuntimeError, KeyError) as exc:
            state["feedback_error"] = str(exc)
    if state.get("feedback_error"):
        st.error(f"Feedback was not recorded: {state['feedback_error']}")
    elif state.get("feedback"):
        st.caption("Feedback recorded in MLflow.")


def render_agent_panel(placeholders: dict[str, Any], state: dict[str, Any]) -> None:
    status = "Error" if state["errors"] else state["status"]
    placeholders["status"].markdown(
        f"""
        <div class="os-panel-title">
          <h2 class="os-panel-name">
            <span class="os-dot"></span>
            {html.escape(state["arch_name"])}
          </h2>
          <span class="os-pill {_status_class(state)}">{html.escape(status)}</span>
        </div>
        <div class="os-chips">{_header_chips(state)}</div>
        {_token_block(state)}
        """,
        unsafe_allow_html=True,
    )

    activity = _current_activity(state)
    if activity:
        last_step = _last_completed_step(state)
        detail = (
            f'<span class="os-activity-detail">Last completed: {_safe(last_step)}</span>'
            if last_step
            else ""
        )
        placeholders["activity"].markdown(
            f"""
            <div class="os-activity" role="status" aria-live="polite">
              <span class="os-spinner" aria-hidden="true"></span>
              <span>{_safe(activity)}</span>
              {detail}
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        placeholders["activity"].empty()

    if state["errors"]:
        failed_step = _last_completed_step(state) or "the last step"
        placeholders["error"].markdown(
            f"""
            <div class="os-error-box">
              <strong>Something went wrong while processing your request.</strong>
              <p>The request stopped after: {_safe(failed_step)}. You can retry, or edit your
              prompt and send it again.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        with placeholders["error_detail"].expander("Technical details", expanded=False):
            st.code("\n".join(state["errors"]), language="text")
    else:
        placeholders["error"].empty()
        placeholders["error_detail"].empty()

    answer_text, chart_specs = _state_answer_and_charts(state)
    with placeholders["answer"].container():
        st.markdown(
            f"""
            <div class="os-answer-meta">
              <span class="os-answer-note">Response</span>
              {_model_badge(state)}
            </div>
            """,
            unsafe_allow_html=True,
        )
        with st.container(border=True, height=420):
            st.markdown(answer_text or "Waiting for response...")
    with placeholders["feedback"].container():
        _render_mlflow_feedback(state)
    if chart_specs:
        with placeholders["visuals"].container():
            st.markdown("**Interactive chart**")
            for chart_spec in chart_specs:
                st.vega_lite_chart(
                    chart_spec,
                    use_container_width=True,
                )
    else:
        placeholders["visuals"].empty()
    has_trace = bool(state["trace"].strip() or state["tools"]) or not state["done"]
    with placeholders["trace"].expander("Tool activity", expanded=has_trace):
        _render_tools(state["tools"])
        if state["trace"].strip():
            with st.expander("Technical details", expanded=False):
                st.code(state["trace"].strip(), language="text")

    attachment_text = ""
    if state.get("attachments"):
        names = ", ".join(_safe(name) for name in state["attachments"])
        attachment_text = f"<br>Uploaded file: <strong>{names}</strong>"
    placeholders["session"].markdown(
        f"""
        <div class="os-session-block">
          Session ID: <code>{html.escape(state["session_id"])}</code>
          {attachment_text}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _stream_agent(
    prompt: str,
    user_id: str,
    session_id: str,
    event_queue: queue.Queue,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    started = time.time()
    try:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "user_id": user_id,
            "session_id": session_id,
        }
        if attachments:
            payload["attachments"] = attachments
        with httpx.stream(
            "POST",
            f"{AGENT_URL}/invocations",
            json=payload,
            headers=_auth_headers(),
            timeout=REQUEST_TIMEOUT,
        ) as response:
            response.raise_for_status()
            for event in _sse_events(response):
                event_queue.put_nowait(("event", event))
    except httpx.HTTPError as exc:
        event_queue.put_nowait(("event", {"type": "error", "content": f"Request failed: {exc}"}))
    finally:
        event_queue.put_nowait(("elapsed", round(time.time() - started, 2)))
        event_queue.put_nowait(("done", None))


def run_local_agent(
    prompt: str,
    attachments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    attachment_names = [item["name"] for item in attachments or []]
    state = _new_state(_current_session_id(), attachment_names)
    state["status"] = "Invoking"
    state["started_at"] = time.time()
    event_queue: queue.Queue = queue.Queue()

    with st.container(border=True):
        placeholders = {
            "status": st.empty(),
            "activity": st.empty(),
            "error": st.empty(),
            "error_detail": st.empty(),
            "answer": st.empty(),
            "feedback": st.empty(),
            "visuals": st.empty(),
            "trace": st.empty(),
            "session": st.empty(),
        }
        render_agent_panel(placeholders, state)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _stream_agent,
            prompt,
            _current_user_id(),
            state["session_id"],
            event_queue,
            attachments,
        )
        while not state["done"]:
            changed = False
            while True:
                try:
                    item = event_queue.get_nowait()
                except queue.Empty:
                    break
                _apply_queue_item(state, item)
                changed = True
            if changed:
                render_agent_panel(placeholders, state)
            if future.done() and not changed:
                if not state["done"]:
                    state["done"] = True
                    state["status"] = "Stopped"
                    render_agent_panel(placeholders, state)
                break
            time.sleep(0.1)

        error = future.exception()
        if error:
            state["errors"].append(str(error))
            state["status"] = "Error"
            state["done"] = True
            render_agent_panel(placeholders, state)

    return state


def _short_prompt(prompt: str, limit: int = 54) -> str:
    compact = " ".join(prompt.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}..."


def _history() -> list[dict[str, Any]]:
    history_key = _workspace_state_key(HISTORY_KEY)
    if history_key not in st.session_state:
        st.session_state[history_key] = []
    return st.session_state[history_key]


def current_prompt_key() -> str:
    counter = st.session_state.get(_workspace_state_key(CHAT_COUNTER_KEY), 0)
    prefix = (
        "opensource_prompt_input"
        if _current_workspace() == TRAVEL_WORKSPACE
        else "opensource_protein_prompt_input"
    )
    return f"{prefix}_{counter}"


def start_new_chat() -> None:
    counter_key = _workspace_state_key(CHAT_COUNTER_KEY)
    st.session_state[counter_key] = st.session_state.get(counter_key, 0) + 1
    st.session_state[_workspace_state_key(ACTIVE_HISTORY_KEY)] = NEW_CHAT_SENTINEL
    st.session_state.pop(_workspace_state_key(CURRENT_ENTRY_KEY), None)
    _rotate_session_id()


def add_history_entry(
    prompt: str,
    state: dict[str, Any],
    *,
    structure: bytes | None = None,
    view_layers: list[dict[str, Any]] | None = None,
) -> None:
    history = _history()
    created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "id": f"run-{time.time_ns()}",
        "label": f"{created_at} | {_short_prompt(prompt)}",
        "created_at": created_at,
        "prompt": prompt,
        "state": _snapshot_state(state),
        "structure": structure,
        # Snapshot of the composed stack, so replaying this run from history
        # shows what it showed rather than the live, still-growing view.
        "view_layers": list(view_layers or []),
    }
    history.insert(0, entry)
    del history[MAX_HISTORY_ITEMS:]
    st.session_state[_workspace_state_key(HISTORY_KEY)] = history
    st.session_state[_workspace_state_key(CURRENT_ENTRY_KEY)] = entry
    st.session_state[_workspace_state_key(ACTIVE_HISTORY_KEY)] = NEW_CHAT_SENTINEL


def current_entry() -> dict[str, Any] | None:
    return st.session_state.get(_workspace_state_key(CURRENT_ENTRY_KEY))


def _run_status_label(entry: dict[str, Any]) -> str:
    state = entry["state"]
    if state.get("errors"):
        return "Failed"
    if not state.get("done"):
        return "Cancelled"
    return "Completed"


def render_history_selector() -> dict[str, Any] | None:
    """Vertical list of past runs (not a continuous conversation - each
    entry is a separate execution), with the active one highlighted."""
    history = _history()
    st.subheader("Past runs")
    if not history:
        st.markdown(
            '<div class="os-empty-state">No past runs yet. Start by sending a prompt on '
            "this page.</div>",
            unsafe_allow_html=True,
        )
        return None

    active_history_key = _workspace_state_key(ACTIVE_HISTORY_KEY)
    active_id = st.session_state.get(active_history_key, NEW_CHAT_SENTINEL)
    for entry in history:
        status_label = _run_status_label(entry)
        is_active = entry["id"] == active_id
        button_label = f"{'➤ ' if is_active else ''}{_short_prompt(entry['prompt'])}"
        if st.button(button_label, key=f"history_{entry['id']}", use_container_width=True):
            st.session_state[active_history_key] = entry["id"]
            st.rerun()
        st.markdown(
            f'<span class="os-history-item-label">{_safe(entry["created_at"])} · {status_label}</span>',
            unsafe_allow_html=True,
        )

    if st.button("Clear history"):
        st.session_state[_workspace_state_key(HISTORY_KEY)] = []
        # Also rotate the session id: keeping the old one would let the next
        # prompt silently continue the cleared conversation's agent memory.
        start_new_chat()
        st.rerun()
    if active_id == NEW_CHAT_SENTINEL:
        return None
    return next((entry for entry in history if entry["id"] == active_id), None)


def render_brand_bar() -> None:
    st.markdown(
        """
        <div class="catalog-brand-bar">
          <div class="catalog-brand-mark">AB</div>
          <div class="catalog-brand-name">Agentic Blueprint</div>
          <div class="catalog-brand-divider"></div>
          <div class="catalog-brand-product">Scientific Agent Workbench</div>
          <div class="catalog-brand-status">OPEN SOURCE</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_catalog_card(workspace: str) -> None:
    config = WORKSPACES[workspace]
    slug = config["slug"]
    tags = "".join(
        f'<span class="catalog-card-tag">{html.escape(tag)}</span>' for tag in config["tags"]
    )
    with st.container(border=True, key=f"agent_card_{slug}"):
        st.markdown(
            f"""
            <div class="catalog-card-kicker">
              <span class="catalog-card-category {slug}">{html.escape(config["category"])}</span>
              <span class="catalog-card-ready">READY</span>
            </div>
            <h3 class="catalog-card-title">{html.escape(config["title"])}</h3>
            <p class="catalog-card-version">Version {html.escape(config["version"])}</p>
            <div class="catalog-card-tags">{tags}</div>
            <p class="catalog-card-description">{html.escape(config["card_description"])}</p>
            <div class="catalog-card-metrics">
              <span><strong>{len(config["primary_prompts"])}</strong> prompt starters</span>
              <span><strong>{len(config["tags"])}</strong> capabilities</span>
              <span><strong>1</strong> isolated session</span>
            </div>
            <div class="catalog-card-access"><span>●</span> Available in this workspace</div>
            """,
            unsafe_allow_html=True,
        )
        if st.button(
            "Open workspace",
            key=f"open_workspace_{slug}",
            type="primary",
            use_container_width=True,
        ):
            st.session_state[WORKSPACE_KEY] = workspace
            st.session_state[PENDING_NAVIGATION_KEY] = workspace
            st.rerun()


def render_catalog() -> None:
    st.markdown(
        """
        <div class="catalog-page-heading">
          <div class="catalog-page-icon">⌬</div>
          <div>
            <h1>Agent Catalog</h1>
            <p>Browse and launch the available agent workspaces.</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    search_column, sort_column = st.columns([4, 1], gap="small")
    with search_column:
        query = st.text_input(
            "Search agents",
            placeholder="Search agents by name, description, or capability…",
            label_visibility="collapsed",
            key="catalog_search",
        )
    with sort_column:
        sort_by = st.selectbox(
            "Sort agents",
            ["Featured", "Name", "Category"],
            label_visibility="collapsed",
            key="catalog_sort",
        )

    workspaces = _matching_workspaces(query)
    if sort_by == "Category":
        workspaces.sort(key=lambda item: (WORKSPACES[item]["category"], item))
    elif sort_by == "Name":
        workspaces.sort()

    st.markdown(
        f"""
        <div class="catalog-results-bar">
          <span><strong>{len(workspaces)}</strong> available agents</span>
          <span class="catalog-grid-indicator">▦ GRID</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if not workspaces:
        st.info("No agents match that search. Try a capability such as weather, mmCIF, or charts.")
        return

    columns = st.columns(2, gap="large")
    for index, workspace in enumerate(workspaces):
        with columns[index % len(columns)]:
            _render_catalog_card(workspace)


def render_header() -> None:
    config = _workspace_config()
    legend = "".join(f"<span>{html.escape(item)}</span>" for item in config["legend"])
    st.markdown(
        f"""
        <div class="os-hero">
          <div>
            <h1 class="os-title">{html.escape(config["title"])}</h1>
            <p class="os-subtitle">{html.escape(config["subtitle"])}</p>
          </div>
          <div class="os-legend">{legend}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_summary(info: dict[str, Any] | None) -> None:
    config = _workspace_config()
    model = info.get("model", "?") if info else "offline"
    if _current_workspace() == PROTEIN_WORKSPACE:
        skills = "mmCIF analysis, code interpreter, Mol* 3D views, interactive charts"
        note = (
            "Upload a molecular structure for file-specific analysis. The viewer uses "
            "validated declarative selections returned by the agent."
        )
    else:
        skills = (
            "flight search, accommodation search, city comparison, weather, maps, web "
            "research, code interpreter"
        )
        note = (
            "This assistant researches and compares options for you; it does not complete "
            "bookings or payments on your behalf."
        )
    with st.expander("How this works", expanded=False):
        st.markdown(
            f"""
| | {config["agent_name"]} |
|---|---|
| **Call** | `POST {AGENT_URL}/invocations` |
| **Transport** | HTTP Server-Sent Events |
| **Agent loop** | Repository-owned Strands agent |
| **Model** | `{model}` |
| **Skills** | {skills} |

{note}
"""
        )


def render_service_status(info: dict[str, Any] | None) -> None:
    agent_name = _workspace_config()["agent_name"]
    st.subheader("Agent service")
    if info:
        st.markdown(
            f"""
            <div class="os-panel-title">
              <h3 class="os-panel-name"><span class="os-dot"></span>{agent_name}</h3>
              <span class="os-pill done">Online · {_safe(info.get("model", "?"))}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"""
            <div class="os-panel-title">
              <h3 class="os-panel-name"><span class="os-dot"></span>{agent_name}</h3>
              <span class="os-pill error">Offline</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="os-note">Start the FastAPI agent service, then refresh this page.</div>',
            unsafe_allow_html=True,
        )
        st.code(START_COMMAND, language="bash")
    st.caption(f"Endpoint: `{AGENT_URL}/invocations`")


def render_current_run(entry: dict[str, Any]) -> None:
    with st.container(border=True):
        placeholders = {
            "status": st.empty(),
            "activity": st.empty(),
            "error": st.empty(),
            "error_detail": st.empty(),
            "answer": st.empty(),
            "feedback": st.empty(),
            "visuals": st.empty(),
            "trace": st.empty(),
            "session": st.empty(),
        }
        render_agent_panel(placeholders, entry["state"])


def render_saved_run(entry: dict[str, Any]) -> None:
    st.markdown("## History Details")
    st.caption(entry["created_at"])
    with st.container(border=True):
        st.markdown("**Prompt**")
        st.markdown(entry["prompt"])
    render_current_run(entry)


def render_docs_selector() -> str:
    documents = _documents()
    return st.selectbox("Document", list(documents))


def _document_label(path: Path) -> str:
    if path.name == "README.md":
        return "README"
    relative = path.relative_to(DOCS_DIR).with_suffix("")
    return str(relative).replace("/", " / ").replace("-", " ").replace("_", " ").title()


def _documents() -> dict[str, Path]:
    docs = {"README": ROOT / "README.md"}
    if DOCS_DIR.exists():
        for path in sorted(DOCS_DIR.rglob("*.md")):
            docs[_document_label(path)] = path
    return docs


def _render_mermaid(source: str) -> None:
    """Renders a Mermaid diagram via CDN, but falls back to a readable
    source block (rather than a silent blank area) when the script fails
    to load - e.g. no internet access in an offline/local environment."""
    diagram = html.escape(source.strip())
    fallback_id = f"mermaid-fallback-{abs(hash(source)) % 10_000}"
    components.html(
        f"""
        <div class="mermaid">{diagram}</div>
        <div id="{fallback_id}" style="display:none;font:12px monospace;white-space:pre-wrap;
             border:1px solid #dde2ea;border-radius:8px;padding:10px;color:#667085;">
          Diagram rendering requires internet access to load Mermaid. Source:
{diagram}
        </div>
        <script type="module">
          try {{
            const mermaid = (await import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs")).default;
            mermaid.initialize({{ startOnLoad: false, securityLevel: "loose", theme: "default" }});
            await mermaid.run({{ querySelector: ".mermaid" }});
          }} catch (err) {{
            document.querySelector(".mermaid").style.display = "none";
            document.getElementById("{fallback_id}").style.display = "block";
          }}
        </script>
        """,
        height=560,
        scrolling=True,
    )


def _render_markdown_with_mermaid(markdown: str) -> None:
    cursor = 0
    for match in MERMAID_BLOCK.finditer(markdown):
        before = markdown[cursor : match.start()]
        if before.strip():
            st.markdown(before)
        _render_mermaid(match.group(1))
        cursor = match.end()
    after = markdown[cursor:]
    if after.strip():
        st.markdown(after)


def render_docs_view(selected: str) -> None:
    documents = _documents()
    path = documents[selected]
    if not path.exists():
        st.error(f"Documentation file not found: {path.relative_to(ROOT)}")
        return
    st.subheader(selected)
    _render_markdown_with_mermaid(path.read_text())


def render_example_prompts() -> None:
    config = _workspace_config()
    st.markdown('<p class="os-example-hint">Try asking</p>', unsafe_allow_html=True)
    for prompt_option in config["primary_prompts"]:
        if st.button(prompt_option, use_container_width=True, key=f"example_{prompt_option}"):
            st.session_state[current_prompt_key()] = prompt_option
            st.rerun()
    with st.expander("More ideas", expanded=False):
        for prompt_option in config["more_prompts"]:
            if st.button(
                prompt_option, use_container_width=True, key=f"example_more_{prompt_option}"
            ):
                st.session_state[current_prompt_key()] = prompt_option
                st.rerun()


def _attachment_payload(uploaded_file: Any) -> list[dict[str, str]]:
    """Build the JSON attachment contract without writing the upload in the UI container."""
    if uploaded_file is None:
        return []
    content = uploaded_file.getvalue()
    if not content:
        raise ValueError("The selected file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"The selected file exceeds the {MAX_UPLOAD_BYTES // 1024:,} KB limit.")
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("The selected file must be UTF-8 encoded text.") from exc
    suffix = Path(uploaded_file.name).suffix.lower()
    content_type = SUPPORTED_UPLOAD_TYPES.get(suffix)
    if content_type is None:
        raise ValueError("Only .csv, .cif, .mcif, and .mmcif files are supported.")
    return [
        {
            "name": Path(uploaded_file.name).name,
            "content_type": content_type,
            "data_base64": base64.b64encode(content).decode("ascii"),
        }
    ]


def _csv_attachment_payload(uploaded_file: Any) -> list[dict[str, str]]:
    """Compatibility wrapper for the original CSV-only UI helper."""
    return _attachment_payload(uploaded_file)


def _is_mmcif_attachment(attachments: list[dict[str, str]]) -> bool:
    """Return whether the validated UI attachment is an mmCIF structure."""
    return bool(attachments and Path(attachments[0]["name"]).suffix.lower() in MMCIF_SUFFIXES)


def main() -> None:
    st.set_page_config(
        page_title="Open-source Agent Workbench",
        layout="wide",
    )
    inject_styles()
    if not _login_gate():
        return
    service_info = _ping()
    render_brand_bar()

    with st.sidebar:
        st.markdown(
            """
            <div class="catalog-sidebar-logo">
              <span>AB</span>
              <strong>WORKBENCH</strong>
            </div>
            <p class="catalog-sidebar-section">LIBRARY</p>
            """,
            unsafe_allow_html=True,
        )
        _render_signout()
        navigation = [
            CATALOG_VIEW,
            TRAVEL_WORKSPACE,
            PROTEIN_WORKSPACE,
            DOCUMENTATION_VIEW,
        ]
        if (pending_navigation := st.session_state.pop(PENDING_NAVIGATION_KEY, None)) in navigation:
            st.session_state[NAVIGATION_KEY] = pending_navigation
        if st.session_state.get(NAVIGATION_KEY) not in navigation:
            st.session_state[NAVIGATION_KEY] = CATALOG_VIEW
        view = st.radio(
            "Navigate",
            navigation,
            key=NAVIGATION_KEY,
            label_visibility="collapsed",
        )
        if view in WORKSPACES:
            st.session_state[WORKSPACE_KEY] = view
        st.divider()

        if view in WORKSPACES and st.button(
            "New chat",
            type="primary",
            use_container_width=True,
        ):
            start_new_chat()
            st.rerun()

        if view == DOCUMENTATION_VIEW:
            selected_doc = render_docs_selector()
        elif view in WORKSPACES:
            st.markdown(
                '<p class="catalog-sidebar-section">RUNS</p>',
                unsafe_allow_html=True,
            )
            st.divider()
            st.header("Run settings")
            render_service_status(service_info)
            st.divider()
            render_example_prompts()
            st.divider()
            selected_history = render_history_selector()

    if view == CATALOG_VIEW:
        render_catalog()
        return

    if view == DOCUMENTATION_VIEW:
        st.markdown("# Documentation")
        render_docs_view(selected_doc)
        return

    config = _workspace_config()
    render_header()
    render_summary(service_info)

    # The scientific workflow is a two-pane page: prompt and analysis on the
    # left, the Mol* result on the right. Travel keeps the single-column flow.
    split_layout = view == PROTEIN_WORKSPACE
    if split_layout:
        prompt_pane, structure_column = st.columns([0.9, 1.1], gap="large")
        structure_slot = structure_column.container(key="structure_pane")
    else:
        prompt_pane = st.container()
        structure_slot = None

    with (
        prompt_pane,
        st.container(
            border=True,
            key="prompt_section_split" if split_layout else "prompt_section",
        ),
    ):
        uploaded_file = st.file_uploader(
            (
                "Upload mmCIF structure"
                if view == PROTEIN_WORKSPACE
                else "Upload travel data (optional)"
            ),
            type=list(config["upload_types"]),
            accept_multiple_files=False,
            key=f"{config['slug']}_data_upload",
            help=config["upload_help"],
        )
        attachment_error = None
        try:
            attachments = _attachment_payload(uploaded_file)
        except ValueError as exc:
            attachments = []
            attachment_error = str(exc)
            st.error(attachment_error)
        is_mmcif_upload = _is_mmcif_attachment(attachments)
        if attachments:
            size_kb = len(uploaded_file.getvalue()) / 1024
            st.caption(
                f"Ready: `{attachments[0]['name']}` ({size_kb:,.1f} KB). "
                + (
                    "Ask for counts or say what Mol* should display."
                    if is_mmcif_upload
                    else "Ask for metrics, trends, or an interactive chart."
                )
            )
        # No structure is rendered on upload: Mol* only appears with an answer,
        # so the selection carried into a prompt is the one made on that answer.
        if is_mmcif_upload:
            structure_bytes = uploaded_file.getvalue()
            st.caption(
                f"Mol* {MOLSTAR_VERSION} · {atom_record_count(structure_bytes):,} atom "
                "records parsed · the 3D view renders with the agent's answer"
            )
            molstar_selection = recalled_structure_selection(structure_bytes)
            if molstar_selection is not None:
                st.caption(f"Mol* selection: {selection_description(molstar_selection)}")
        else:
            molstar_selection = None
        prompt = st.text_input(
            "Prompt",
            placeholder=config["placeholder"],
            key=current_prompt_key(),
        )
        run = st.button(
            config["button_label"],
            type="primary",
            disabled=(not prompt.strip() and not attachments)
            or attachment_error is not None
            or service_info is None,
        )

    result_slot = prompt_pane.container()
    rendered_structure = False
    notebook_structure: bytes | None = None
    if run:
        if prompt.strip():
            run_prompt = prompt.strip()
        elif view == PROTEIN_WORKSPACE:
            run_prompt = (
                "Analyze the uploaded mmCIF structure and report atom, chain, residue, and "
                "non-water ligand counts."
            )
        else:
            run_prompt = (
                "Analyze the uploaded CSV, summarize its key numeric metrics, and plot the "
                "most useful comparison."
            )
        agent_prompt = run_prompt
        if view == PROTEIN_WORKSPACE:
            agent_prompt = (
                "Protein analysis workspace. Focus on molecular structure and the uploaded "
                f"mmCIF data when present.\n\nUser request: {run_prompt}"
            )
        if selection_context := (
            selection_prompt_context(molstar_selection) if molstar_selection else ""
        ):
            agent_prompt = f"{agent_prompt}\n\n{selection_context}"
        structure_bytes = uploaded_file.getvalue() if is_mmcif_upload else None
        previous_layers = recalled_view_layers(structure_bytes)
        if layers_context := view_layers_prompt_context(previous_layers):
            agent_prompt = f"{agent_prompt}\n\n{layers_context}"
        with result_slot:
            state = run_local_agent(agent_prompt, attachments)
        # Compose once per completed run, not per rerender, so an additive
        # request is not applied again every time Streamlit reruns the page.
        run_layers = (
            compose_view_layers(previous_layers, state) if structure_bytes is not None else []
        )
        if structure_bytes is not None and structure_slot is not None:
            remember_view_layers(structure_bytes, run_layers)
            with structure_slot:
                render_structure_view(
                    structure_bytes,
                    state,
                    layers=run_layers,
                    allow_reset=True,
                )
            rendered_structure = True
            notebook_structure = structure_bytes
        add_history_entry(
            run_prompt,
            state,
            structure=structure_bytes,
            view_layers=run_layers,
        )
    elif selected_history:
        with result_slot:
            render_saved_run(selected_history)
        if (structure := selected_history.get("structure")) and structure_slot is not None:
            with structure_slot:
                # A saved run is a snapshot: render its own stack, and do not
                # let it disturb the live view stack.
                render_structure_view(
                    structure,
                    selected_history["state"],
                    layers=selected_history.get("view_layers") or None,
                )
            rendered_structure = True
    elif entry := current_entry():
        with result_slot:
            render_current_run(entry)
        structure = entry.get("structure")
        if structure and structure_slot is not None:
            with structure_slot:
                # The live stack, so a Reset takes effect on the next rerun.
                render_structure_view(
                    structure,
                    entry["state"],
                    layers=recalled_view_layers(structure) or None,
                    allow_reset=True,
                )
            rendered_structure = True
            notebook_structure = structure

    # The notebook controls only a structure already attached to an answer.
    # Merely selecting an upload never creates either the notebook or Mol*.
    if notebook_structure is not None and (run or selected_history is None):
        with prompt_pane:
            render_structure_analysis(notebook_structure)
            render_structure_notebook(notebook_structure)

    if structure_slot is not None and not rendered_structure:
        with structure_slot:
            render_structure_placeholder()


if __name__ == "__main__":
    main()
