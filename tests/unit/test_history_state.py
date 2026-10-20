"""Tests for comparison history and current-chat state."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from components import history  # noqa: E402


class _FakeStreamlit:
    def __init__(self):
        self.session_state = {}


def _state() -> dict:
    return {
        "Managed agent": {
            "arch_name": "Managed agent",
            "config": {"region": "eu-west-1"},
            "session_id": "streamlit_demo-managed",
            "started_at": 1,
            "status": "Done",
            "phase": "post_tool",
            "answer": "managed answer",
            "pending_text": "",
            "reasoning": "I should compare the available tools.",
            "trace": "",
            "tools": [
                {
                    "id": "tool-1",
                    "name": "webSearch",
                    "input": {"query": "Berlin"},
                    "result": "Found Berlin",
                }
            ],
            "metrics": {},
            "elapsed": 1.2,
            "done": True,
            "errors": [],
        }
    }


def test_completed_run_stays_current_instead_of_selecting_history(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(history, "st", fake_st)

    entry = history.add_history_entry("hello", {"enable_thinking": False}, _state())

    assert fake_st.session_state[history.ACTIVE_HISTORY_KEY] == history.NEW_CHAT_SENTINEL
    assert history.current_comparison_entry() == entry
    assert fake_st.session_state[history.HISTORY_KEY][0] == entry
    assert entry["states"]["Managed agent"]["reasoning"] == (
        "I should compare the available tools."
    )
    assert entry["states"]["Managed agent"]["tools"][0]["name"] == "webSearch"
    assert entry["parent_id"] is None
    assert entry["branch_source_id"] is None
    assert entry["ledger_id"] is None
    assert entry["run_status"] == "Completed"


def test_new_chat_clears_current_result(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(history, "st", fake_st)
    history.add_history_entry("hello", {"enable_thinking": False}, _state())

    history.start_new_chat()

    assert history.current_comparison_entry() is None
    assert fake_st.session_state[history.ACTIVE_HISTORY_KEY] == history.NEW_CHAT_SENTINEL


def test_edit_queue_preserves_source_snapshot_and_prefills_prompt(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(history, "st", fake_st)
    entry = history.add_history_entry("hello", {"enable_thinking": False}, _state())

    history.begin_prompt_edit(entry)
    history.queue_prompt_resubmit(entry, "  revised hello  ")
    entry["prompt"] = "mutated later"

    request = history.pending_prompt_resubmit()
    assert request["prompt"] == "revised hello"
    assert request["source_entry"]["prompt"] == "hello"
    assert fake_st.session_state[history.current_prompt_key()] == "revised hello"
    assert history.EDITING_PROMPT_KEY not in fake_st.session_state
    assert history.consume_prompt_resubmit() == request
    assert history.pending_prompt_resubmit() is None


def test_new_chat_cancels_queued_revision(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(history, "st", fake_st)
    entry = history.add_history_entry("hello", {"enable_thinking": False}, _state())
    history.queue_prompt_resubmit(entry, "revised")

    history.start_new_chat()

    assert history.pending_prompt_resubmit() is None


def test_custom_page_history_is_isolated_from_comparison(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(history, "st", fake_st)

    comparison_entry = history.add_history_entry("compare", {}, _state())
    custom_entry = history.add_history_entry(
        "custom",
        {"_page_scope": "custom"},
        _state(),
    )

    assert history.history_entries() == [comparison_entry]
    assert history.history_entries("custom") == [custom_entry]
    assert history.current_comparison_entry() == comparison_entry
    assert history.current_comparison_entry("custom") == custom_entry


def test_history_tree_preserves_parent_child_and_sibling_branches(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(history, "st", fake_st)
    root = history.add_history_entry(
        "Plan Nice",
        {},
        _state(),
        entry_id="root",
    )
    hotels = history.add_history_entry(
        "Add hotels",
        {},
        _state(),
        entry_id="hotels",
        parent_id=root["id"],
    )
    trains = history.add_history_entry(
        "Add trains instead",
        {},
        _state(),
        entry_id="trains",
        parent_id=root["id"],
        branch_source_id=hotels["id"],
    )

    rows = history.history_tree_rows(history.history_entries())

    assert [(entry["id"], depth) for entry, depth in rows] == [
        ("root", 0),
        ("hotels", 1),
        ("trains", 1),
    ]
    assert history._tree_label(trains, 1).startswith("↳ ↺")


def test_orphaned_legacy_node_is_rendered_as_a_root() -> None:
    orphan = {"id": "orphan", "parent_id": "pruned", "prompt": "legacy"}

    assert history.history_tree_rows([orphan]) == [(orphan, 0)]


def test_tree_preview_marks_selection_and_escapes_prompt() -> None:
    entry = {"id": "node", "prompt": "<script>alert(1)</script>"}

    rendered = history.history_tree_html([(entry, 0)], "node")

    assert 'class="conversation-tree-node selected"' in rendered
    assert '<button type="button"' in rendered
    assert 'data-node-id="node"' in rendered
    assert 'aria-current="true"' in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert 'role="tree"' in rendered
    assert "Root" in rendered


def test_tree_preview_renders_nested_production_topology() -> None:
    root = {"id": "root", "prompt": "Plan launch"}
    deploy = {"id": "deploy", "prompt": "Deploy safely"}
    rollback = {
        "id": "rollback",
        "prompt": "Prepare rollback",
        "branch_source_id": "deploy",
    }
    monitor = {"id": "monitor", "prompt": "Monitor rollout"}

    rendered = history.history_tree_html(
        [(root, 0), (deploy, 1), (monitor, 2), (rollback, 1)],
        "rollback",
    )

    assert rendered.count('class="conversation-tree-children"') == 2
    assert rendered.count('role="treeitem"') == 4
    assert 'class="conversation-tree-node selected branch"' in rendered
    assert "Revision branch" in rendered
    assert "Current" in rendered


def test_stopped_run_and_its_continuation_are_visible_in_tree() -> None:
    stopped_states = _state()
    stopped_states["Managed agent"]["status"] = "Stopped"
    root = {"id": "stopped", "prompt": "Research launch", "states": stopped_states}
    continued = {
        "id": "continued",
        "prompt": "Carry on from there",
        "states": _state(),
    }

    rendered = history.history_tree_html([(root, 0), (continued, 1)], "continued")

    assert '<span class="conversation-tree-status stopped">Stopped</span>' in rendered
    assert "Continued after stop" in rendered
    assert '<span class="conversation-tree-status completed">Completed</span>' in rendered
    assert rendered.index("Research launch") < rendered.index("Carry on from there")


def test_any_stopped_architecture_marks_the_whole_run_stopped() -> None:
    states = _state()
    states["Custom agent"] = copy.deepcopy(states["Managed agent"])
    states["Custom agent"]["arch_name"] = "Custom agent"
    states["Custom agent"]["status"] = "Stopped"

    assert history._states_run_status(states) == "Stopped"


def test_current_branch_is_named_once_in_selector() -> None:
    ids = ["root", "older", "current"]
    current = {"id": "current", "prompt": "is there any restaurant nearby?"}

    options = history._branch_selector_options(ids, current)

    assert options == [history.NEW_CHAT_SENTINEL, "root", "older"]
    assert history._current_branch_label(current) == (
        "Current branch · is there any restaurant nearby?"
    )


def test_branch_selector_has_new_chat_without_a_current_run() -> None:
    assert history._branch_selector_options(["root"], None) == [
        history.NEW_CHAT_SENTINEL,
        "root",
    ]
    assert history._current_branch_label(None) == "New chat"


def test_tree_selection_accepts_only_known_history_nodes() -> None:
    assert history._validated_tree_selection(
        {"id": "turn-2", "event": "123-1"}, {"root", "turn-2"}
    ) == ("turn-2", "123-1")
    assert (
        history._validated_tree_selection(
            {"id": "unknown", "event": "123-2"}, {"root", "turn-2"}
        )
        is None
    )
    assert history._validated_tree_selection({"id": "root"}, {"root"}) is None
