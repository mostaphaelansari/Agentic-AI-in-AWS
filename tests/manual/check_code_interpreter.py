"""Live CSV Code Interpreter smoke test for both AWS agent architectures.

Run from the repository root with AWS credentials available:
    .venv/bin/python tests/manual/check_code_interpreter.py

The script stages a tiny CSV once, sends the same presigned reference to both
agents, and requires a Code Interpreter call, the correct mean, and a compact
Vega-Lite payload. The staged S3 object is deleted when the run finishes.
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
CSV_BYTES = b"month,value\nJan,10\nFeb,20\n"
PROMPT = "Use the code interpreter to calculate the mean value and return a bar chart."


def load_modules():
    sys.path.insert(0, str(COMPARISON_APP))
    from backends.harness import invoke_harness
    from backends.runtime import invoke_runtime
    from config import ARCHITECTURES
    from services.uploads import prepare_csv_upload, prompt_with_csv, staged_csv_upload

    return (
        ARCHITECTURES,
        invoke_harness,
        invoke_runtime,
        prepare_csv_upload,
        prompt_with_csv,
        staged_csv_upload,
    )


def session_id() -> str:
    return f"code-csv-{uuid.uuid4().hex}-{uuid.uuid4().hex}"


def summarize(event_queue: queue.Queue) -> dict[str, Any]:
    items: list[tuple[str, Any]] = []
    while not event_queue.empty():
        items.append(event_queue.get_nowait())

    events = [payload for kind, payload in items if kind == "event" and isinstance(payload, dict)]
    tool_names = [
        str(event.get("name", "")) for event in events if event.get("type") == "tool_start"
    ]
    tool_results = [
        str(event.get("result", "")) for event in events if event.get("type") == "tool_end"
    ]
    errors = [str(event.get("content", "")) for event in events if event.get("type") == "error"]
    answer = "".join(
        str(event.get("content", "")) for event in events if event.get("type") == "text"
    )
    combined = "\n".join([answer, *tool_results])
    elapsed = [payload for kind, payload in items if kind == "elapsed"]

    return {
        "tool_names": tool_names,
        "code_interpreter_called": any(
            name.rsplit("___", 1)[-1] == "code_interpreter" for name in tool_names
        ),
        "correct_mean": "15.0" in combined or "**15**" in combined,
        "chart_returned": "__VEGA_LITE__:" in combined,
        "errors": errors,
        "answer_preview": answer[:240],
        "elapsed_seconds": elapsed[-1] if elapsed else None,
    }


def invoke(
    backend: Callable,
    config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    event_queue: queue.Queue = queue.Queue()
    backend(
        config,
        session_id(),
        prompt,
        event_queue,
        {"enable_caching": False, "enable_thinking": False},
    )
    return summarize(event_queue)


def main() -> int:
    (
        architectures,
        invoke_harness,
        invoke_runtime,
        prepare_csv_upload,
        prompt_with_csv,
        staged_csv_upload,
    ) = load_modules()
    upload = prepare_csv_upload("metrics.csv", CSV_BYTES)
    checks = {
        "custom": (invoke_runtime, architectures["Custom agent"]),
        "managed": (invoke_harness, architectures["Managed agent"]),
    }
    results: dict[str, dict[str, Any]] = {}

    try:
        with staged_csv_upload(upload) as staged:
            prompt = prompt_with_csv(PROMPT, staged)
            for name, (backend, config) in checks.items():
                try:
                    results[name] = invoke(backend, config, prompt)
                except Exception as exc:
                    results[name] = {
                        "tool_names": [],
                        "code_interpreter_called": False,
                        "correct_mean": False,
                        "chart_returned": False,
                        "errors": [repr(exc)],
                        "answer_preview": "",
                        "elapsed_seconds": None,
                    }
                print(json.dumps({name: results[name]}, indent=2, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"staging_error": repr(exc)}, indent=2))
        return 1

    failed = [
        name
        for name, result in results.items()
        if result["errors"]
        or not result["code_interpreter_called"]
        or not result["correct_mean"]
        or not result["chart_returned"]
        or not result["answer_preview"]
    ]
    if failed:
        print(f"Code Interpreter smoke test failed: {', '.join(failed)}")
        return 1

    print("Code Interpreter smoke test passed for custom and managed agents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
