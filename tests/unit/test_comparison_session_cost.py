"""Running session-cost total for the Managed and Custom agent comparison.

The session total must be the sum of the per-run cost tables, must not shrink
when history is trimmed, and must absorb AgentCore Runtime compute whenever the
delayed USAGE_LOGS lookup finally resolves.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from config import BEDROCK_CHEAP_MODEL_ID  # noqa: E402
from services.cost import gateway_cost, inference_cost  # noqa: E402
from services.runtime_usage import (  # noqa: E402
    RUNTIME_USAGE_AVAILABLE,
    RUNTIME_USAGE_PENDING,
)

MANAGED = "Managed agent"
CUSTOM = "Custom agent"


@pytest.fixture(autouse=True)
def session_cost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Load the module against a plain dict standing in for st.session_state."""
    import services.session_cost as module

    monkeypatch.setattr(module.st, "session_state", {}, raising=False)
    monkeypatch.setenv(module.LEDGER_DB_ENV, str(tmp_path / "costs.sqlite3"))
    module.st.session_state[module.LEDGER_ID_KEY] = "test-ledger"
    return module


def _state(arch: str, *, tokens: int, tools: int, runtime: dict | None = None) -> dict:
    return {
        "arch_name": arch,
        "session_id": f"session-{arch}-{tokens}",
        "done": True,
        "elapsed": 5.0,
        "config": {"service": "runtime" if arch == CUSTOM else "harness"},
        "metrics": {
            "usage": {
                "inputTokens": tokens,
                "outputTokens": tokens // 2,
                "totalTokens": tokens + tokens // 2,
            },
            "tool_calls": tools,
            "tool_names": ["searchLocation"] * tools,
        },
        "runtime_usage": runtime or {},
    }


def _states(tokens: int = 1000, tools: int = 2, runtime: dict | None = None) -> dict[str, dict]:
    return {
        MANAGED: _state(MANAGED, tokens=tokens, tools=tools),
        CUSTOM: _state(CUSTOM, tokens=tokens, tools=tools, runtime=runtime),
    }


def test_session_total_matches_the_per_run_cost_functions(session_cost) -> None:
    session_cost.record_run(_states(tokens=1_000_000, tools=4))

    totals = session_cost.session_totals()
    managed = totals[MANAGED]

    expected_tokens = inference_cost(
        {"fresh_input": 1_000_000, "cache_read": 0, "cache_write": 0, "output": 500_000}
    )["total_cost"]
    assert managed["token_cost"] == pytest.approx(expected_tokens)
    assert managed["gateway_cost"] == pytest.approx(gateway_cost(4))
    assert session_cost.total_cost(managed) == pytest.approx(expected_tokens + gateway_cost(4))


def test_cheap_model_uses_its_own_token_rates() -> None:
    usage = {"fresh_input": 1_000_000, "cache_read": 0, "cache_write": 0, "output": 1_000_000}

    assert inference_cost(usage, BEDROCK_CHEAP_MODEL_ID)["total_cost"] == pytest.approx(6.0)
    assert inference_cost(usage)["total_cost"] == pytest.approx(18.0)


def test_harness_specific_haiku_id_uses_cheap_rates() -> None:
    usage = {"fresh_input": 1_000_000, "cache_read": 0, "cache_write": 0, "output": 0}

    cost = inference_cost(usage, "arn:example:claude-haiku-4-5-regional-alias")

    assert cost["total_cost"] == pytest.approx(1.0)


def test_totals_accumulate_across_runs_per_architecture(session_cost) -> None:
    session_cost.record_run(_states(tokens=1000, tools=1))
    session_cost.record_run(_states(tokens=3000, tools=2))

    totals = session_cost.session_totals()

    assert set(totals) == {MANAGED, CUSTOM}
    assert totals[MANAGED]["runs"] == 2
    assert totals[MANAGED]["total_tokens"] == (1000 + 500) + (3000 + 1500)
    assert totals[MANAGED]["operations"] == 3
    assert totals[CUSTOM]["runs"] == 2
    assert session_cost.grand_total(totals) == pytest.approx(
        session_cost.total_cost(totals[MANAGED]) + session_cost.total_cost(totals[CUSTOM])
    )


