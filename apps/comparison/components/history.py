"""Streamlit session-history controls."""

from __future__ import annotations

import copy
import html
import time

import streamlit as st
from models.state import snapshot_states
from services.session_cost import reset_session_cost

HISTORY_KEY = "comparison_history"
ACTIVE_HISTORY_KEY = "active_history_id"
BRANCH_SELECTOR_KEY = "comparison_branch_selector"
CHAT_COUNTER_KEY = "chat_counter"
CURRENT_ENTRY_KEY = "current_comparison_entry"
MAX_HISTORY_ITEMS = 12
NEW_CHAT_SENTINEL = "__new_chat__"
EDITING_PROMPT_KEY = "editing_comparison_prompt_id"
RESUBMIT_REQUEST_KEY = "comparison_prompt_resubmit_request"
TREE_SELECTION_EVENT_KEY = "comparison_tree_selection_event"

_CONVERSATION_TREE_HTML = '<div data-conversation-tree-component></div>'
_CONVERSATION_TREE_JS = """
let clickSequence = 0;

export default function(component) {
  const { data, parentElement, setStateValue } = component;
  const host = parentElement.querySelector('[data-conversation-tree-component]');
  host.innerHTML = data.html;

  const selectNode = (event) => {
    const node = event.target.closest('[data-node-id]');
    if (!node || !host.contains(node)) return;
    clickSequence += 1;
    setStateValue('selection', {
      id: node.dataset.nodeId,
      event: `${Date.now()}-${clickSequence}`,
    });
  };

  host.addEventListener('click', selectNode);
  return () => host.removeEventListener('click', selectNode);
}
"""

_conversation_tree_component = st.components.v2.component(
    "aws_blueprint_conversation_tree",
    html=_CONVERSATION_TREE_HTML,
    js=_CONVERSATION_TREE_JS,
    isolate_styles=False,
)


def _scoped_key(key: str, page_scope: str) -> str:
    return key if page_scope == "comparison" else f"{key}:{page_scope}"


def _entry_scope(entry: dict) -> str:
    return str(entry.get("options", {}).get("_page_scope") or "comparison")


def current_prompt_key(page_scope: str = "comparison") -> str:
    counter_key = _scoped_key(CHAT_COUNTER_KEY, page_scope)
    return f"prompt_input_{page_scope}_{st.session_state.get(counter_key, 0)}"


def prompt_edit_key(entry_id: str) -> str:
    return f"comparison_prompt_edit_{entry_id}"


def begin_prompt_edit(entry: dict) -> None:
    page_scope = _entry_scope(entry)
    st.session_state[_scoped_key(EDITING_PROMPT_KEY, page_scope)] = entry["id"]
    st.session_state[prompt_edit_key(entry["id"])] = entry["prompt"]


def cancel_prompt_edit(entry_id: str, page_scope: str = "comparison") -> None:
    editing_key = _scoped_key(EDITING_PROMPT_KEY, page_scope)
    if st.session_state.get(editing_key) == entry_id:
        st.session_state.pop(editing_key, None)
    st.session_state.pop(prompt_edit_key(entry_id), None)


def queue_prompt_resubmit(entry: dict, revised_prompt: str) -> None:
    """Queue one edited prompt to run on the next complete Streamlit pass."""
    prompt = revised_prompt.strip()
    if not prompt:
        return
    page_scope = _entry_scope(entry)
    st.session_state[_scoped_key(RESUBMIT_REQUEST_KEY, page_scope)] = {
        "source_entry": copy.deepcopy(entry),
        "prompt": prompt,
    }
    st.session_state[current_prompt_key(page_scope)] = prompt
    st.session_state[_scoped_key(ACTIVE_HISTORY_KEY, page_scope)] = NEW_CHAT_SENTINEL
    cancel_prompt_edit(entry["id"], page_scope)


def pending_prompt_resubmit(page_scope: str = "comparison") -> dict | None:
    return st.session_state.get(_scoped_key(RESUBMIT_REQUEST_KEY, page_scope))


def consume_prompt_resubmit(page_scope: str = "comparison") -> dict | None:
    return st.session_state.pop(_scoped_key(RESUBMIT_REQUEST_KEY, page_scope), None)


