"""Comparison-state construction and snapshots."""

from __future__ import annotations

import time
import uuid


def new_session_id() -> str:
    return f"streamlit_demo-{uuid.uuid4().hex}--{uuid.uuid4().hex}"


def new_comparison_state(arch_name: str, config: dict) -> dict:
    return {
        "arch_name": arch_name,
        "config": config,
        "session_id": new_session_id(),
        "started_at": time.time(),
        "status": "Waiting",
        "phase": "pre_tool",
        "answer": "",
        "pending_text": "",
        "reasoning": "",
        "trace": "",
        "tools": [],
        "metrics": {},
        "tokens_available": False,
        "runtime_usage": {},
        "builtin_usage": {},
        "elapsed": 0,
        "done": False,
        "errors": [],
    }


def snapshot_states(states: dict[str, dict]) -> dict[str, dict]:
    return {
        arch_name: {
            "arch_name": state["arch_name"],
            "config": dict(state["config"]),
            "session_id": state["session_id"],
            "started_at": state.get("started_at"),
            "status": state["status"],
            "phase": state["phase"],
            "answer": state["answer"],
            "pending_text": state["pending_text"],
            "reasoning": state.get("reasoning", ""),
            "trace": state["trace"],
            "tools": [dict(tool) for tool in state.get("tools", [])],
            "metrics": dict(state["metrics"]),
            "tokens_available": state.get(
                "tokens_available", bool(state.get("metrics", {}).get("usage"))
            ),
            "runtime_usage": dict(state.get("runtime_usage", {})),
            "builtin_usage": dict(state.get("builtin_usage", {})),
            "elapsed": state["elapsed"],
            "done": state["done"],
            "errors": list(state["errors"]),
        }
        for arch_name, state in states.items()
    }