def test_runtime_compute_is_flagged_pending_until_usage_logs_land(session_cost) -> None:
    states = _states(runtime={"status": RUNTIME_USAGE_PENDING})
    session_cost.record_run(states)

    totals = session_cost.session_totals()

    # Harness is a managed abstraction over Runtime, so both have compute usage.
    assert totals[CUSTOM]["is_runtime"] is True
    assert totals[MANAGED]["is_runtime"] is True
    assert totals[CUSTOM]["runtime_cost"] == 0.0
    assert totals[CUSTOM]["runtime_pending_runs"] == 1
    assert session_cost.is_partial(totals) is True


def test_refreshed_runtime_usage_updates_the_recorded_run(session_cost) -> None:
    states = _states(runtime={"status": RUNTIME_USAGE_PENDING})
    states[MANAGED]["runtime_usage"] = {
        "status": RUNTIME_USAGE_AVAILABLE,
        "vcpu_hours": 0.0,
        "memory_gb_hours": 0.0,
        "runtime_cost": 0.0,
    }
    session_cost.record_run(states)
    before = session_cost.grand_total(session_cost.session_totals())

    resolved = states[CUSTOM]
    resolved["runtime_usage"] = {
        "status": RUNTIME_USAGE_AVAILABLE,
        "vcpu_hours": 0.0004,
        "memory_gb_hours": 0.0011,
        "runtime_cost": 0.000045,
    }
    session_cost.update_runtime_usage(resolved)

    totals = session_cost.session_totals()
    assert totals[CUSTOM]["runtime_cost"] == pytest.approx(0.000045)
    assert totals[CUSTOM]["vcpu_hours"] == pytest.approx(0.0004)
    assert totals[CUSTOM]["runtime_pending_runs"] == 0
    assert session_cost.is_partial(totals) is False
    # The run is patched, never appended twice.
    assert totals[CUSTOM]["runs"] == 1
    assert session_cost.grand_total(totals) == pytest.approx(before + 0.000045)


def test_a_run_in_flight_is_shown_but_not_double_counted(session_cost) -> None:
    session_cost.record_run(_states(tokens=1000, tools=1))
    live = _states(tokens=2000, tools=1)
    for state in live.values():
        state["done"] = False

    during = session_cost.session_totals(live)
    assert during[MANAGED]["runs"] == 2

    session_cost.record_run(live)
    after = session_cost.session_totals()

    assert after[MANAGED]["runs"] == 2
    assert after[MANAGED]["total_tokens"] == during[MANAGED]["total_tokens"]


def test_new_chat_zeroes_the_total(session_cost) -> None:
    session_cost.record_run(_states())
    session_cost.reset_session_cost()

    assert session_cost.session_totals() == {}
    assert session_cost.grand_total(session_cost.session_totals()) == 0.0


def test_session_total_survives_a_process_restart(session_cost) -> None:
    session_cost.record_run(_states(tokens=1234, tools=2))
    ledger_id = session_cost.st.session_state[session_cost.LEDGER_ID_KEY]

    session_cost.st.session_state = {session_cost.LEDGER_ID_KEY: ledger_id}
    totals = session_cost.session_totals()

    assert totals[MANAGED]["runs"] == 1
    assert totals[MANAGED]["total_tokens"] == 1234 + 617


def test_pending_compute_can_refresh_after_a_process_restart(session_cost, monkeypatch) -> None:
    states = _states(runtime={"status": RUNTIME_USAGE_PENDING})
    for state in states.values():
        state["config"].update(
            {
                "region": "eu-test-1",
                "arn": f"arn:test:{state['arch_name']}",
                "usage_log_group": "usage-log",
            }
        )
    session_cost.record_run(states)
    ledger_id = session_cost.st.session_state[session_cost.LEDGER_ID_KEY]
    session_cost.st.session_state = {session_cost.LEDGER_ID_KEY: ledger_id}
    monkeypatch.setattr(
        session_cost,
        "fetch_compute_usage_for_session",
        lambda *args, **kwargs: {
            "status": RUNTIME_USAGE_AVAILABLE,
            "vcpu_hours": 0.1,
            "memory_gb_hours": 0.2,
            "runtime_cost": 0.01,
        },
    )

    checked, resolved = session_cost.refresh_pending_compute()
    totals = session_cost.session_totals()

    assert (checked, resolved) == (2, 2)
    assert totals[MANAGED]["runtime_cost"] == pytest.approx(0.01)
    assert totals[CUSTOM]["runtime_cost"] == pytest.approx(0.01)
    assert session_cost.is_partial(totals) is False


