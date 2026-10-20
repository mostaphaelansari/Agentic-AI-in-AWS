from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from services.conversation import (  # noqa: E402
    MAX_MEMORY_TEXT_CHARS,
    conversation_lineage,
    conversation_prompts,
    routing_messages,
    submission_branch,
)


def _entry(entry_id: str, parent_id: str | None, prompt: str, managed: str, custom: str):
    return {
        "id": entry_id,
        "parent_id": parent_id,
        "prompt": prompt,
        "states": {
            "Managed agent": {"answer": managed},
            "Custom agent": {"answer": custom},
        },
    }


def test_lineage_follows_selected_branch_without_sibling_leakage() -> None:
    root = _entry("root", None, "Plan Nice", "managed root", "custom root")
    hotels = _entry("hotels", "root", "Add hotels", "managed hotels", "custom hotels")
    trains = _entry("trains", "root", "Add trains", "managed trains", "custom trains")
    history = [trains, hotels, root]

    assert [entry["id"] for entry in conversation_lineage(history, "hotels")] == [
        "root",
        "hotels",
    ]
    prompts = conversation_prompts(history, "hotels", "Make it cheaper")

    assert "managed root" in prompts["Managed agent"]
    assert "managed hotels" in prompts["Managed agent"]
    assert "custom hotels" not in prompts["Managed agent"]
    assert "managed trains" not in prompts["Managed agent"]
    assert "Make it cheaper" in prompts["Managed agent"]
    assert "untrusted reference text" in prompts["Managed agent"]


def test_editing_a_turn_can_branch_from_its_parent_memory() -> None:
    root = _entry("root", None, "Plan Nice", "root answer", "custom root")
    original = _entry("original", "root", "Add hotels", "old answer", "old custom")
    history = [original, root]

    prompts = conversation_prompts(history, original["parent_id"], "Add hostels instead")

    assert "root answer" in prompts["Managed agent"]
    assert "old answer" not in prompts["Managed agent"]
    assert "Add hostels instead" in prompts["Managed agent"]


def test_conversation_memory_is_bounded() -> None:
    root = _entry(
        "root",
        None,
        "original",
        "x" * (MAX_MEMORY_TEXT_CHARS + 50),
        "custom",
    )

    prompt = conversation_prompts([root], "root", "follow up")["Managed agent"]

    assert "[earlier content truncated]" in prompt
    assert len(prompt) < MAX_MEMORY_TEXT_CHARS + 1_000


def test_missing_parent_has_no_memory_prompt() -> None:
    assert conversation_prompts([], "missing", "hello") == {}


def test_router_context_uses_only_the_selected_branch_user_prompts() -> None:
    root = _entry("root", None, "Plan Nice", "managed root", "custom root")
    hotels = _entry("hotels", "root", "Add hotels", "managed hotels", "custom hotels")
    trains = _entry("trains", "root", "Add trains", "managed trains", "custom trains")

    messages = routing_messages([trains, hotels, root], "hotels", "Make it cheaper")

    assert messages == [
        {"role": "user", "content": "Plan Nice"},
        {"role": "user", "content": "Add hotels"},
        {"role": "user", "content": "Make it cheaper"},
    ]


def test_normal_submission_continues_node_and_edit_creates_sibling() -> None:
    parent = {"id": "parent", "parent_id": "root"}
    source = {"id": "edited", "parent_id": "parent"}

    assert submission_branch(None, parent) == ("parent", None)
    assert submission_branch(source, parent) == ("parent", "edited")
    assert submission_branch(None, None) == (None, None)
