"""Live smoke test for semantic skill selection in both AWS agents.

Run from the repository root:
    .venv/bin/python tests/manual/check_semantic_skills.py

The prompt intentionally avoids the old weather-skill keywords. A successful
run must show that each agent calls its ``skills`` tool before answering.
"""

from __future__ import annotations

import json
import pathlib
import queue
import sys
import uuid
from typing import Any, Callable

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPARISON_APP = REPO_ROOT / "apps" / "comparison"
PROMPT = "Should I carry an umbrella in Lyon on Saturday?"


def load_backend_modules():
    sys.path.insert(0, str(COMPARISON_APP))
    from backends.harness import invoke_harness
    from backends.runtime import invoke_runtime
    from config import ARCHITECTURES

    return ARCHITECTURES, invoke_harness, invoke_runtime


def session_id() -> str:
    return f"semantic-{uuid.uuid4().hex}-{uuid.uuid4().hex}"


def summarize(event_queue: queue.Queue) -> dict[str, Any]:
    items: list[tuple[str, Any]] = []
    while not event_queue.empty():
        items.append(event_queue.get_nowait())

    events = [payload for kind, payload in items if kind == "event" and isinstance(payload, dict)]
    tool_names = [
        str(event.get("name", "")) for event in events if event.get("type") == "tool_start"
    ]
    errors = [str(event.get("content", "")) for event in events if event.get("type") == "error"]
    answer = "".join(
        str(event.get("content", "")) for event in events if event.get("type") == "text"
    )
    elapsed = [payload for kind, payload in items if kind == "elapsed"]

    return {
        "tool_names": tool_names,
        "skill_selected": any("skill" in name.casefold() for name in tool_names),
        "errors": errors,
        "answer_preview": answer[:240],
        "elapsed_seconds": elapsed[-1] if elapsed else None,
    }


def invoke(
    backend: Callable,
    config: dict[str, Any],
) -> dict[str, Any]:
    event_queue: queue.Queue = queue.Queue()
    backend(
        config,
        session_id(),
        PROMPT,
        event_queue,
        {"enable_caching": False, "enable_thinking": False},
    )
    return summarize(event_queue)


def main() -> int:
    architectures, invoke_harness, invoke_runtime = load_backend_modules()
    checks = {
        "custom": (invoke_runtime, architectures["Custom agent"]),
        "managed": (invoke_harness, architectures["Managed agent"]),
    }
    results: dict[str, dict[str, Any]] = {}

    for name, (backend, config) in checks.items():
        try:
            results[name] = invoke(backend, config)
        except Exception as exc:
            results[name] = {
                "tool_names": [],
                "skill_selected": False,
                "errors": [repr(exc)],
                "answer_preview": "",
                "elapsed_seconds": None,
            }
        print(json.dumps({name: results[name]}, indent=2, ensure_ascii=False))

    failed = [
        name
        for name, result in results.items()
        if result["errors"] or not result["skill_selected"] or not result["answer_preview"]
    ]
    if failed:
        print(f"Semantic skill smoke test failed: {', '.join(failed)}")
        return 1

    print("Semantic skill smoke test passed for custom and managed agents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
