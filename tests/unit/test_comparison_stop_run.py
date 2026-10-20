"""Stopping an in-flight comparison.

Stop has to return immediately even though both workers sit in blocking
botocore reads, so cancellation closes the stream handles from another thread
and asks AgentCore to terminate the Custom Runtime session. Whatever streamed
before the stop is kept, the run is committed to history and the ledger exactly
once, and usage that never arrived is recorded as pending rather than zero.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

MANAGED = "Managed agent"
CUSTOM = "Custom agent"


class _FakeStream:
    """Stands in for a botocore stream whose read only ends when it is closed."""

    def __init__(self) -> None:
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch):
    import services.run_controller as module

    streams: dict[str, _FakeStream] = {}
    stop_calls: list[dict] = []
    prompts: dict[str, str] = {}
    routing_prompts: list[str] = []

    def fake_dispatch(config, session_id, prompt, event_queue, options, *, controller, arch_name):
        prompts[arch_name] = prompt
        stream = _FakeStream()
        streams[arch_name] = stream
        controller.register_stream(arch_name, stream)
        event_queue.put_nowait(("event", {"type": "text", "content": f"partial {arch_name}"}))
        stream.closed.wait(timeout=10)
        event_queue.put_nowait(("elapsed", 1.0))
        event_queue.put_nowait(("done", None))

    class _FakeClient:
        def stop_runtime_session(self, **kwargs):
            stop_calls.append(kwargs)
            return {"statusCode": 200}

    monkeypatch.setattr(module, "dispatch", fake_dispatch)
    monkeypatch.setattr(module, "get_client", lambda region: _FakeClient())

    def fake_routed_options(prompt, options):
        routing_prompts.append(prompt)
        return dict(options)

    monkeypatch.setattr(module, "routed_options", fake_routed_options)
    module.streams = streams
    module.stop_calls = stop_calls
    module.prompts = prompts
    module.routing_prompts = routing_prompts
    return module


def _run_until_finalized(controller) -> dict[str, dict]:
    deadline = time.monotonic() + 10
    while not controller.ready_to_finalize():
        if time.monotonic() > deadline:  # pragma: no cover - guards a hung test
            raise AssertionError("run never became ready to finalize")
        time.sleep(0.05)
    return controller.finalize()


def test_slow_model_routing_does_not_block_widget_start(controller_module, monkeypatch) -> None:
    routing_gate = threading.Event()

    def slow_route(_prompt, options):
        routing_gate.wait(timeout=5)
        return {
            **options,
            "model_tier": "strong",
            "model_id": "sonnet",
            "model_router_name": "litellm-complexity-router",
            "model_router_latency_ms": 12.75,
        }

    monkeypatch.setattr(controller_module, "routed_options", slow_route)
    started = time.monotonic()
    controller = controller_module.RunController("hi", {})
    controller.start()

    assert time.monotonic() - started < 0.5
    assert {state["status"] for state in controller.states.values()} == {"Routing"}
    assert controller_module.prompts == {}

    routing_gate.set()
    deadline = time.monotonic() + 5
    while len(controller_module.prompts) < 2:
        assert time.monotonic() < deadline, "workers did not start after routing"
        time.sleep(0.05)
    assert {
        state["metrics"]["model_router_latency_ms"] for state in controller.states.values()
    } == {12.75}
    controller.stop()
    _run_until_finalized(controller)


def test_stop_returns_without_waiting_on_blocked_workers(controller_module) -> None:
    controller = controller_module.RunController("hi", {})
    controller.start()
    time.sleep(0.3)

    started = time.monotonic()
    controller.stop()
    elapsed = time.monotonic() - started

    # The whole point: no join, no 600s botocore read to sit through.
    assert elapsed < 1.0
    deadline = time.monotonic() + 5
    while not all(stream.closed.is_set() for stream in controller_module.streams.values()):
        assert time.monotonic() < deadline, "stop did not close the stream handles"
        time.sleep(0.05)


def test_controller_dispatches_architecture_specific_memory_prompts(controller_module) -> None:
    controller = controller_module.RunController(
        "fallback",
        {},
        agent_prompts={MANAGED: "managed memory", CUSTOM: "custom memory"},
        parent_id="parent-run",
        branch_source_id="edited-run",
        ledger_id="chat-ledger",
    )
    controller.start()
    time.sleep(0.3)
    controller.stop()
    _run_until_finalized(controller)

    assert controller_module.prompts == {
        MANAGED: "managed memory",
        CUSTOM: "custom memory",
    }
    assert controller.parent_id == "parent-run"
    assert controller.branch_source_id == "edited-run"
    assert controller.ledger_id == "chat-ledger"


def test_custom_only_controller_dispatches_no_managed_worker(controller_module) -> None:
    controller = controller_module.RunController(
        "custom only",
        {},
        architectures=(CUSTOM,),
        page_scope="custom",
    )
    controller.start()
    deadline = time.monotonic() + 5
    while CUSTOM not in controller_module.prompts:
        assert time.monotonic() < deadline, "custom worker did not start"
        time.sleep(0.05)
    controller.stop()
    states = _run_until_finalized(controller)

    assert set(states) == {CUSTOM}
    assert controller_module.prompts == {CUSTOM: "custom only"}
    assert controller.page_scope == "custom"


def test_controller_routes_original_prompt_but_dispatches_augmented_prompt(
    controller_module,
) -> None:
    original_prompt = (
        "Compare annotated and geometry-inferred secondary structure for this protein. "
        "Investigate disagreements, account for missing coordinates and alternate "
        "conformers, and explain how confident we should be in each helix boundary."
    )
    augmented_prompt = f"{original_prompt}\n\naugmented upload and tool instructions"
    controller = controller_module.RunController(
        augmented_prompt,
        {"model_routing": "auto", "uploaded_file": "structure.cif"},
        display_prompt=original_prompt,
    )
    controller.start()
    deadline = time.monotonic() + 5
    while len(controller_module.prompts) < 2:
        assert time.monotonic() < deadline, "workers did not start after routing"
        time.sleep(0.05)
    controller.stop()
    _run_until_finalized(controller)

    assert controller_module.routing_prompts == [original_prompt]
    assert controller_module.prompts == {
        MANAGED: augmented_prompt,
        CUSTOM: augmented_prompt,
    }


def test_stop_terminates_only_the_custom_runtime_session(controller_module) -> None:
    controller = controller_module.RunController("hi", {})
    controller.start()
    time.sleep(0.3)
    controller.stop()
    _run_until_finalized(controller)

    # invoke_harness has no stop operation, so Managed is client-side only.
    assert len(controller_module.stop_calls) == 1
    assert (
        controller_module.stop_calls[0]["runtimeSessionId"]
        == (controller.states[CUSTOM]["session_id"])
    )


def test_partial_answer_survives_and_usage_is_pending(controller_module) -> None:
    controller = controller_module.RunController("hi", {})
    controller.start()
    time.sleep(0.3)
    controller.drain()
    controller.stop()

    states = _run_until_finalized(controller)

    for arch_name, state in states.items():
        assert state["status"] == "Stopped"
        assert state["done"] is True
        assert state["answer"] == f"partial {arch_name}"
        # No metrics event arrived, so cost must not be recorded as zero.
        assert state["tokens_available"] is False


def test_run_is_committed_once_and_late_events_are_dropped(controller_module) -> None:
    controller = controller_module.RunController("hi", {})
    controller.start()
    time.sleep(0.3)
    controller.stop()
    _run_until_finalized(controller)

    commits: list[dict] = []
    assert controller.commit_once(commits.append) is True
    assert controller.commit_once(commits.append) is False
    assert len(commits) == 1

    controller.queues[CUSTOM].put_nowait(("event", {"type": "text", "content": "late"}))
    controller.drain()
    assert "late" not in controller.states[CUSTOM]["answer"]
    assert "late" not in controller.states[CUSTOM]["pending_text"]


def test_a_failed_stop_call_still_closes_the_streams(controller_module, monkeypatch) -> None:
    class _BrokenClient:
        def stop_runtime_session(self, **kwargs):
            raise RuntimeError("AccessDeniedException: StopRuntimeSession")

    monkeypatch.setattr(controller_module, "get_client", lambda region: _BrokenClient())
    controller = controller_module.RunController("hi", {})
    controller.start()
    time.sleep(0.3)
    controller.stop()

    states = _run_until_finalized(controller)
    assert all(state["status"] == "Stopped" for state in states.values())
    assert all(stream.closed.is_set() for stream in controller_module.streams.values())


def test_stopped_run_is_recorded_as_pending_not_zero(controller_module, tmp_path, monkeypatch):
    import services.session_cost as session_cost

    monkeypatch.setattr(session_cost.st, "session_state", {}, raising=False)
    monkeypatch.setenv(session_cost.LEDGER_DB_ENV, str(tmp_path / "costs.sqlite3"))
    session_cost.st.session_state[session_cost.LEDGER_ID_KEY] = "stop-test-ledger"

    controller = controller_module.RunController("hi", {})
    controller.start()
    time.sleep(0.3)
    controller.stop()
    states = _run_until_finalized(controller)

    session_cost.record_run(states, run_id=controller.run_id)
    session_cost.record_run(states, run_id=controller.run_id)

    totals = session_cost.session_totals()
    assert totals[CUSTOM]["token_pending_runs"] == 1
    assert totals[MANAGED]["token_pending_runs"] == 1
    assert totals[CUSTOM]["token_cost"] == 0.0
    assert session_cost.is_partial(totals) is True
    store = session_cost._store()
    assert len(store[CUSTOM]) == 1, "record_run must be idempotent for one run id"
    assert store[CUSTOM][0]["total_tokens"] is None