def start_new_chat(page_scope: str = "comparison") -> None:
    editing_key = _scoped_key(EDITING_PROMPT_KEY, page_scope)
    editing_entry_id = st.session_state.pop(editing_key, None)
    if editing_entry_id:
        st.session_state.pop(prompt_edit_key(editing_entry_id), None)
    counter_key = _scoped_key(CHAT_COUNTER_KEY, page_scope)
    st.session_state[counter_key] = st.session_state.get(counter_key, 0) + 1
    st.session_state[_scoped_key(ACTIVE_HISTORY_KEY, page_scope)] = NEW_CHAT_SENTINEL
    st.session_state.pop(_scoped_key(CURRENT_ENTRY_KEY, page_scope), None)
    st.session_state.pop(_scoped_key(RESUBMIT_REQUEST_KEY, page_scope), None)
    reset_session_cost()


def current_comparison_entry(page_scope: str = "comparison") -> dict | None:
    return st.session_state.get(_scoped_key(CURRENT_ENTRY_KEY, page_scope))


def _history(page_scope: str = "comparison") -> list[dict]:
    history_key = _scoped_key(HISTORY_KEY, page_scope)
    if history_key not in st.session_state:
        st.session_state[history_key] = []
    return st.session_state[history_key]


def history_entries(page_scope: str = "comparison") -> list[dict]:
    """Return the current run nodes, newest first."""
    return _history(page_scope)


def history_tree_rows(entries: list[dict]) -> list[tuple[dict, int]]:
    """Lay out legacy and parent-linked entries as root-to-leaf tree rows."""
    ordered = list(reversed(entries))
    by_id = {entry.get("id"): entry for entry in ordered if entry.get("id")}
    children: dict[str, list[dict]] = {entry_id: [] for entry_id in by_id}
    roots: list[dict] = []
    for entry in ordered:
        entry_id = entry.get("id")
        parent_id = entry.get("parent_id")
        if parent_id in by_id and parent_id != entry_id:
            children[parent_id].append(entry)
        else:
            roots.append(entry)

    rows: list[tuple[dict, int]] = []
    visited: set[str] = set()

    def visit(entry: dict, depth: int) -> None:
        entry_id = entry.get("id")
        if not entry_id or entry_id in visited:
            return
        visited.add(entry_id)
        rows.append((entry, depth))
        for child in children.get(entry_id, []):
            visit(child, depth + 1)

    for root in roots:
        visit(root, 0)
    for entry in ordered:
        visit(entry, 0)
    return rows


def _tree_label(entry: dict, depth: int) -> str:
    marker = "●" if depth == 0 else f"{'  ' * (depth - 1)}↳"
    branch = " ↺" if entry.get("branch_source_id") else ""
    return f"{marker}{branch} {_short_prompt(entry.get('prompt', 'Untitled'))}"


def _states_run_status(states: dict[str, dict]) -> str:
    """Collapse architecture states into one terminal history status."""
    values = list(states.values())
    statuses = {str(state.get("status", "")).casefold() for state in values}
    if "stopped" in statuses:
        return "Stopped"
    if statuses & {"error", "failed"} or any(state.get("errors") for state in values):
        return "Failed"
    if values and all(state.get("done") for state in values):
        return "Completed"
    return "Saved"


def _entry_run_status(entry: dict) -> str:
    status = entry.get("run_status")
    if status in {"Completed", "Stopped", "Failed", "Saved"}:
        return status
    return _states_run_status(entry.get("states", {}))


