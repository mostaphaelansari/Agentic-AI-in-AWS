"""Accumulate per-run cost into a running total for the whole chat session.

`render_run_cost` prices one run; this prices the session. Every figure is
built from the same measured inputs (token usage from the model stream, tool
invocations from the event stream, Runtime compute from vended USAGE_LOGS) and
the same list rates, so the session total is exactly the sum of the run tables.

Runs are held in their own accumulator rather than derived from history: the
history list is capped, and a session total must not shrink when an old run
scrolls off it. Runtime compute arrives asynchronously, so its per-run slot is
patched in place when a USAGE_LOGS lookup finally lands.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import streamlit as st
from formatting.metrics import normalized_usage

from services.builtin_usage import (
    BUILTIN_USAGE_AVAILABLE,
    fetch_builtin_usage_for_session,
)
from services.cost import (
    builtin_tool_calls,
    builtin_tool_kinds,
    cache_savings,
    gateway_cost,
    gateway_operations,
    inference_cost,
)
from services.runtime_usage import RUNTIME_USAGE_AVAILABLE, fetch_compute_usage_for_session
from services.token_usage import TOKEN_USAGE_AVAILABLE, fetch_token_usage_for_session

SESSION_COST_KEY = "comparison_session_cost"
LEDGER_ID_KEY = "comparison_cost_ledger_id"
LEDGER_QUERY_PARAM = "cost_session"
LEDGER_DB_ENV = "SESSION_COST_DB_PATH"


def _db_path() -> Path:
    return Path(os.environ.get(LEDGER_DB_ENV, "var/session-costs.sqlite3"))


def _ledger_id() -> str:
    if existing := st.session_state.get(LEDGER_ID_KEY):
        return str(existing)
    candidate = ""
    try:
        candidate = str(st.query_params.get(LEDGER_QUERY_PARAM, ""))
    except Exception:
        pass
    if not candidate or len(candidate) > 64 or not candidate.replace("-", "").isalnum():
        candidate = uuid.uuid4().hex
    st.session_state[LEDGER_ID_KEY] = candidate
    try:
        st.query_params[LEDGER_QUERY_PARAM] = candidate
    except Exception:
        pass
    return candidate


def current_ledger_id() -> str:
    """Return the durable chat identifier shared with related UI records."""
    return _ledger_id()


def _load_durable_store() -> dict[str, list[dict[str, Any]]]:
    path = _db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS session_cost_ledger "
                "(ledger_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            row = connection.execute(
                "SELECT payload FROM session_cost_ledger WHERE ledger_id = ?",
                (f"comparison:{_ledger_id()}",),
            ).fetchone()
        return json.loads(row[0]) if row else {}
    except (OSError, sqlite3.Error, json.JSONDecodeError):
        return {}


def _persist(store: dict[str, list[dict[str, Any]]]) -> None:
    path = _db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS session_cost_ledger "
                "(ledger_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            connection.execute(
                "INSERT INTO session_cost_ledger (ledger_id, payload, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(ledger_id) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (f"comparison:{_ledger_id()}", json.dumps(store), time.time()),
            )
    except (OSError, sqlite3.Error):
        # The live widget still works when a deployment has a read-only disk;
        # it simply cannot promise restart durability until a writable path is configured.
        return


def _store() -> dict[str, list[dict[str, Any]]]:
    """Per-architecture run records for the current chat."""
    if SESSION_COST_KEY not in st.session_state:
        st.session_state[SESSION_COST_KEY] = _load_durable_store()
    return st.session_state[SESSION_COST_KEY]


def reset_session_cost() -> None:
    """Start a new durable ledger without deleting the previous chat's ledger."""
    ledger_id = uuid.uuid4().hex
    st.session_state[LEDGER_ID_KEY] = ledger_id
    try:
        st.query_params[LEDGER_QUERY_PARAM] = ledger_id
    except Exception:
        pass
    st.session_state[SESSION_COST_KEY] = {}
    _persist({})