def test_widget_renders_a_card_per_architecture(session_cost) -> None:
    import components.session_cost as widget

    widget.st.session_state = session_cost.st.session_state
    session_cost.record_run(_states(runtime={"status": RUNTIME_USAGE_PENDING}))

    markup = widget.session_cost_html()

    assert "session-cost-card managed" in markup
    assert "session-cost-card custom" in markup
    assert "Managed agent" in markup and "Custom agent" in markup
    assert "pending" in markup


def test_refresh_button_stays_visible_without_pending_runs(session_cost, monkeypatch) -> None:
    import components.session_cost as widget

    events = []

    class Slot:
        def markdown(self, *_args, **_kwargs):
            events.append("widget")

    class FakeStreamlit:
        session_state = session_cost.st.session_state

        def button(self, label, **_kwargs):
            events.append(label)
            return False

        def empty(self):
            return Slot()

        def markdown(self, *_args, **_kwargs):
            events.append("widget")

        def fragment(self, **_kwargs):
            # The widget auto-refreshes inside a fragment; render it inline here.
            return lambda func: func

    monkeypatch.setattr(widget, "st", FakeStreamlit())
    monkeypatch.setattr(widget, "_session_cost_fragment", None)

    widget.mount_session_cost_widget()

    assert events == ["Refresh session cost", "widget"]


def test_builtin_tool_compute_is_visible_and_marks_the_total_partial(session_cost) -> None:
    states = _states(runtime={"status": RUNTIME_USAGE_AVAILABLE})
    states[CUSTOM]["metrics"]["tool_names"] = ["browser", "code_interpreter"]
    states[CUSTOM]["metrics"]["tool_calls"] = 2
    session_cost.record_run(states)

    totals = session_cost.session_totals()

    assert totals[CUSTOM]["builtin_tool_calls"] == 2
    assert totals[CUSTOM]["operations"] == 0
    assert session_cost.is_partial(totals) is True


def test_measured_builtin_compute_is_included_in_the_total(session_cost) -> None:
    states = _states(runtime={"status": RUNTIME_USAGE_AVAILABLE})
    states[CUSTOM]["metrics"]["tool_names"] = ["code_interpreter"]
    states[CUSTOM]["metrics"]["tool_calls"] = 1
    states[CUSTOM]["builtin_usage"] = {
        "status": RUNTIME_USAGE_AVAILABLE,
        "vcpu_hours": 0.001,
        "memory_gb_hours": 0.002,
        "builtin_cost": 0.0001084,
    }
    session_cost.record_run(states)

    total = session_cost.session_totals()[CUSTOM]

    assert total["builtin_pending_runs"] == 0
    assert total["builtin_vcpu_hours"] == pytest.approx(0.001)
    assert total["builtin_memory_gb_hours"] == pytest.approx(0.002)
    assert total["builtin_cost"] == pytest.approx(0.0001084)
    assert session_cost.total_cost(total) == pytest.approx(
        total["token_cost"] + total["gateway_cost"] + total["runtime_cost"] + 0.0001084
    )


def test_pending_builtin_compute_is_refreshed_from_the_durable_record(
    session_cost, monkeypatch
) -> None:
    states = _states(runtime={"status": RUNTIME_USAGE_AVAILABLE})
    states[CUSTOM]["metrics"]["tool_names"] = ["code_interpreter"]
    states[CUSTOM]["metrics"]["tool_calls"] = 1
    states[CUSTOM]["config"].update(
        {"region": "eu-test-1", "builtin_usage_log_group": "agentcore-tools"}
    )
    session_cost.record_run(states)

    monkeypatch.setattr(
        session_cost,
        "fetch_builtin_usage_for_session",
        lambda *_args, **_kwargs: {
            "status": "available",
            "vcpu_hours": 0.003,
            "memory_gb_hours": 0.004,
            "builtin_cost": 0.0003063,
        },
    )

    checked, resolved = session_cost.refresh_pending_compute()
    total = session_cost.session_totals()[CUSTOM]

    assert (checked, resolved) == (1, 1)
    assert total["builtin_pending_runs"] == 0
    assert total["builtin_cost"] == pytest.approx(0.0003063)