def history_tree_html(rows: list[tuple[dict, int]], selected_id: str) -> str:
    """Render flattened traversal rows as a semantic, connected tree."""
    if not rows:
        return '<div class="conversation-tree-map empty">No runs yet</div>'

    # Each stack item contains a node and the children collected beneath it.
    # Treat malformed depth jumps as one level so legacy session data still renders.
    forest: list[dict] = []
    stack: list[dict] = []
    for entry, requested_depth in rows:
        depth = max(0, min(requested_depth, len(stack)))
        node = {"entry": entry, "depth": depth, "children": []}
        if depth == 0:
            forest.append(node)
        else:
            stack[depth - 1]["children"].append(node)
        stack[depth:] = [node]

    def render_node(node: dict, parent_entry: dict | None = None) -> str:
        entry = node["entry"]
        depth = node["depth"]
        entry_id = entry.get("id")
        run_status = _entry_run_status(entry)
        classes = "conversation-tree-node"
        selected = entry_id == selected_id
        if selected:
            classes += " selected"
        if entry.get("branch_source_id"):
            classes += " branch"

        label = html.escape(_short_prompt(entry.get("prompt", "Untitled"), limit=42))
        if entry.get("branch_source_id"):
            kind = "Revision branch"
        elif depth == 0:
            kind = "Root"
        else:
            kind = f"Turn {depth + 1}"
        continued = (
            '<span class="conversation-tree-relation">Continued after stop</span>'
            if parent_entry and _entry_run_status(parent_entry) == "Stopped"
            else ""
        )
        status_class = run_status.casefold()
        status = (
            f'<span class="conversation-tree-status {status_class}">{run_status}</span>'
        )
        current = '<span class="conversation-tree-current">Current</span>' if selected else ""
        children = node["children"]
        child_html = ""
        if children:
            child_html = (
                '<ul class="conversation-tree-children" role="group">'
                f'{"".join(render_node(child, entry) for child in children)}</ul>'
            )
        aria_current = ' aria-current="true"' if selected else ""
        return (
            '<li class="conversation-tree-item" role="treeitem">'
            f'<button type="button" class="{classes}" '
            f'data-node-id="{html.escape(str(entry_id or ""))}"{aria_current}>'
            '<span class="conversation-tree-marker" aria-hidden="true"></span>'
            '<span class="conversation-tree-copy">'
            f'<span class="conversation-tree-label">{label}</span>'
            f'<span class="conversation-tree-meta">{kind}{continued}{status}{current}</span>'
            "</span></button>"
            f"{child_html}</li>"
        )

    tree = "".join(render_node(node) for node in forest)
    return (
        '<div class="conversation-tree-map">'
        f'<ul class="conversation-tree-roots" role="tree" aria-label="Conversation branches">'
        f"{tree}</ul></div>"
    )


def _short_prompt(prompt: str, limit: int = 54) -> str:
    compact = " ".join(prompt.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}..."


def _branch_selector_options(ids: list[str], current_entry: dict | None) -> list[str]:
    """Show the synthetic current selection without duplicating its saved node."""
    current_id = current_entry.get("id") if current_entry else None
    return [NEW_CHAT_SENTINEL] + [entry_id for entry_id in ids if entry_id != current_id]


def _current_branch_label(current_entry: dict | None) -> str:
    if not current_entry:
        return "New chat"
    prompt = _short_prompt(current_entry.get("prompt", "Untitled"), limit=42)
    return f"Current branch · {prompt}"


def _validated_tree_selection(
    selection: object, valid_ids: set[str]
) -> tuple[str, str] | None:
    """Validate the small, untrusted selection event returned by the browser."""
    if not isinstance(selection, dict):
        return None
    entry_id = selection.get("id")
    event_id = selection.get("event")
    if not isinstance(entry_id, str) or entry_id not in valid_ids:
        return None
    if not isinstance(event_id, str) or not event_id or len(event_id) > 80:
        return None
    return entry_id, event_id


def add_history_entry(
    prompt: str,
    options: dict,
    states: dict[str, dict],
    *,
    entry_id: str | None = None,
    parent_id: str | None = None,
    branch_source_id: str | None = None,
    ledger_id: str | None = None,
) -> dict:
    page_scope = str(options.get("_page_scope") or "comparison")
    history = _history(page_scope)
    if entry_id:
        existing = next((entry for entry in history if entry["id"] == entry_id), None)
        if existing is not None:
            return existing
    created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    frozen_states = snapshot_states(states)
    entry = {
        "id": entry_id or f"run-{time.time_ns()}",
        "label": f"{created_at} | {_short_prompt(prompt)}",
        "created_at": created_at,
        "prompt": prompt,
        "parent_id": parent_id,
        "branch_source_id": branch_source_id,
        "ledger_id": ledger_id,
        "run_status": _states_run_status(frozen_states),
        "options": dict(options),
        "states": frozen_states,
    }
    history.insert(0, entry)
    del history[MAX_HISTORY_ITEMS:]
    st.session_state[_scoped_key(HISTORY_KEY, page_scope)] = history
    st.session_state[_scoped_key(CURRENT_ENTRY_KEY, page_scope)] = entry
    st.session_state[_scoped_key(ACTIVE_HISTORY_KEY, page_scope)] = NEW_CHAT_SENTINEL
    return entry