def _record_from_state(state: dict) -> dict[str, Any]:
    usage = normalized_usage(state.get("metrics", {}).get("usage", {}))
    metrics = state.get("metrics", {})
    model_id = metrics.get("model_id")
    inference = inference_cost(usage, model_id)
    operations = gateway_operations(metrics)
    runtime_usage = state.get("runtime_usage", {}) or {}
    available = runtime_usage.get("status") == RUNTIME_USAGE_AVAILABLE
    builtin_usage = state.get("builtin_usage", {}) or {}
    builtin_calls = builtin_tool_calls(metrics)
    builtin_available = not builtin_calls or builtin_usage.get("status") == BUILTIN_USAGE_AVAILABLE
    tokens_available = state.get("tokens_available")
    if tokens_available is None:
        tokens_available = bool(metrics.get("usage"))
    elapsed = float(state.get("elapsed") or 0.0)
    if not elapsed and not state.get("done") and (started_at := state.get("started_at")):
        # A run in flight has no final latency yet; show the clock it is on.
        elapsed = max(time.time() - float(started_at), 0.0)
    return {
        "session_id": state.get("session_id", ""),
        "model_id": model_id,
        "region": state.get("config", {}).get("region", ""),
        "service": state.get("config", {}).get("service", ""),
        "arn": state.get("config", {}).get("arn", ""),
        "usage_log_group": state.get("config", {}).get("usage_log_group", ""),
        "token_usage_log_group": state.get("config", {}).get("token_usage_log_group", ""),
        "builtin_usage_log_group": state.get("config", {}).get("builtin_usage_log_group", ""),
        "started_at": state.get("started_at"),
        "completed_at": (
            float(state.get("started_at")) + float(state.get("elapsed"))
            if state.get("started_at") and state.get("elapsed")
            else None
        ),
        "elapsed": elapsed,
        "tokens_available": bool(tokens_available),
        "fresh_input": usage["fresh_input"] if tokens_available else None,
        "cache_read": usage["cache_read"] if tokens_available else None,
        "cache_write": usage["cache_write"] if tokens_available else None,
        "output": usage["output"] if tokens_available else None,
        "total_tokens": usage["total"] if tokens_available else None,
        "token_cost": inference["total_cost"] if tokens_available else None,
        "cache_savings": cache_savings(usage, model_id) if tokens_available else None,
        "operations": operations,
        "gateway_cost": gateway_cost(operations),
        "builtin_tool_calls": builtin_calls,
        "builtin_tool_kinds": builtin_tool_kinds(metrics),
        "builtin_available": builtin_available,
        "builtin_vcpu_hours": (
            float(builtin_usage.get("vcpu_hours", 0.0) or 0.0) if builtin_available else 0.0
        ),
        "builtin_memory_gb_hours": (
            float(builtin_usage.get("memory_gb_hours", 0.0) or 0.0) if builtin_available else 0.0
        ),
        "builtin_cost": (
            float(builtin_usage.get("builtin_cost", 0.0) or 0.0) if builtin_available else 0.0
        ),
        "is_runtime": state.get("config", {}).get("service") in {"runtime", "harness"},
        "runtime_available": available,
        "vcpu_hours": float(runtime_usage.get("vcpu_hours", 0.0) or 0.0) if available else 0.0,
        "memory_gb_hours": (
            float(runtime_usage.get("memory_gb_hours", 0.0) or 0.0) if available else 0.0
        ),
        "runtime_cost": float(runtime_usage.get("runtime_cost", 0.0) or 0.0) if available else 0.0,
    }


def record_run(states: dict[str, dict], *, run_id: str | None = None) -> None:
    """Fold one completed comparison (both agents) into the session total."""
    store = _store()
    for arch_name, state in states.items():
        records = store.setdefault(arch_name, [])
        if run_id and any(record.get("run_id") == run_id for record in records):
            continue
        record = _record_from_state(state)
        record["run_id"] = run_id
        records.append(record)
    st.session_state[SESSION_COST_KEY] = store
    _persist(store)


def update_runtime_usage(state: dict) -> None:
    """Patch the stored run after a delayed USAGE_LOGS lookup resolves.

    Runtime compute is vended minutes after the run, so the session total is
    incomplete until the user refreshes; this keeps the widget in step without
    re-recording the run.
    """
    session_id = state.get("session_id", "")
    if not session_id:
        return
    store = _store()
    refreshed = _record_from_state(state)
    for records in store.values():
        for index, record in enumerate(records):
            if record["session_id"] == session_id:
                refreshed["run_id"] = record.get("run_id")
                records[index] = refreshed
    st.session_state[SESSION_COST_KEY] = store
    _persist(store)


