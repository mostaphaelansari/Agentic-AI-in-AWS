"""Keep the 2Wiki bridge test independent of sampling and comparison controls."""

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rag.multihop.datasets import TWO_WIKI_BRIDGE_TYPES, prepare_dataset  # noqa: E402
from rag.multihop.evaluate import (  # noqa: E402
    aggregate_retrieval_metrics,
    question_type_cohort_metrics,
)
from scripts import multihop_graph_rag as cli  # noqa: E402


def _records():
    return [
        {
            "_id": f"{kind}-{index}",
            "type": kind,
            "question": f"Who directed Film {kind} {index}?",
            "context": [
                [f"Film {kind} {index}", ["Directed by Jane Doe."]],
                ["Jane Doe", ["Jane Doe was born in Paris."]],
                ["Distractor", ["An unrelated passage."]],
            ],
            "supporting_facts": [[f"Film {kind} {index}", 0], ["Jane Doe", 0]],
            "evidences": [[f"Film {kind} {index}", "director", "Jane Doe"]],
        }
        for kind in ("comparison", "bridge_comparison", "compositional", "inference")
        for index in range(6)
    ]


def test_prepare_filters_before_balanced_sampling_and_preserves_distractors(tmp_path):
    source = tmp_path / "dev.json"
    source.write_text(json.dumps(_records()))
    kwargs = dict(dataset="2wiki", question_types=TWO_WIKI_BRIDGE_TYPES,
                  sample_size=8, seed=17, exclude_case_ids={"inference-0"})
    prepared = prepare_dataset(source, **kwargs)
    assert prepared == prepare_dataset(source, **kwargs)
    assert Counter(c["question_type"] for c in prepared["cases"]) == {
        "compositional": 4, "inference": 4,
    }
    assert "inference-0" not in {c["case_id"] for c in prepared["cases"]}
    assert all(len(c["candidate_paragraph_ids"]) == 3 for c in prepared["cases"])
    assert all("comparison" not in p["title"] for p in prepared["paragraphs"])
    assert len(prepared["gold_relations"]) == 8
    assert prepared["manifest"]["question_types"] == list(TWO_WIKI_BRIDGE_TYPES)
    assert prepared["manifest"]["question_type_counts"] == {"compositional": 4, "inference": 4}


@pytest.mark.parametrize("requested", [[], ["infernece"], ["inference", "infernece"]])
def test_prepare_rejects_empty_or_partly_unknown_types(tmp_path, requested):
    source = tmp_path / "dev.json"
    source.write_text(json.dumps(_records()))
    with pytest.raises(ValueError, match="available types"):
        prepare_dataset(source, dataset="2wiki", question_types=requested)


def test_cohort_pools_cases_instead_of_averaging_type_means():
    cases = [
        {"case_id": str(i), "question_type": kind, "supporting_paragraph_ids": ["gold"]}
        for i, kind in enumerate(["compositional"] * 3 + ["inference", "comparison"])
    ]
    flat = aggregate_retrieval_metrics(cases, lambda c: [], rank_cutoff=5)
    graph = aggregate_retrieval_metrics(
        cases, lambda c: [{"id": "gold"}] if c["question_type"] != "inference" else [],
        rank_cutoff=5,
    )
    report = question_type_cohort_metrics({"flat": flat, "graph": graph}, TWO_WIKI_BRIDGE_TYPES)
    assert report["strategies"]["graph"]["case_count"] == 4
    assert report["strategies"]["graph"]["all_support_recall"] == 0.75
    assert report["comparisons"]["graph_vs_flat"]["paired_all_support"] == {
        "wins": 3, "ties": 1, "losses": 0,
    }
    empty = question_type_cohort_metrics({"graph": graph}, ["absent"])
    assert empty["strategies"]["graph"]["case_count"] == 0
    assert empty["comparisons"] == {}


def test_evaluate_filters_before_limit_without_rebuilding_corpus(tmp_path, monkeypatch, capsys):
    source = tmp_path / "dev.json"
    source.write_text(json.dumps(_records()))
    prepared = prepare_dataset(source, dataset="2wiki")
    seen = []

    class Store:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(cli, "load_prepared", lambda path: prepared)
    monkeypatch.setattr(cli, "_search_store", lambda args: Store())
    monkeypatch.setattr(cli, "_embedder", lambda args: object())
    monkeypatch.setattr(cli, "run_provenance", lambda **kwargs: {})
    monkeypatch.setattr(cli, "_retrieve", lambda *args, **kwargs: seen.append(kwargs["case_id"]) or [])
    args = cli.build_parser().parse_args([
        "evaluate", "--prepared", str(tmp_path), "--strategy", "flat",
        "--question-type", "inference", "--limit", "2", "--top-k", "5", "--no-rerank",
    ])
    cli.command_evaluate(args)
    report = json.loads(capsys.readouterr().out)
    assert seen == ["inference-0", "inference-1"]
    assert report["primary_cohort"] == "bridge"
    assert report["question_type_cohorts"]["bridge"]["strategies"]["flat"]["case_count"] == 2
    assert len(prepared["cases"]) == 24


def test_prepare_cli_passes_repeated_types_to_sampler(tmp_path, capsys):
    source = tmp_path / "dev.json"
    source.write_text(json.dumps(_records()))
    args = cli.build_parser().parse_args([
        "prepare", "--dataset", "2wiki", "--input", str(source), "--output", str(tmp_path / "out"),
        "--question-type", "compositional", "--question-type", "inference", "--sample-size", "4",
    ])
    cli.command_prepare(args)
    assert json.loads(capsys.readouterr().out)["question_type_counts"] == {
        "compositional": 2, "inference": 2,
    }
