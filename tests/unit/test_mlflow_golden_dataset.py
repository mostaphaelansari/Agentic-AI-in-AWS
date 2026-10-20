from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evaluations.run_mlflow_golden_dataset import (  # noqa: E402
    DATASET_PATH,
    _configure_kimi_judges,
    _delete_stale_managed_records,
    _register_scorer_if_needed,
    configure_eval_model,
    golden_forbidden_claims_absent,
    golden_refusal_contract,
    golden_required_terms,
    latency_ms,
    load_golden_dataset,
)


def test_golden_dataset_schema_and_balance():
    dataset = load_golden_dataset()
    records = dataset["records"]
    case_ids = {record["tags"]["case_id"] for record in records}
    categories = {record["tags"]["category"] for record in records}

    assert dataset["name"] == "opensource-golden-v1"
    assert len(records) >= 12
    assert len(case_ids) == len(records)
    assert {"weather", "map_research", "booking_search", "flight_search"} <= categories
    assert {"code_interpreter", "privacy", "safety", "web_research"} <= categories
    assert any(record["expectations"]["must_refuse"] for record in records)
    assert any(not record["expectations"]["must_refuse"] for record in records)


def test_golden_dataset_records_match_mlflow_genai_shape():
    raw = json.loads(DATASET_PATH.read_text())
    for record in raw["records"]:
        assert set(record) == {"inputs", "expectations", "tags"}
        assert set(record["inputs"]) == {"question"}
        assert isinstance(record["expectations"]["expected_response"], str)
        assert isinstance(record["expectations"]["required_terms"], list)
        assert isinstance(record["expectations"]["forbidden_claims"], list)
        assert isinstance(record["expectations"]["must_refuse"], bool)
        assert record["tags"]["case_id"]
        assert record["tags"]["category"]


def test_golden_code_scorers_accept_good_output():
    expectations = {
        "required_terms": ["Paris", "weather"],
        "forbidden_claims": ["I cannot access current weather"],
        "must_refuse": False,
    }
    output = "Paris weather is available from the live forecast."

    assert golden_required_terms(outputs=output, expectations=expectations).value is True
    assert golden_forbidden_claims_absent(outputs=output, expectations=expectations).value is True
    assert golden_refusal_contract(outputs=output, expectations=expectations).value is True


def test_golden_code_scorers_reject_bad_output():
    expectations = {
        "required_terms": ["Paris"],
        "forbidden_claims": ["jane@example.com"],
        "must_refuse": True,
    }
    output = "Here is jane@example.com with no refusal."

    assert golden_required_terms(outputs=output, expectations=expectations).value is False
    assert golden_forbidden_claims_absent(outputs=output, expectations=expectations).value is False
    assert golden_refusal_contract(outputs=output, expectations=expectations).value is False


def test_latency_metric_reads_trace_duration():
    trace = SimpleNamespace(info=SimpleNamespace(execution_duration=1234))

    feedback = latency_ms(trace=trace)

    assert feedback.value == 1234.0
    assert feedback.valid is True


def test_latency_metric_is_invalid_without_duration():
    trace = SimpleNamespace(info=SimpleNamespace(execution_duration=None))

    feedback = latency_ms(trace=trace)

    assert feedback.value is None
    assert feedback.valid is False


def test_kimi_judge_bundle_contains_requested_metrics(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "test-key")
    judges = _configure_kimi_judges()

    assert [judge.name for judge in judges] == [
        "coherence",
        "correctness",
        "faithfulness",
        "relevance_to_query",
        "safety",
    ]


def test_managed_dataset_sync_deletes_stale_case_ids():
    deleted = []

    class FakeFrame:
        def to_dict(self, orient):
            assert orient == "records"
            return [
                {
                    "dataset_record_id": "dr-old",
                    "tags": {"case_id": "old_mlflow_docs_case"},
                },
                {
                    "dataset_record_id": "dr-keep",
                    "tags": {"case_id": "weather_paris_current"},
                },
                {"dataset_record_id": "dr-untagged", "tags": {}},
            ]

    class FakeDataset:
        def to_df(self):
            return FakeFrame()

        def delete_records(self, record_ids):
            deleted.extend(record_ids)
            return len(record_ids)

    dataset = {
        "records": [
            {
                "tags": {"case_id": "weather_paris_current"},
            }
        ]
    }

    assert _delete_stale_managed_records(FakeDataset(), dataset) == 1
    assert deleted == ["dr-old"]


def test_configure_eval_model_uses_default_logged_model(monkeypatch):
    calls = []
    monkeypatch.delenv("MLFLOW_EVAL_MODEL_NAME", raising=False)
    monkeypatch.delenv("MLFLOW_ACTIVE_MODEL_NAME", raising=False)
    monkeypatch.setattr(
        "evaluations.run_mlflow_golden_dataset.mlflow.set_active_model",
        lambda name: calls.append(name) or SimpleNamespace(name=name, model_id="m-test"),
    )

    model_name, model_id = configure_eval_model()

    assert model_name == "opensource-custom-agent-v3"
    assert model_id == "m-test"
    assert calls == ["opensource-custom-agent-v3"]


def test_register_scorer_updates_latest_version_when_model_was_missing(monkeypatch):
    calls = []
    current = SimpleNamespace(name="faithfulness", model=None)

    class DesiredScorer:
        name = "faithfulness"
        model = "openai:/kimi-k2.6"

        def register(self, experiment_id):
            calls.append(experiment_id)
            return self

    monkeypatch.setattr(
        "evaluations.run_mlflow_golden_dataset.list_scorers",
        lambda experiment_id: [current],
    )

    registered = _register_scorer_if_needed("1", DesiredScorer())

    assert registered.model == "openai:/kimi-k2.6"
    assert calls == ["1"]