def render_history_selector(page_scope: str = "comparison") -> dict | None:
    history = _history(page_scope)
    st.subheader("Conversation tree")
    if not history:
        st.caption("No runs in this session yet.")
        return None

    tree_rows = history_tree_rows(history)
    ids = [entry["id"] for entry, _depth in tree_rows]
    labels = {entry["id"]: _tree_label(entry, depth) for entry, depth in tree_rows}
    depths = {entry["id"]: depth for entry, depth in tree_rows}
    current_entry = current_comparison_entry(page_scope)
    options = _branch_selector_options(ids, current_entry)
    active_history_key = _scoped_key(ACTIVE_HISTORY_KEY, page_scope)
    branch_selector_key = _scoped_key(BRANCH_SELECTOR_KEY, page_scope)
    tree_event_key = _scoped_key(TREE_SELECTION_EVENT_KEY, page_scope)
    selected_id = st.session_state.get(active_history_key, NEW_CHAT_SENTINEL)
    if selected_id not in options:
        selected_id = NEW_CHAT_SENTINEL
        st.session_state[active_history_key] = selected_id
    current_label = _current_branch_label(current_entry)
    selector_slot = st.empty()
    preview_selected_id = (
        current_entry["id"] if selected_id == NEW_CHAT_SENTINEL and current_entry else selected_id
    )
    tree_result = _conversation_tree_component(
        key=f"comparison_conversation_tree_{page_scope}",
        data={"html": history_tree_html(tree_rows, preview_selected_id)},
        default={"selection": None},
        on_selection_change=lambda: None,
    )
    tree_selection = _validated_tree_selection(
        getattr(tree_result, "selection", None), set(ids)
    )
    if tree_selection:
        clicked_id, event_id = tree_selection
        if event_id != st.session_state.get(tree_event_key):
            st.session_state[tree_event_key] = event_id
            st.session_state[active_history_key] = (
                NEW_CHAT_SENTINEL
                if current_entry and clicked_id == current_entry.get("id")
                else clicked_id
            )
            st.rerun()

    with selector_slot:
        st.session_state[branch_selector_key] = selected_id
        selected_id = st.selectbox(
            "Branch",
            options,
            format_func=lambda entry_id: (
                current_label if entry_id == NEW_CHAT_SENTINEL else labels[entry_id]
            ),
            help=(
                "Select any node to review it or continue from it. Selecting an earlier "
                "node and submitting creates a new branch."
            ),
            key=branch_selector_key,
        )
    st.session_state[active_history_key] = selected_id

    if selected_id != NEW_CHAT_SENTINEL:
        depth = depths[selected_id]
        st.caption(
            f"Selected turn {depth + 1}. A new prompt continues from this node; editing "
            "creates a sibling branch."
        )

    if st.button("Clear conversation tree"):
        st.session_state[_scoped_key(HISTORY_KEY, page_scope)] = []
        st.session_state[active_history_key] = NEW_CHAT_SENTINEL
        st.session_state.pop(_scoped_key(CURRENT_ENTRY_KEY, page_scope), None)
        st.session_state.pop(_scoped_key(EDITING_PROMPT_KEY, page_scope), None)
        st.session_state.pop(_scoped_key(RESUBMIT_REQUEST_KEY, page_scope), None)
        st.session_state.pop(tree_event_key, None)
        st.rerun()
    if selected_id == NEW_CHAT_SENTINEL:
        return None
    return next(entry for entry in history if entry["id"] == selected_id)
