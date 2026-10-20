"""Tests for the Travel/Protein LLMRouter cold-start corpus."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evaluations.llmrouter.build_artifacts import materialize_config  # noqa: E402
from evaluations.llmrouter.generate_dataset import (  # noqa: E402
    generate_records,
    validate_records,
)
from evaluations.llmrouter.promote_measurements import promote  # noqa: E402


def test_generated_dataset_is_large_balanced_and_paired() -> None:
    queries, routing = generate_records()
    manifest = validate_records(queries, routing)

    assert manifest["query_count"] == 7_212
    assert manifest["routing_record_count"] == 14_424
    assert manifest["domain_counts"] == {
        "general": 803,
        "protein": 3_204,
        "travel": 3_205,
    }
    assert abs(manifest["label_counts"]["cheap"] - manifest["label_counts"]["strong"]) <= 4


def test_value_maxxing_depth_prompt_is_a_strong_training_seed() -> None:
    queries, routing = generate_records()
    prompt = "Explain in depth the concept of value maxxing."
    query = next(record for record in queries["train"] if record["query"] == prompt)
    pair = [record for record in routing["train"] if record["task_id"] == query["task_id"]]

    assert query["expected_model"] == "strong"
    assert max(pair, key=lambda item: item["performance"])["model_name"] == "strong"
    assert all(record["measured"] is False for record in pair)


def test_measured_promotion_uses_quality_threshold_and_high_risk_floor() -> None:
    gold = [
        {
            "task_id": "low-risk",
            "query": "bounded task",
            "risk": "low",
            "expected_model": "strong",
        },
        {
            "task_id": "high-risk",
            "query": "consequential task",
            "risk": "high",
            "expected_model": "strong",
        },
    ]
    measurements = []
    for task_id in ("low-risk", "high-risk"):
        for model_name, scores in (("cheap", (0.94, 0.95, 0.96)), ("strong", (0.97, 0.98, 0.99))):
            measurements.extend(
                {
                    "task_id": task_id,
                    "model_name": model_name,
                    "quality_score": score,
                    "cost_usd": 0.001 if model_name == "cheap" else 0.004,
                }
                for score in scores
            )

    promoted = promote(gold, measurements)
    winners = {
        task_id: next(
            record["value_winner"] for record in promoted if record["task_id"] == task_id
        )
        for task_id in ("low-risk", "high-risk")
    }

    assert winners == {"low-risk": "cheap", "high-risk": "strong"}
    assert all(record["measured"] is True for record in promoted)


def test_materialized_config_uses_absolute_project_data_paths(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    artifacts = tmp_path / "artifacts"
    data_dir.mkdir()

    config_path = materialize_config(data_dir, artifacts)
    config = config_path.read_text(encoding="utf-8")

    assert str(data_dir.resolve() / "routing_data_train.jsonl") in config
    assert str(artifacts.resolve() / "knnrouter.pkl") in config
    assert 'weights: "distance"' in config
    assert 'metric: "cosine"' in config
