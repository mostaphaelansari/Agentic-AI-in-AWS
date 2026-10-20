"""Coverage for the diagnostic instrumentation added after the held-out audit.

Each test pins one thing the audit could not answer because the pipeline did not
record it: which reasoning hop a lost support belonged to, whether traversal
actually contributed a passage, whether a chain died in retrieval or in
selection, and whether a reranker quietly stopped running.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rag.multihop.datasets import _hop_ordered_supports, _musique_record  # noqa: E402
from rag.multihop.evaluate import (  # noqa: E402
    aggregate_retrieval_metrics,
    case_retrieval_metrics,
    compare_retrieval_metrics,
    stage_attribution,
)
from rag.multihop.retrieve import (  # noqa: E402
    GRAPH_DISTANCE_WEIGHT,
    HYBRID_ARM_COUNT,
    RRF_RANK_CONSTANT,
    retrieve_graph,
)
from rag.multihop.selection import (  # noqa: E402
    MAX_PASSAGES_PER_ARTICLE,
    EvidenceSelectionError,
    select_evidence,
)


# --------------------------------------------------------------------------
# Hop ordering of gold supports
# --------------------------------------------------------------------------
def test_gold_supports_follow_decomposition_order_not_identifier_order():
    record = {
        "id": "3hop__a_b_c",
        "question": "q",
        "answer": "a",
        "paragraphs": [
            {"title": "Third", "paragraph_text": "third", "is_supporting": True},
            {"title": "Distractor", "paragraph_text": "noise", "is_supporting": False},
            {"title": "First", "paragraph_text": "first", "is_supporting": True},
            {"title": "Second", "paragraph_text": "second", "is_supporting": True},
        ],
        "question_decomposition": [
            {"paragraph_support_idx": 2},
            {"paragraph_support_idx": 3},
            {"paragraph_support_idx": 0},
        ],
    }
    case = _musique_record(record, "dev")
    titles = {p["paragraph_id"]: p["title"] for p in case["paragraphs"]}
    assert [titles[pid] for pid in case["supporting_paragraph_ids"]] == [
        "First",
        "Second",
        "Third",
    ]
    assert case["hop_count"] == 3


def test_hop_ordering_survives_filtered_paragraphs_and_missing_indices():
    # Index 1 is dropped for having no text, so a naive enumerate would misalign.
    record = {
        "id": "2hop__a_b",
        "question": "q",
        "answer": "a",
        "paragraphs": [
            {"title": "Second", "paragraph_text": "second", "is_supporting": True},
            {"title": "Empty", "paragraph_text": "   ", "is_supporting": False},
            {"title": "First", "paragraph_text": "first", "is_supporting": True},
        ],
        "question_decomposition": [{"paragraph_support_idx": 2}, {"no_index": True}],
    }
    case = _musique_record(record, "dev")
    titles = {p["paragraph_id"]: p["title"] for p in case["paragraphs"]}
    # The referenced step leads; the unreferenced support still appears exactly once.
    assert [titles[pid] for pid in case["supporting_paragraph_ids"]] == ["First", "Second"]


def test_hop_ordering_rejects_bool_and_out_of_range_indices():
    index_to_id = {0: "p0", 1: "p1"}
    steps = [
        {"paragraph_support_idx": True},  # bool is an int subclass; must not match p1
        {"paragraph_support_idx": 99},
        {"paragraph_support_idx": 1},
    ]
    assert _hop_ordered_supports(steps, index_to_id, ["p0", "p1"]) == ["p1", "p0"]
    assert _hop_ordered_supports([], index_to_id, ["p1", "p0"]) == ["p1", "p0"]


# --------------------------------------------------------------------------
# Provenance and score comparability
# --------------------------------------------------------------------------
def _stores(hits, paths):
    search = SimpleNamespace(
        hybrid_search_paragraphs=lambda *a, **kw: hits,
        get_paragraphs=lambda ids, **kw: {pid: {"id": pid, "text": pid} for pid in ids},
    )
    return search, SimpleNamespace(expand_paths=lambda *a, **kw: paths)


def test_provenance_separates_graph_only_from_found_by_both():
    hits = [
        {"node": {"id": pid, "text": pid}, "score": s}
        for pid, s in [("seed", 0.03), ("shared", 0.02)]
    ]
    paths = {
        "seed": [
            {
                "paragraph_id": "shared",
                "distance": 2,
                "entity_path": ["e"],
                "entity_degrees": [2],
                "predicates": [],
            },
            {
                "paragraph_id": "graph-only",
                "distance": 2,
                "entity_path": ["e"],
                "entity_degrees": [2],
                "predicates": [],
            },
        ]
    }
    search, graph = _stores(hits, paths)
    results = retrieve_graph(
        "q",
        search_store=search,
        graph_store=graph,
        embedder=SimpleNamespace(embed_one=lambda q: []),
        seed_limit=1,
        top_k=3,
        candidate_limit=3,
    )
    sources = {row["id"]: row["retrieval_source"] for row in results}
    assert sources == {"seed": "seed", "shared": "both", "graph-only": "graph"}


def test_graph_score_is_on_the_same_scale_as_a_hybrid_hit():
    # A one-hop graph candidate must be a documented fraction of a top hybrid
    # score, not an order of magnitude below every search result.
    best_hybrid = HYBRID_ARM_COUNT / (RRF_RANK_CONSTANT + 1)
    hits = [{"node": {"id": "seed", "text": "seed"}, "score": best_hybrid}]
    paths = {
        "seed": [
            {
                "paragraph_id": "near",
                "distance": 1,
                "entity_path": ["e"],
                "entity_degrees": [2],
                "predicates": [],
            }
        ]
    }
    search, graph = _stores(hits, paths)
    results = retrieve_graph(
        "q",
        search_store=search,
        graph_store=graph,
        embedder=SimpleNamespace(embed_one=lambda q: []),
        seed_limit=1,
        top_k=2,
        candidate_limit=2,
    )
    near = next(row for row in results if row["id"] == "near")
    assert near["score"] == pytest.approx(GRAPH_DISTANCE_WEIGHT * best_hybrid)


# --------------------------------------------------------------------------
# Diversity cap on the final set
# --------------------------------------------------------------------------
def _selector(indices_by_attempt, titles):
    calls = []

    def converse(**kwargs):
        indices = indices_by_attempt[min(len(calls), len(indices_by_attempt) - 1)]
        calls.append(kwargs)
        return {
            "stopReason": "tool_use",
            "usage": {},
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "t",
                                "name": "submit_evidence",
                                "input": {
                                    "indices": indices,
                                    "evidence": [
                                        {"index": i, "quote": titles[i], "role": "r"}
                                        for i in indices
                                    ],
                                },
                            }
                        }
                    ],
                }
            },
        }

    return converse, calls


def test_selector_retries_when_one_article_crowds_the_budget():
    titles = ["Hub", "Hub", "Hub", "Other"]
    candidates = [{"id": str(i), "title": t, "text": t} for i, t in enumerate(titles)]
    converse, calls = _selector([[0, 1, 2], [0, 1, 3]], titles)
    result = select_evidence(
        "q", candidates, 3, model_id="m", region="r", client=SimpleNamespace(converse=converse)
    )
    assert len(calls) == 2
    assert f"at most {MAX_PASSAGES_PER_ARTICLE} passages" in str(calls[1]["messages"])
    assert [row["id"] for row in result] == ["0", "1", "3"]


def test_diversity_cap_stands_down_when_the_pool_cannot_satisfy_it():
    # Two articles, four slots: the cap is unsatisfiable and must not loop.
    titles = ["A", "A", "B", "B"]
    candidates = [{"id": str(i), "title": t, "text": t} for i, t in enumerate(titles)]
    converse, calls = _selector([[0, 1, 2, 3]], titles)
    result = select_evidence(
        "q", candidates, 4, model_id="m", region="r", client=SimpleNamespace(converse=converse)
    )
    assert len(calls) == 1
    assert len(result) == 4


def test_selector_fails_when_it_keeps_crowding_one_article():
    titles = ["Hub"] * 4 + ["Other"]
    candidates = [{"id": str(i), "title": t, "text": t} for i, t in enumerate(titles)]
    converse, _ = _selector([[0, 1, 2]], titles)
    with pytest.raises(EvidenceSelectionError, match="after 3 attempts"):
        select_evidence(
            "q", candidates, 3, model_id="m", region="r", client=SimpleNamespace(converse=converse)
        )


# --------------------------------------------------------------------------
# Rescue rate, stage attribution, reranker fallback
# --------------------------------------------------------------------------
def _case(cid, hop, gold):
    return {
        "case_id": cid,
        "hop_count": hop,
        "question_type": f"{hop}hop",
        "supporting_paragraph_ids": gold,
    }


def _report(cases, retrieved, cutoff):
    return aggregate_retrieval_metrics(
        cases,
        lambda case: [{"id": pid, "text": pid} for pid in retrieved[case["case_id"]]],
        rank_cutoff=cutoff,
    )


def test_rescue_counts_only_gold_the_baseline_arm_missed():
    cases = [_case("a", 2, ["g1", "g2"]), _case("b", 4, ["g3"]), _case("c", 3, ["g4"])]
    flat = _report(cases, {"a": ["g1", "x"], "b": ["x"], "c": ["g4"]}, 2)
    graph = _report(cases, {"a": ["g1", "g2"], "b": ["g3"], "c": ["g4"]}, 2)
    rescue = compare_retrieval_metrics(flat, graph)["rescue"]
    # "c" found the same gold as flat, so it is not a rescue.
    assert rescue["cases"] == 2
    assert rescue["supports"] == 2
    assert rescue["case_ids"] == ["a", "b"]
    assert rescue["by_hop"]["4"] == {"cases": 1, "rescued": 1}
    assert rescue["by_hop"]["3"] == {"cases": 1, "rescued": 0}


def test_stage_attribution_separates_pool_misses_from_selection_losses():
    cases = [_case("kept", 2, ["g1"]), _case("dropped", 3, ["g2"]), _case("absent", 4, ["g3"])]
    pool = _report(cases, {"kept": ["g1"], "dropped": ["x", "g2"], "absent": ["x", "y"]}, 2)
    final = _report(cases, {"kept": ["g1"], "dropped": ["x"], "absent": ["x"]}, 1)
    attribution = stage_attribution(pool, final)
    assert attribution["counts"] == {
        "complete": 1,
        "lost_in_selection": 1,
        "never_pooled": 1,
    }
    assert attribution["case_ids"]["lost_in_selection"] == ["dropped"]
    assert attribution["case_ids"]["never_pooled"] == ["absent"]


def test_stage_attribution_rejects_a_pool_narrower_than_the_final_set():
    cases = [_case("a", 2, ["g1"])]
    narrow = _report(cases, {"a": ["g1"]}, 1)
    wide = _report(cases, {"a": ["g1"]}, 2)
    with pytest.raises(ValueError, match="at least as wide"):
        stage_attribution(narrow, wide)


def test_reranker_fallback_is_reported_rather_than_silent():
    case = _case("a", 2, ["g1"])
    degraded = [{"id": "g1", "text": "g1", "reranker_fallback": True}]
    row = case_retrieval_metrics(case, degraded, rank_cutoff=1)
    assert row["reranker_fallback"] is True
    assert (
        case_retrieval_metrics(case, [{"id": "g1", "text": "g1"}], rank_cutoff=1)[
            "reranker_fallback"
        ]
        is False
    )


def test_gold_support_ids_are_retained_in_hop_order():
    case = _case("a", 3, ["first", "second", "third"])
    row = case_retrieval_metrics(case, [{"id": "second", "text": "s"}], rank_cutoff=1)
    assert row["gold_support_ids"] == ["first", "second", "third"]
    assert row["missing_support_ids"] == ["first", "third"]


def test_chain_metrics_only_score_after_the_last_required_support():
    case = _case("a", 3, ["first", "second", "third"])
    incomplete = case_retrieval_metrics(
        case,
        [{"id": "first"}, {"id": "noise"}, {"id": "second"}],
        rank_cutoff=3,
    )
    complete = case_retrieval_metrics(
        case,
        [{"id": "first"}, {"id": "noise"}, {"id": "second"}, {"id": "third"}],
        rank_cutoff=4,
    )

    # Any-support metrics are positive for both rows, while only one chain is done.
    assert incomplete["hit_at_3"] is True
    assert incomplete["chain_hit_at_3"] is False
    assert incomplete["reciprocal_chain_rank_at_3"] == 0.0
    assert complete["chain_hit_at_4"] is True
    assert complete["chain_completion_rank"] == 4
    assert complete["reciprocal_chain_rank_at_4"] == 0.25
