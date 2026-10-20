"""Tests for the AgentCore evaluation runner's local parsing and release gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from bedrock_agentcore.evaluation import AgentInvokerInput

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evaluations.run_agentcore_evaluation import (  # noqa: E402
    RequiredCloudWatchSpanCollector,
    _build_agent_invoker,
    _gate_report,
    _load_config,
    _parse_response_body,
    _region_from_arn,
    _runtime_id,
    _wait_for_runtime,
)

RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/weather-agent-123"


class _Body:
    def __init__(self, lines: list[bytes]):
        self.lines = lines

    def iter_lines(self, chunk_size=1):
        assert chunk_size == 1
        return iter(self.lines)


def test_runtime_identity_is_derived_from_arn():
    assert _runtime_id(RUNTIME_ARN) == "weather-agent-123"
    assert _region_from_arn(RUNTIME_ARN) == "eu-west-1"


def test_runtime_readiness_records_exact_container(monkeypatch):
    class _ControlClient:
        def get_agent_runtime(self, agentRuntimeId):
            assert agentRuntimeId == "weather-agent-123"
            return {
                "agentRuntimeId": agentRuntimeId,
                "agentRuntimeName": "weather-agent",
                "agentRuntimeVersion": "7",
                "status": "READY",
                "agentRuntimeArtifact": {
                    "containerConfiguration": {
                        "containerUri": "123456789012.dkr.ecr.eu-west-1.amazonaws.com/agent:abc123"
                    }
                },
            }

    monkeypatch.setattr(
        "evaluations.run_agentcore_evaluation.boto3.client",
        lambda *args, **kwargs: _ControlClient(),
    )

    metadata = _wait_for_runtime(RUNTIME_ARN, "eu-west-1", 0, 1, "abc123")

    assert metadata["status"] == "READY"
    assert metadata["runtime_version"] == "7"
    assert metadata["container_uri"].endswith(":abc123")


def test_runtime_readiness_rejects_wrong_deployment_image(monkeypatch):
    class _ControlClient:
        def get_agent_runtime(self, agentRuntimeId):
            return {
                "agentRuntimeId": agentRuntimeId,
                "status": "READY",
                "agentRuntimeArtifact": {
                    "containerConfiguration": {"containerUri": "repository:old-commit"}
                },
            }

    monkeypatch.setattr(
        "evaluations.run_agentcore_evaluation.boto3.client",
        lambda *args, **kwargs: _ControlClient(),
    )

    with pytest.raises(RuntimeError, match="expected deployment image"):
        _wait_for_runtime(RUNTIME_ARN, "eu-west-1", 0, 1, "new-commit")


def test_sse_response_is_reassembled_for_agentcore_runner():
    body = _Body(
        [
            b'data: {"type":"text","content":"Paris is "}',
            b'data: {"type":"text","content":"sunny."}',
            b'data: {"type":"metrics","tool_names":["getForecast"]}',
        ]
    )

    answer, events = _parse_response_body(body)

    assert answer == "Paris is sunny."
    assert len(events) == 3


def test_sse_error_event_fails_invocation():
    body = _Body([b'data: {"type":"error","content":"gateway unavailable"}'])

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        _parse_response_body(body)


def test_agent_invoker_uses_pinned_date_and_production_options(monkeypatch):
    class _DataClient:
        def invoke_agent_runtime(self, **kwargs):
            payload = json.loads(kwargs["payload"])
            assert payload == {
                "prompt": "Current date: 2026-06-22\nWhat is the weather in Paris?",
                "enable_caching": False,
                "enable_thinking": False,
            }
            return {
                "contentType": "text/event-stream; charset=utf-8",
                "response": _Body([b'data: {"type":"text","content":"18 C"}']),
            }

    monkeypatch.setattr(
        "evaluations.run_agentcore_evaluation.boto3.client",
        lambda *args, **kwargs: _DataClient(),
    )
    invoker = _build_agent_invoker(
        RUNTIME_ARN,
        "eu-west-1",
        {"enable_caching": False, "enable_thinking": False},
        "2026-06-22",
    )

    output = invoker(
        AgentInvokerInput(
            payload="What is the weather in Paris?",
            session_id="evaluation-session-12345678901234567890",
        )
    )

    assert output.agent_output == "18 C"


def test_cloudwatch_collector_requires_at_least_one_span(monkeypatch):
    monkeypatch.setattr(
        "evaluations.run_agentcore_evaluation.CloudWatchAgentSpanCollector.collect",
        lambda self, session_id, start_time, end_time: [],
    )
    collector = RequiredCloudWatchSpanCollector(
        log_group_name="/aws/bedrock-agentcore/runtimes/weather-agent-123-DEFAULT",  # pragma: allowlist secret
        region="eu-west-1",
        max_wait_seconds=1,
        poll_interval_seconds=1,
    )

    with pytest.raises(RuntimeError, match="No CloudWatch spans were collected"):
        collector.collect("evaluation-session", None, None)


def test_cloudwatch_collector_records_collection_evidence(monkeypatch):
    monkeypatch.setattr(
        "evaluations.run_agentcore_evaluation.CloudWatchAgentSpanCollector.collect",
        lambda self, session_id, start_time, end_time: [
            {"traceId": "trace-2", "spanId": "span-2"},
            {"traceId": "trace-1", "spanId": "span-1"},
        ],
    )
    collector = RequiredCloudWatchSpanCollector(
        log_group_name="/aws/bedrock-agentcore/runtimes/weather-agent-123-DEFAULT",  # pragma: allowlist secret
        region="eu-west-1",
        max_wait_seconds=1,
        poll_interval_seconds=1,
    )

    spans = collector.collect("evaluation-session", None, None)
    report = collector.collection_report()

    assert len(spans) == 2
    assert report["source"] == "cloudwatch_logs_insights"
    assert report["log_groups"] == [
        "aws/spans",
        "/aws/bedrock-agentcore/runtimes/weather-agent-123-DEFAULT",  # pragma: allowlist secret
    ]
    assert report["collections"] == [
        {
            "session_id": "evaluation-session",
            "span_count": 2,
            "trace_ids": ["trace-1", "trace-2"],
        }
    ]


def test_gate_fails_score_below_evaluator_threshold():
    payload = {
        "scenario_results": [
            {
                "scenario_id": "weather",
                "status": "COMPLETED",
                "evaluator_results": [
                    {
                        "evaluator_id": "Builtin.Correctness",
                        "results": [{"value": 0.5, "label": "Partially correct"}],
                    }
                ],
            }
        ]
    }

    gate = _gate_report(payload, {"Builtin.Correctness": 0.7})

    assert gate["passed"] is False
    assert "below" in gate["failures"][0]


def test_gate_passes_complete_results_at_threshold():
    payload = {
        "scenario_results": [
            {
                "scenario_id": "weather",
                "status": "COMPLETED",
                "evaluator_results": [
                    {
                        "evaluator_id": "Builtin.Correctness",
                        "results": [{"value": 0.7, "label": "Mostly correct"}],
                    }
                ],
            }
        ]
    }

    gate = _gate_report(payload, {"Builtin.Correctness": 0.7})

    assert gate["passed"] is True
    assert gate["failures"] == []


def test_gate_fails_when_an_expected_scenario_is_missing():
    payload = {"scenario_results": []}

    gate = _gate_report(
        payload,
        {"Builtin.Correctness": 0.7},
        {"weather", "missing-location"},
    )

    assert gate["passed"] is False
    assert any("Missing scenario results" in failure for failure in gate["failures"])


def test_gate_rejects_non_finite_score():
    payload = {
        "scenario_results": [
            {
                "scenario_id": "weather",
                "status": "COMPLETED",
                "evaluator_results": [
                    {
                        "evaluator_id": "Builtin.Correctness",
                        "results": [{"value": float("nan")}],
                    }
                ],
            }
        ]
    }

    gate = _gate_report(payload, {"Builtin.Correctness": 0.7}, {"weather"})

    assert gate["passed"] is False
    assert "no numeric score" in gate["failures"][0]


def test_config_rejects_out_of_range_threshold(tmp_path):
    config = {
        "schema_version": 1,
        "evaluator_groups": [
            {
                "name": "semantic",
                "scenario_filter": "all",
                "evaluator_thresholds": {"Builtin.Correctness": 1.1},
            }
        ],
        "evaluation_delay_seconds": 0,
        "max_concurrent_scenarios": 1,
        "span_max_wait_seconds": 1,
        "span_poll_interval_seconds": 1,
        "runtime_ready_timeout_seconds": 1,
        "runtime_ready_poll_seconds": 1,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="between 0 and 1"):
        _load_config(path)


def test_checked_in_invocation_profile_matches_comparison_backend():
    config_path = ROOT / "evaluations" / "agentcore_evaluation_config.json"
    config = _load_config(config_path)

    assert config["invocation_payload"] == {
        "enable_caching": False,
        "enable_thinking": False,
    }


def test_agentcore_dataset_matches_mlflow_golden_cases():
    agentcore_dataset = json.loads(
        (ROOT / "evaluations" / "agentcore_dataset.json").read_text(encoding="utf-8")
    )
    mlflow_dataset = json.loads(
        (ROOT / "evaluations" / "datasets" / "opensource_golden_v1.json").read_text(
            encoding="utf-8"
        )
    )

    scenarios = {scenario["scenario_id"]: scenario for scenario in agentcore_dataset["scenarios"]}
    records = {record["tags"]["case_id"]: record for record in mlflow_dataset["records"]}

    assert set(scenarios) == set(records)
    for case_id, record in records.items():
        scenario = scenarios[case_id]
        assert scenario["turns"] == [
            {
                "input": record["inputs"]["question"],
                "expected_response": record["expectations"]["expected_response"],
            }
        ]
        assert scenario["metadata"]["mlflow_dataset_name"] == mlflow_dataset["name"]
        assert scenario["metadata"]["mlflow_dataset_version"] == mlflow_dataset["version"]
        assert scenario["metadata"]["required_terms"] == record["expectations"]["required_terms"]
        assert (
            scenario["metadata"]["forbidden_claims"] == record["expectations"]["forbidden_claims"]
        )
        assert scenario["metadata"]["must_refuse"] == record["expectations"]["must_refuse"]