def test_only_custom_runtime_pending_tokens_are_reconciled(session_cost, monkeypatch) -> None:
    available_compute = {
        "status": RUNTIME_USAGE_AVAILABLE,
        "vcpu_hours": 0.0,
        "memory_gb_hours": 0.0,
        "runtime_cost": 0.0,
    }
    states = _states(runtime=available_compute)
    states[MANAGED]["runtime_usage"] = available_compute
    for state in states.values():
        state["tokens_available"] = False
        state["config"].update(
            {"region": "eu-test-1", "token_usage_log_group": "runtime-application"}
        )
    session_cost.record_run(states)
    monkeypatch.setattr(
        session_cost,
        "fetch_token_usage_for_session",
        lambda *_args, **_kwargs: {
            "status": "available",
            "usage": {"inputTokens": 120, "outputTokens": 30, "totalTokens": 150},
        },
    )

    checked, resolved = session_cost.refresh_pending_compute()
    totals = session_cost.session_totals()

    assert (checked, resolved) == (2, 1)
    assert totals[CUSTOM]["token_pending_runs"] == 0
    assert totals[CUSTOM]["total_tokens"] == 150
    assert totals[MANAGED]["token_pending_runs"] == 1


def test_new_chat_resets_cost_and_the_widget_is_wired_into_the_run(session_cost) -> None:
    comparison = (APP_ROOT / "components" / "comparison.py").read_text()
    history = (APP_ROOT / "components" / "history.py").read_text()
    app = (APP_ROOT / "app.py").read_text()

    # The run is committed once, from the fragment that renders the live run.
    assert "record_run(frozen_states, run_id=controller.run_id)" in comparison
    assert "commit_once(_commit)" in comparison
    assert "update_runtime_usage(state)" in comparison
    assert "reset_session_cost()" in history
    assert "mount_session_cost_widget()" in app
    assert "Refresh session cost" in (APP_ROOT / "components" / "session_cost.py").read_text()


def test_harness_usage_log_group_follows_the_underlying_runtime(monkeypatch) -> None:
    from services import runtime_usage

    class ControlClient:
        def get_harness(self, **kwargs):
            assert kwargs == {"harnessId": "managed-agent-123"}
            return {
                "harness": {
                    "environment": {
                        "agentCoreRuntimeEnvironment": {
                            "agentRuntimeId": "harness_managed-agent-runtime-456"
                        }
                    }
                }
            }

    monkeypatch.setattr(runtime_usage.boto3, "client", lambda *args, **kwargs: ControlClient())

    group = runtime_usage._harness_usage_log_group(
        {
            "service": "harness",
            "region": "eu-central-1",
            "arn": "arn:aws:bedrock-agentcore:eu-central-1:123:harness/managed-agent-123",
        }
    )

    assert group.endswith("/USAGE_LOGS/harness_managed-agent-runtime-456")


def test_vended_usage_logs_fall_back_to_bounded_stream_scan(monkeypatch) -> None:
    from services import runtime_usage

    class LogsClient:
        calls = 0

        def filter_log_events(self, **kwargs):
            return {"events": []}

        def describe_log_streams(self, **kwargs):
            return {
                "logStreams": [
                    {
                        "logStreamName": "BedrockAgentCoreRuntime_UsageLogs",
                        "firstEventTimestamp": 1,
                        "lastEventTimestamp": 9_999_999_999_999,
                    }
                ]
            }

        def get_log_events(self, **kwargs):
            self.calls += 1
            if self.calls > 1:
                return {"events": [], "nextForwardToken": "finished"}
            return {
                "events": [
                    {
                        "message": json.dumps(
                            {
                                "attributes": {
                                    "session.id": "durable-harness-session",
                                    "time_elapsed_seconds": 1.0,
                                },
                                "metrics": {
                                    "agent.runtime.vcpu.hours.used": 0.1,
                                    "agent.runtime.memory.gb_hours.used": 0.2,
                                },
                            }
                        )
                    }
                ],
                "nextForwardToken": "finished",
            }

    client = LogsClient()
    monkeypatch.setattr(runtime_usage.boto3, "client", lambda *args, **kwargs: client)

    usage = runtime_usage.fetch_runtime_usage_for_session(
        "eu-test-1", "vended-usage", "durable-harness-session", 100.0, 101.0
    )

    assert usage["status"] == RUNTIME_USAGE_AVAILABLE
    assert usage["vcpu_hours"] == pytest.approx(0.1)
    assert usage["memory_gb_hours"] == pytest.approx(0.2)