def refresh_pending_compute() -> tuple[int, int]:
    """Resolve delayed Runtime/Harness usage directly from the durable ledger."""
    store = _store()
    checked = 0
    resolved = 0
    for records in store.values():
        for record in records:
            runtime_pending = record.get("is_runtime") and not record.get("runtime_available")
            builtin_pending = bool(record.get("builtin_tool_calls")) and not record.get(
                "builtin_available"
            )
            token_pending = not record.get("tokens_available", True)
            if not runtime_pending and not builtin_pending and not token_pending:
                continue
            if not record.get("region") or not record.get("service"):
                continue
            checked += 1
            config = {
                "region": record["region"],
                "service": record["service"],
                "arn": record.get("arn", ""),
                "usage_log_group": record.get("usage_log_group", ""),
                "token_usage_log_group": record.get("token_usage_log_group", ""),
                "builtin_usage_log_group": record.get("builtin_usage_log_group", ""),
            }
            if runtime_pending:
                usage = fetch_compute_usage_for_session(
                    config,
                    record.get("session_id", ""),
                    record.get("started_at"),
                    record.get("completed_at"),
                )
                if usage.get("status") == RUNTIME_USAGE_AVAILABLE:
                    record["runtime_available"] = True
                    record["vcpu_hours"] = float(usage.get("vcpu_hours", 0.0) or 0.0)
                    record["memory_gb_hours"] = float(usage.get("memory_gb_hours", 0.0) or 0.0)
                    record["runtime_cost"] = float(usage.get("runtime_cost", 0.0) or 0.0)
            if builtin_pending:
                builtin = fetch_builtin_usage_for_session(
                    config,
                    record.get("session_id", ""),
                    record.get("builtin_tool_kinds") or ["code_interpreter"],
                    record.get("started_at"),
                    record.get("completed_at"),
                )
                if builtin.get("status") == BUILTIN_USAGE_AVAILABLE:
                    record["builtin_available"] = True
                    record["builtin_vcpu_hours"] = float(builtin.get("vcpu_hours", 0.0) or 0.0)
                    record["builtin_memory_gb_hours"] = float(
                        builtin.get("memory_gb_hours", 0.0) or 0.0
                    )
                    record["builtin_cost"] = float(builtin.get("builtin_cost", 0.0) or 0.0)
            if token_pending and record.get("service") == "runtime":
                token_usage = fetch_token_usage_for_session(
                    record["region"],
                    record.get("token_usage_log_group", ""),
                    record.get("session_id", ""),
                    record.get("started_at"),
                    record.get("completed_at"),
                )
                if token_usage.get("status") == TOKEN_USAGE_AVAILABLE:
                    usage = normalized_usage(token_usage.get("usage", {}))
                    model_id = record.get("model_id")
                    inference = inference_cost(usage, model_id)
                    record.update(
                        {
                            "tokens_available": True,
                            "fresh_input": usage["fresh_input"],
                            "cache_read": usage["cache_read"],
                            "cache_write": usage["cache_write"],
                            "output": usage["output"],
                            "total_tokens": usage["total"],
                            "token_cost": inference["total_cost"],
                            "cache_savings": cache_savings(usage, model_id),
                        }
                    )
            if (
                (not runtime_pending or record.get("runtime_available"))
                and (not builtin_pending or record.get("builtin_available"))
                and (not token_pending or record.get("tokens_available"))
            ):
                resolved += 1
    st.session_state[SESSION_COST_KEY] = store
    _persist(store)
    return checked, resolved


def _blank_total(arch_name: str) -> dict[str, Any]:
    return {
        "arch_name": arch_name,
        "runs": 0,
        "elapsed": 0.0,
        "fresh_input": 0,
        "cache_read": 0,
        "cache_write": 0,
        "output": 0,
        "total_tokens": 0,
        "token_cost": 0.0,
        "token_pending_runs": 0,
        "cache_savings": 0.0,
        "operations": 0,
        "gateway_cost": 0.0,
        "builtin_tool_calls": 0,
        "builtin_vcpu_hours": 0.0,
        "builtin_memory_gb_hours": 0.0,
        "builtin_cost": 0.0,
        "builtin_pending_runs": 0,
        "vcpu_hours": 0.0,
        "memory_gb_hours": 0.0,
        "runtime_cost": 0.0,
        "is_runtime": False,
        "runtime_pending_runs": 0,
    }


def _add(total: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    total["runs"] += 1
    for key in (
        "elapsed",
        "fresh_input",
        "cache_read",
        "cache_write",
        "output",
        "total_tokens",
        "token_cost",
        "cache_savings",
        "operations",
        "gateway_cost",
        "builtin_tool_calls",
        "builtin_vcpu_hours",
        "builtin_memory_gb_hours",
        "builtin_cost",
        "vcpu_hours",
        "memory_gb_hours",
        "runtime_cost",
    ):
        value = record.get(key, 0)
        if value is not None:
            total[key] += value
    total["is_runtime"] = total["is_runtime"] or record.get("is_runtime", False)
    if record.get("is_runtime") and not record.get("runtime_available"):
        total["runtime_pending_runs"] += 1
    if record.get("builtin_tool_calls") and not record.get("builtin_available"):
        total["builtin_pending_runs"] += 1
    if not record.get("tokens_available", True):
        total["token_pending_runs"] += 1
    return total


def session_totals(live_states: dict[str, dict] | None = None) -> dict[str, dict[str, Any]]:
    """Session total per architecture, optionally including a run in flight.

    A live state is folded in without being recorded, so the widget moves
    while the agents stream and does not double count when the run is stored.
    """
    store = _store()
    names = list(store) + [name for name in (live_states or {}) if name not in store]
    totals: dict[str, dict[str, Any]] = {}
    for arch_name in names:
        total = _blank_total(arch_name)
        for record in store.get(arch_name, []):
            _add(total, record)
        if live_states and (live := live_states.get(arch_name)) is not None:
            _add(total, _record_from_state(live))
        totals[arch_name] = total
    return totals


def total_cost(total: dict[str, Any]) -> float:
    return (
        total["token_cost"] + total["gateway_cost"] + total["runtime_cost"] + total["builtin_cost"]
    )


def grand_total(totals: dict[str, dict[str, Any]]) -> float:
    return sum(total_cost(total) for total in totals.values())


def is_partial(totals: dict[str, dict[str, Any]]) -> bool:
    """True when a consumed compute dimension is not present in the total."""
    return any(
        total["runtime_pending_runs"]
        or total["builtin_pending_runs"]
        or total["token_pending_runs"]
        for total in totals.values()
    )
