from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evaluations.run_mlflow_trace_evaluation import (  # noqa: E402
    _configure_eval_model,
    latency_ms,
    non_empty_output,
)


def test_non_empty_output_accepts_nested_content():
    feedback = non_empty_output(outputs={"messages": [{"content": "hello"}]})

    assert feedback.value is True


def test_non_empty_output_rejects_empty_content():
    feedback = non_empty_output(outputs={"messages": [{"content": "   "}]})

    assert feedback.value is False


def test_trace_latency_metric_reads_execution_duration():
    trace = SimpleNamespace(info=SimpleNamespace(execution_duration=987))

    feedback = latency_ms(trace=trace)

    assert feedback.value == 987.0
    assert feedback.valid is True


def test_trace_latency_metric_is_invalid_without_duration():
    trace = SimpleNamespace(info=SimpleNamespace(execution_duration=None))

    feedback = latency_ms(trace=trace)

    assert feedback.value is None
    assert feedback.valid is False


def test_trace_eval_model_uses_default_logged_model(monkeypatch):
    calls = []
    monkeypatch.delenv("MLFLOW_EVAL_MODEL_NAME", raising=False)
    monkeypatch.delenv("MLFLOW_ACTIVE_MODEL_NAME", raising=False)
    monkeypatch.setattr(
        "evaluations.run_mlflow_trace_evaluation.mlflow.set_active_model",
        lambda name: calls.append(name) or SimpleNamespace(name=name, model_id="m-test"),
    )

    model_name, model_id = _configure_eval_model()

    assert model_name == "opensource-custom-agent-v3"
    assert model_id == "m-test"
    assert calls == ["opensource-custom-agent-v3"]
