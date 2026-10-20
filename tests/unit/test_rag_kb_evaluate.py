from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rag.opensource import query as planner  # noqa: E402
from scripts.rag_kb_evaluate import evaluate  # noqa: E402


class FakeBedrockClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "retrievalResults": [
                {
                    "score": 0.7,
                    "metadata": {
                        "entity_name": "Musee d'Orsay",
                        "category_key": "museums",
                        "corpus_version": "wikivoyage-pilot-v1",
                    },
                },
                {
                    "score": 0.6,
                    "metadata": {
                        "entity_name": "Musee d'Orsay",
                        "category_key": "museums",
                        "corpus_version": "wikivoyage-pilot-v1",
                    },
                },
            ]
        }


def test_bedrock_evaluation_uses_the_same_planner_and_pipeline_contract():
    client = FakeBedrockClient()
    case = {
        "query": "museum with impressionist paintings",
        "destination": "Paris",
        "accept": ["Musee d'Orsay"],
    }

    report = evaluate(
        client,
        "KB123",
        [case],
        top_k=3,
        search_type="SEMANTIC",
        expected_corpus_version="wikivoyage-pilot-v1",
    )

    expected_plan = planner.plan(case["query"], case["destination"])
    request = client.calls[0]
    vector = request["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert request["retrievalQuery"]["text"] == expected_plan["retrieval_query"]
    assert vector["numberOfResults"] == 6
    assert vector["filter"]["andAll"][1]["equals"]["value"] == "museums"
    assert report["hit_at_3"] == 1.0
    assert report["top1_named_entity_rate"] == 1.0
    assert report["comparison_contract"]["deduplicate_entities"] is True


def test_bedrock_evaluation_rejects_results_from_another_corpus_version():
    client = FakeBedrockClient()
    client.retrieve = lambda **_kwargs: {
        "retrievalResults": [
            {
                "score": 0.7,
                "metadata": {
                    "entity_name": "Musee d'Orsay",
                    "corpus_version": "some-other-corpus",
                },
            }
        ]
    }

    try:
        evaluate(
            client,
            "KB123",
            [{"query": "museums", "destination": "Paris", "accept": ["Musee d'Orsay"]}],
            top_k=3,
            search_type="SEMANTIC",
            expected_corpus_version="wikivoyage-pilot-v1",
        )
    except RuntimeError as exc:
        assert "expected only 'wikivoyage-pilot-v1'" in str(exc)
    else:
        raise AssertionError("evaluation accepted a different corpus version")
