"""Manual diagnostic for AgentCore thinking events.

Run from the repository root:
    .venv/bin/python tests/manual/check_thinking_events.py

This makes real AWS AgentCore calls. If AWS SSO credentials are expired, run:
    aws sso login
"""

from __future__ import annotations

import pathlib
import queue
import sys
import uuid
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPARISON_APP = REPO_ROOT / "apps" / "comparison"
PROMPT = "Find X, X**2 +4X +4 =0"


def load_backend_modules():
    sys.path.insert(0, str(COMPARISON_APP))
    from backends.harness import invoke_harness
    from backends.runtime import invoke_runtime
    from config import ARCHITECTURES

    return ARCHITECTURES, invoke_harness, invoke_runtime


def drain(event_queue: queue.Queue) -> dict[str, Any]:
    items: list[tuple[str, Any]] = []
    while not event_queue.empty():
        items.append(event_queue.get_nowait())

    events = [payload for kind, payload in items if kind == "event" and isinstance(payload, dict)]
    reasoning = [event for event in events if event.get("type") == "reasoning"]
    text = [event for event in events if event.get("type") == "text"]
    errors = [event for event in events if event.get("type") == "error"]
    metrics = [event for event in events if event.get("type") == "metrics"]
    elapsed = [payload for kind, payload in items if kind == "elapsed"]

    return {
        "reasoning_events": len(reasoning),
        "text_events": len(text),
        "errors": [event.get("content") for event in errors],
        "usage": metrics[-1].get("usage") if metrics else None,
        "elapsed": elapsed[-1] if elapsed else None,
        "reasoning_preview": [event.get("content", "")[:80] for event in reasoning[:3]],
        "answer_preview": "".join(event.get("content", "") for event in text)[:160],
    }


def session_id() -> str:
    return f"test-{uuid.uuid4().hex}-{uuid.uuid4().hex}"


def run_custom(
    architectures,
    invoke_runtime,
    enable_thinking: bool,
) -> dict[str, Any]:
    event_queue: queue.Queue = queue.Queue()
    sid = session_id()
    invoke_runtime(
        architectures["Custom agent"],
        sid,
        PROMPT,
        event_queue,
        {
            "enable_caching": True,
            "enable_thinking": enable_thinking,
        },
    )
    result = drain(event_queue)
    result["session_id"] = sid
    result["requested_thinking"] = enable_thinking
    return result


def run_managed(
    architectures,
    invoke_harness,
    enable_thinking: bool,
) -> dict[str, Any]:
    event_queue: queue.Queue = queue.Queue()
    sid = session_id()
    invoke_harness(
        architectures["Managed agent"],
        sid,
        PROMPT,
        event_queue,
        {
            "enable_caching": False,
            "enable_thinking": enable_thinking,
        },
    )
    result = drain(event_queue)
    result["session_id"] = sid
    result["requested_thinking"] = enable_thinking
    return result


def print_result(label: str, result: dict[str, Any]) -> None:
    print(f"\n== {label} ==")
    for key in [
        "session_id",
        "requested_thinking",
        "reasoning_events",
        "text_events",
        "errors",
        "usage",
        "elapsed",
        "reasoning_preview",
        "answer_preview",
    ]:
        print(f"{key}: {result.get(key)}")


def main() -> int:
    architectures, invoke_harness, invoke_runtime = load_backend_modules()

    checks = [
        (
            "CUSTOM_THINKING_ON",
            lambda: run_custom(architectures, invoke_runtime, True),
        ),
        (
            "CUSTOM_THINKING_OFF",
            lambda: run_custom(architectures, invoke_runtime, False),
        ),
        (
            "MANAGED_THINKING_ON",
            lambda: run_managed(architectures, invoke_harness, True),
        ),
    ]

    results: dict[str, dict[str, Any]] = {}
    for label, check in checks:
        try:
            results[label] = check()
        except Exception as exc:
            results[label] = {"exception": repr(exc)}
        print_result(label, results[label])

    print("\n== Verdict ==")
    custom_on = results.get("CUSTOM_THINKING_ON", {})
    custom_off = results.get("CUSTOM_THINKING_OFF", {})
    managed_on = results.get("MANAGED_THINKING_ON", {})

    if any(result.get("errors") or result.get("exception") for result in results.values()):
        print("At least one call failed. If this is an AWS SSO error, run: aws sso login")
        return 2

    print(f"Custom thinking on emitted reasoning: {custom_on.get('reasoning_events', 0) > 0}")
    print(f"Custom thinking off emitted reasoning: {custom_off.get('reasoning_events', 0) > 0}")
    print(f"Managed thinking on emitted reasoning: {managed_on.get('reasoning_events', 0) > 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
