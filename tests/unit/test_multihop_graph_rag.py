import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rag.multihop import index as multihop_index  # noqa: E402
from rag.multihop.datasets import (  # noqa: E402
    augment_content_graph,
    load_prepared,
    prepare_dataset,
    write_prepared,
)
from rag.multihop.evaluate import (  # noqa: E402
    aggregate_retrieval_metrics,
    case_retrieval_metrics,
    compare_retrieval_metrics,
)
from rag.multihop.neptune import MultihopNeptuneStore  # noqa: E402
from rag.multihop.opensearch import (  # noqa: E402
    MultihopOpenSearchStore,
    multihop_index_mapping,
)
from rag.multihop.retrieve import (  # noqa: E402
    retrieve_graph,
    retrieve_iterative,
)
from rag.multihop.schema import content_entity_phrases  # noqa: E402
from rag.opensource.rerank import bedrock_rerank  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _musique_case() -> dict:
    return {
        "id": "m-1",
        "question": "Who is the spouse of the director of Film X?",
        "answer": "Alex Smith",
        "paragraphs": [
            {
                "idx": 0,
                "title": "Film X",
                "paragraph_text": "Film X was directed by Jane Doe.",
                "is_supporting": True,
            },
            {
                "idx": 1,
                "title": "Jane Doe",
                "paragraph_text": "Jane Doe married Alex Smith in 2001.",
                "is_supporting": True,
            },
            {
                "idx": 2,
                "title": "Film Y",
                "paragraph_text": "Film Y is an unrelated drama.",
                "is_supporting": False,
            },
        ],
        "question_decomposition": [{"id": 1}, {"id": 2}],
    }


def test_prepare_musique_keeps_gold_out_of_index_rows(tmp_path: Path) -> None:
    source = tmp_path / "musique.jsonl"
    _write_jsonl(source, [_musique_case()])

    prepared = prepare_dataset(source, dataset="musique", split="dev")

    assert prepared["manifest"]["gold_fields_indexed"] is False
    assert prepared["cases"][0]["hop_count"] == 2
    assert len(prepared["cases"][0]["supporting_paragraph_ids"]) == 2
    assert all("is_supporting" not in paragraph for paragraph in prepared["paragraphs"])
    assert all("answer" not in paragraph for paragraph in prepared["paragraphs"])
    film = next(row for row in prepared["paragraphs"] if row["title"] == "Film X")
    jane = next(row for row in prepared["paragraphs"] if row["title"] == "Jane Doe")
    assert "Jane Doe" in film["entity_names"]
    assert any(
        mention["paragraph_id"] == film["paragraph_id"]
        and mention["entity_id"] == jane["about_entity_id"]
        for mention in prepared["mentions"]
    )


def test_prepare_musique_excludes_case_ids_before_sampling(tmp_path: Path) -> None:
    first = _musique_case()
    second = _musique_case()
    second["id"] = "2hop__held_out"
    second["question"] = "A held-out question?"
    _write_jsonl(tmp_path / "musique.jsonl", [first, second])

    prepared = prepare_dataset(
        tmp_path / "musique.jsonl",
        dataset="musique",
        sample_size=1,
        exclude_case_ids={str(first["id"])},
    )

    assert [case["case_id"] for case in prepared["cases"]] == ["2hop__held_out"]
    assert prepared["manifest"]["excluded_case_count"] == 1


def test_content_entity_phrases_extracts_names_and_places_conservatively() -> None:
    phrases = content_entity_phrases(
        "The album featured Eric Carmen and was recorded in Rowan County, North Carolina."
    )

    assert "Eric Carmen" in phrases
    assert "Rowan County" in phrases
    assert "North Carolina" in phrases


def test_prepare_connects_paragraphs_through_repeated_content_entities(tmp_path: Path) -> None:
    source = tmp_path / "musique.jsonl"
    case = _musique_case()
    case["paragraphs"][0]["paragraph_text"] = "Film X starred Audrey Hepburn."
    case["paragraphs"][1] = {
        "idx": 1,
        "title": "Awards list",
        "paragraph_text": "Audrey Hepburn received many awards.",
        "is_supporting": True,
    }
    _write_jsonl(source, [case])

    prepared = prepare_dataset(source, dataset="musique", split="dev")
    audrey_mentions = [
        row for row in prepared["mentions"] if row["entity_name"] == "Audrey Hepburn"
    ]

    assert len(audrey_mentions) == 2
    assert len({row["paragraph_id"] for row in audrey_mentions}) == 2


def test_augment_content_graph_upgrades_legacy_prepared_data() -> None:
    prepared = {
        "manifest": {"content_graph_policy": "legacy"},
        "paragraphs": [
            {
                "paragraph_id": "p-1",
                "title": "Film",
                "text": "The film starred Audrey Hepburn.",
                "entity_ids": [],
                "entity_names": [],
            },
            {
                "paragraph_id": "p-2",
                "title": "Awards",
                "text": "Audrey Hepburn received several awards.",
                "entity_ids": [],
                "entity_names": [],
            },
        ],
        "cases": [{"candidate_paragraph_ids": ["p-1", "p-2"]}],
        "mentions": [],
    }

    augmented = augment_content_graph(prepared)

    assert len(augmented["mentions"]) == 2
    assert {row["entity_name"] for row in augmented["mentions"]} == {"Audrey Hepburn"}


def test_prepare_2wiki_isolates_gold_evidence_as_oracle_only(tmp_path: Path) -> None:
    source = tmp_path / "2wiki.json"
    source.write_text(
        json.dumps(
            [
                {
                    "_id": "w-1",
                    "question": "Who is related?",
                    "answer": "B",
                    "type": "bridge",
                    "context": [["A", ["A refers to B."]], ["B", ["B is the answer."]]],
                    "supporting_facts": [["A", 0], ["B", 0]],
                    "evidences": [["A", "related to", "B"]],
                }
            ]
        ),
        encoding="utf-8",
    )

    prepared = prepare_dataset(source, dataset="2wiki", split="dev")

    assert len(prepared["gold_relations"]) == 1
    assert prepared["gold_relations"][0]["source"] == "gold_evidence"
    assert all("gold_evidences" not in row for row in prepared["paragraphs"])
    output = tmp_path / "prepared"
    write_prepared(prepared, output)
    loaded = load_prepared(output)
    assert loaded["manifest"] == prepared["manifest"]
    assert loaded["gold_relations"] == prepared["gold_relations"]


def test_multihop_mapping_has_controlled_mode_and_entity_fields() -> None:
    properties = multihop_index_mapping(768)["mappings"]["properties"]

    assert properties["embedding"]["dimension"] == 768
    assert properties["case_ids"]["type"] == "keyword"
    assert properties["dataset"]["type"] == "keyword"
    assert properties["entity_names"]["type"] == "text"


def test_multihop_hybrid_search_applies_same_case_filter_to_both_arms(monkeypatch) -> None:
    store = object.__new__(MultihopOpenSearchStore)
    calls = []

    def fake_hits(payload):
        calls.append(payload)
        return [
            {
                "_score": 1.0,
                "_source": {"paragraph_id": "p-1", "title": "Film X", "text": "text"},
            }
        ]

    monkeypatch.setattr(store, "_hits", fake_hits)
    results = store.hybrid_search_paragraphs(
        "director",
        [0.1, 0.2],
        dataset="musique",
        case_id="m-1",
        limit=2,
    )

    assert results[0]["node"]["id"] == "p-1"
    vector_filters = calls[0]["query"]["knn"]["embedding"]["filter"]["bool"]["filter"]
    lexical_filters = calls[1]["query"]["bool"]["filter"]
    assert {"term": {"case_ids": "m-1"}} in vector_filters
    assert vector_filters == lexical_filters


def test_neptune_expansion_is_bounded_and_preserves_paths(monkeypatch) -> None:
    store = object.__new__(MultihopNeptuneStore)
    store.namespace = "benchmark"
    captured = {}

    def fake_execute(query, **parameters):
        captured["query"] = query
        captured["parameters"] = parameters
        return [
            {
                "seed_id": "p-1",
                "candidate_id": "p-2",
                "distance": 2,
                "entity_path": ["Jane Doe"],
                "predicates": [],
            }
        ]

    monkeypatch.setattr(store, "_execute", fake_execute)
    expanded = store.expand_paths(
        ["p-1"],
        dataset="musique",
        case_id="m-1",
        max_hops=2,
        relationship_sources=("content",),
    )

    assert "ABOUT|MENTIONS|RELATED_TO*1..4" in captured["query"]
    assert captured["parameters"]["case_id"] == "m-1"
    assert "HAS_PARAGRAPH" in captured["query"]
    assert "size([relation IN relationships(path)" in captured["query"]
    assert expanded["p-1"][0]["entity_path"] == ["Jane Doe"]


class _Embedder:
    provider = "test"
    model = "tiny"
    dimension = 2

    def embed(self, texts):
        return [[float(len(text)), 1.0] for text in texts]

    def embed_one(self, text):
        return self.embed([text])[0]


def test_graph_retrieval_hydrates_ids_and_keeps_provenance() -> None:
    seed = {
        "node": {
            "id": "p-1",
            "paragraph_id": "p-1",
            "title": "Film X",
            "text": "Film X was directed by Jane Doe.",
        },
        "score": 0.03,
    }
    search = SimpleNamespace(
        hybrid_search_paragraphs=lambda *args, **kwargs: [seed],
        get_paragraphs=lambda ids, **kwargs: {
            "p-2": {
                "id": "p-2",
                "paragraph_id": "p-2",
                "title": "Jane Doe",
                "text": "Jane Doe married Alex Smith.",
            }
        },
    )
    graph = SimpleNamespace(
        expand_paths=lambda ids, **kwargs: {
            "p-1": [
                {
                    "paragraph_id": "p-2",
                    "distance": 2,
                    "entity_path": ["Jane Doe"],
                    "predicates": [],
                }
            ]
        }
    )

    results = retrieve_graph(
        "Who is the spouse of the director of Film X?",
        search_store=search,
        graph_store=graph,
        embedder=_Embedder(),
        dataset="musique",
        case_id="m-1",
        top_k=2,
        seed_limit=1,
        candidate_limit=2,
    )

    assert [row["id"] for row in results] == ["p-1", "p-2"]
    assert results[1]["retrieval_source"] == "graph"
    assert results[1]["entity_path"] == ["Jane Doe"]


def test_graph_retrieval_uses_common_depth_and_backfills_sparse_graph() -> None:
    requested_limits = []
    hits = [
        {
            "node": {
                "id": f"p-{rank}",
                "paragraph_id": f"p-{rank}",
                "title": f"Paragraph {rank}",
                "text": "text",
            },
            "score": 1 / (60 + rank),
        }
        for rank in range(1, 5)
    ]

    class Search:
        def hybrid_search_paragraphs(self, *args, **kwargs):
            requested_limits.append(kwargs["limit"])
            return hits

        def get_paragraphs(self, ids, **kwargs):
            return {}

    graph = SimpleNamespace(expand_paths=lambda ids, **kwargs: {pid: [] for pid in ids})

    results = retrieve_graph(
        "question",
        search_store=Search(),
        graph_store=graph,
        embedder=_Embedder(),
        top_k=3,
        seed_limit=1,
        candidate_limit=4,
    )

    assert requested_limits == [4]
    assert [row["id"] for row in results] == ["p-1", "p-2", "p-3"]
    assert results[0]["retrieval_source"] == "seed"
    assert results[1]["retrieval_source"] == "fallback"


def test_graph_evidence_adds_a_bounded_distance_boost() -> None:
    hits = [
        {
            "node": {"id": "seed", "paragraph_id": "seed", "text": "seed"},
            "score": 0.03,
        },
        {
            "node": {"id": "linked", "paragraph_id": "linked", "text": "linked"},
            "score": 0.02,
            "vector_rank": 4,
        },
    ]
    search = SimpleNamespace(
        hybrid_search_paragraphs=lambda *args, **kwargs: hits,
        get_paragraphs=lambda ids, **kwargs: {
            "linked": {"id": "linked", "paragraph_id": "linked", "text": "linked"}
        },
    )
    graph = SimpleNamespace(
        expand_paths=lambda ids, **kwargs: {
            "seed": [
                {
                    "paragraph_id": "linked",
                    "distance": 2,
                    "entity_path": ["entity"],
                    "predicates": ["MENTIONS"],
                }
            ]
        }
    )

    results = retrieve_graph(
        "question",
        search_store=search,
        graph_store=graph,
        embedder=_Embedder(),
        top_k=2,
        seed_limit=1,
        candidate_limit=2,
    )

    linked = next(row for row in results if row["id"] == "linked")
    assert results[0]["id"] == "seed"
    # Search found it too, so its provenance is "both" -- "graph" is reserved for
    # passages only traversal produced.
    assert linked["retrieval_source"] == "both"
    assert linked["score"] > 0.02
    assert linked["vector_rank"] == 4


def test_graph_retrieval_preserves_the_selected_seeds() -> None:
    hits = [
        {
            "node": {"id": pid, "paragraph_id": pid, "text": pid},
            "score": score,
        }
        for pid, score in (
            ("seed-1", 0.03),
            ("noise-2", 0.012),
            ("noise-3", 0.011),
            ("linked-1", 0.010),
            ("linked-2", 0.009),
        )
    ]
    search = SimpleNamespace(
        hybrid_search_paragraphs=lambda *args, **kwargs: hits,
        get_paragraphs=lambda ids, **kwargs: {
            pid: {"id": pid, "paragraph_id": pid, "text": pid} for pid in ids
        },
    )
    graph = SimpleNamespace(
        expand_paths=lambda ids, **kwargs: {
            "seed-1": [
                {"paragraph_id": "linked-1", "distance": 2,
                 "entity_path": ["one"], "predicates": ["MENTIONS"]},
                {"paragraph_id": "linked-2", "distance": 2,
                 "entity_path": ["two"], "predicates": ["MENTIONS"]},
            ],
            "noise-2": [],
            "noise-3": [],
        }
    )

    results = retrieve_graph(
        "question",
        search_store=search,
        graph_store=graph,
        embedder=_Embedder(),
        top_k=3,
        seed_limit=3,
        candidate_limit=5,
    )

    assert [row["id"] for row in results] == ["seed-1", "noise-2", "noise-3"]


def test_graph_only_candidates_reach_reranker_within_shared_budget(monkeypatch) -> None:
    hits = [
        {
            "node": {"id": pid, "paragraph_id": pid, "text": pid},
            "score": score,
        }
        for pid, score in (
            ("seed", 0.03),
            ("search-2", 0.02),
            ("search-3", 0.019),
            ("search-4", 0.018),
        )
    ]
    search = SimpleNamespace(
        hybrid_search_paragraphs=lambda *args, **kwargs: hits,
        get_paragraphs=lambda ids, **kwargs: {
            "graph-only": {
                "id": "graph-only",
                "paragraph_id": "graph-only",
                "text": "bridge evidence",
            }
        },
    )
    graph = SimpleNamespace(
        expand_paths=lambda ids, **kwargs: {
            "seed": [
                {
                    "paragraph_id": "graph-only",
                    "distance": 2,
                    "entity_path": ["bridge"],
                    "predicates": ["MENTIONS"],
                }
            ]
        }
    )
    captured = []

    def fake_rerank(question, candidates, top_k, **kwargs):
        captured.extend(candidates)
        by_id = {candidate["id"]: candidate for candidate in candidates}
        return [
            {**by_id[pid], "rerank_score": 1.0 - rank / 10}
            for rank, pid in enumerate(("graph-only", "search-2", "seed", "search-3"))
        ]

    monkeypatch.setattr("rag.multihop.retrieve.bedrock_rerank", fake_rerank)
    results = retrieve_graph(
        "question",
        search_store=search,
        graph_store=graph,
        embedder=_Embedder(),
        top_k=2,
        seed_limit=1,
        candidate_limit=4,
        bedrock_reranker_model="amazon.rerank-v1:0",
    )

    assert len(captured) == 4
    assert "graph-only" in {row["id"] for row in captured}
    assert [row["id"] for row in results] == ["graph-only", "seed"]


def test_bedrock_reranker_preserves_documents_and_scores() -> None:
    captured = {}

    class Client:
        def rerank(self, **kwargs):
            captured.update(kwargs)
            return {"results": [{"index": 1, "relevanceScore": 0.9}]}

    ranked = bedrock_rerank(
        "question",
        [
            {
                "id": "p-1",
                "title": "One",
                "text": "first",
                "entity_path": ["Start", "Common hub", "One"],
                "entity_degrees": [7],
            },
            {
                "id": "p-2",
                "title": "Two",
                "text": "second",
                "entity_path": ["Starting article", "Bridge entity", "Two"],
            },
        ],
        1,
        region="eu-central-1",
        client=Client(),
    )

    assert ranked == [
        {
            "id": "p-2",
            "title": "Two",
            "text": "second",
            "entity_path": ["Starting article", "Bridge entity", "Two"],
            "rerank_score": 0.9,
        }
    ]
    config = captured["rerankingConfiguration"]["bedrockRerankingConfiguration"]
    assert config["modelConfiguration"]["modelArn"].endswith(
        "/amazon.rerank-v1:0"
    )
    assert config["numberOfResults"] == 1
    second_source = captured["sources"][1]["inlineDocumentSource"]["textDocument"]["text"]
    assert "Specific graph path from a retrieved seed: Starting article -> Bridge entity -> Two" in second_source
    first_source = captured["sources"][0]["inlineDocumentSource"]["textDocument"]["text"]
    assert "Common hub" not in first_source


def test_evaluation_reports_whether_reranking_really_ran() -> None:
    case = {
        "case_id": "case-1",
        "supporting_paragraph_ids": ["p-1"],
    }
    without_scores = case_retrieval_metrics(case, [{"id": "p-1", "text": "one"}])
    with_scores = case_retrieval_metrics(
        case, [{"id": "p-1", "text": "one", "rerank_score": 0.8}]
    )

    assert without_scores["reranker_applied"] is False
    assert with_scores["reranker_applied"] is True


def test_iterative_retrieval_uses_first_hop_entities_in_second_query() -> None:
    calls = []

    class Search:
        def hybrid_search_paragraphs(self, query, vector, **kwargs):
            calls.append(query)
            if len(calls) == 1:
                return [
                    {
                        "node": {
                            "id": "p-1",
                            "paragraph_id": "p-1",
                            "title": "Film X",
                            "text": "Film X was directed by Jane Doe.",
                            "entity_names": ["Jane Doe"],
                        },
                        "score": 0.03,
                    }
                ]
            return [
                {
                    "node": {
                        "id": "p-2",
                        "paragraph_id": "p-2",
                        "title": "Jane Doe",
                        "text": "Jane Doe married Alex Smith.",
                    },
                    "score": 0.02,
                }
            ]

    results = retrieve_iterative(
        "Who is the spouse of the director of Film X?",
        search_store=Search(),
        embedder=_Embedder(),
        top_k=2,
        seed_limit=1,
    )

    assert "Jane Doe" in calls[1]
    assert [row["id"] for row in results] == ["p-1", "p-2"]


def test_index_resume_repairs_stores_and_content_mode_does_not_write_gold(
    tmp_path: Path,
) -> None:
    source = tmp_path / "musique.jsonl"
    _write_jsonl(source, [_musique_case()])
    prepared = prepare_dataset(source, dataset="musique")
    prepared["gold_relations"] = [
        {
            "source_entity_id": "a",
            "source_entity_name": "A",
            "predicate": "p",
            "target_entity_id": "b",
            "target_entity_name": "B",
            "source": "gold_evidence",
            "case_id": "m-1",
        }
    ]
    output = tmp_path / "prepared"
    write_prepared(prepared, output)

    class Search:
        rows = []

        def ensure_index(self, dimension):
            return False

        def existing_paragraph_ids(self, ids):
            return {ids[0]}

        def upsert_paragraphs(self, rows):
            self.rows.extend(rows)

        def count(self):
            return len(prepared["paragraphs"])

    class Graph:
        paragraphs = []
        mentions = []
        relations = []
        memberships = []

        def existing_paragraph_ids(self, ids):
            return set(ids[1:])

        def upsert_paragraphs(self, rows):
            self.paragraphs.extend(rows)

        def upsert_mentions(self, rows):
            self.mentions.extend(rows)

        def upsert_relations(self, rows):
            self.relations.extend(rows)

        def upsert_case_memberships(self, rows):
            self.memberships.extend(rows)

        def counts(self):
            return {"paragraphs": len(prepared["paragraphs"]), "entities": 3}

    search = Search()
    graph = Graph()
    result = multihop_index.index_prepared(
        output,
        search_store=search,
        graph_store=graph,
        embedder=_Embedder(),
        graph_source="content-derived",
    )

    assert len(search.rows) == len(prepared["paragraphs"]) - 1
    assert len(graph.paragraphs) == 1
    assert graph.mentions
    assert graph.memberships == prepared["cases"]
    assert graph.relations == []
    assert result["oracle_graph"] is False


def test_new_index_skips_resume_search(tmp_path: Path) -> None:
    source = tmp_path / "musique.jsonl"
    _write_jsonl(source, [_musique_case()])
    prepared = prepare_dataset(source, dataset="musique")
    output = tmp_path / "prepared"
    write_prepared(prepared, output)

    class Search:
        rows = []

        def ensure_index(self, dimension):
            return True

        def existing_paragraph_ids(self, ids):
            raise AssertionError("a newly created index must not be searched")

        def upsert_paragraphs(self, rows):
            self.rows.extend(rows)

        def count(self):
            return len(self.rows)

    class Graph:
        def existing_paragraph_ids(self, ids):
            return set()

        def upsert_paragraphs(self, rows):
            pass

        def upsert_mentions(self, rows):
            pass

        def upsert_relations(self, rows):
            pass

        def upsert_case_memberships(self, rows):
            pass

        def counts(self):
            return {"paragraphs": len(prepared["paragraphs"])}

    search = Search()
    result = multihop_index.index_prepared(
        output, search_store=search, graph_store=Graph(), embedder=_Embedder()
    )

    assert result["opensearch_index_created"] is True
    assert len(search.rows) == len(prepared["paragraphs"])


def test_evaluation_reports_strict_all_support_recall_by_hop() -> None:
    cases = [
        {
            "case_id": "one",
            "question_type": "2hop",
            "hop_count": 2,
            "supporting_paragraph_ids": ["p-1", "p-2"],
        },
        {
            "case_id": "two",
            "question_type": "2hop",
            "hop_count": 2,
            "supporting_paragraph_ids": ["p-3", "p-4"],
        },
    ]

    metrics = aggregate_retrieval_metrics(
        cases,
        lambda case: (
            [{"id": "p-1"}, {"id": "p-2"}] if case["case_id"] == "one" else [{"id": "p-3"}]
        ),
        rank_cutoff=4,
    )

    assert metrics["all_support_recall"] == 0.5
    assert metrics["mean_support_recall"] == 0.75
    assert metrics["ranking_cutoff"] == 4
    assert metrics["hit_at_4"] == 1.0
    assert metrics["mrr_at_4"] == 1.0
    assert metrics["first_support_hit_at_4"] == 1.0
    assert metrics["first_support_mrr_at_4"] == 1.0
    assert metrics["chain_hit_at_4"] == 0.5
    assert metrics["chain_mrr_at_4"] == 0.25
    assert metrics["by_hop"]["2"]["case_count"] == 2


def test_ranking_metrics_use_first_gold_rank_and_cutoff() -> None:
    case = {
        "case_id": "ranked",
        "supporting_paragraph_ids": ["gold-1", "gold-2"],
    }

    metrics = case_retrieval_metrics(
        case,
        [
            {"id": "noise-1"},
            {"id": "noise-2"},
            {"id": "gold-1"},
            {"id": "noise-3"},
            {"id": "noise-4"},
            {"id": "gold-2"},
        ],
        rank_cutoff=4,
    )

    assert metrics["first_relevant_rank"] == 3
    assert metrics["ranking_cutoff"] == 4
    assert metrics["hit_at_4"] is True
    assert metrics["reciprocal_rank_at_4"] == 1 / 3
    assert metrics["first_support_hit_at_4"] is True
    assert metrics["first_support_reciprocal_rank_at_4"] == 1 / 3
    assert metrics["chain_hit_at_4"] is False
    assert metrics["chain_completion_rank"] == 0
    assert metrics["reciprocal_chain_rank_at_4"] == 0.0
    assert metrics["all_support_recalled"] is False
    assert metrics["retrieved_ids"] == ["noise-1", "noise-2", "gold-1", "noise-3"]


def test_paired_comparison_reports_graph_wins_ties_losses() -> None:
    baseline = {
        "ranking_cutoff": 4,
        "cases": [
            {"case_id": "a", "all_support_recalled": False, "support_recall": 0.5,
             "support_precision": 0.5, "hit_at_4": True,
             "reciprocal_rank_at_4": 1.0},
            {"case_id": "b", "all_support_recalled": True, "support_recall": 1.0,
             "support_precision": 0.5, "hit_at_4": True,
             "reciprocal_rank_at_4": 1.0},
        ]
    }
    graph = {
        "ranking_cutoff": 4,
        "cases": [
            {"case_id": "a", "all_support_recalled": True, "support_recall": 1.0,
             "support_precision": 0.5, "hit_at_4": True,
             "reciprocal_rank_at_4": 1.0},
            {"case_id": "b", "all_support_recalled": False, "support_recall": 0.75,
             "support_precision": 0.5, "hit_at_4": True,
             "reciprocal_rank_at_4": 0.5},
        ]
    }

    comparison = compare_retrieval_metrics(baseline, graph)

    assert comparison["case_count"] == 2
    assert comparison["paired_all_support"] == {"wins": 1, "ties": 0, "losses": 1}
    assert comparison["paired_support_recall"] == {"wins": 1, "ties": 0, "losses": 1}
    assert comparison["candidate_minus_baseline"]["support_recall"] == 0.125


def test_graph_seed_selection_keeps_every_selected_anchor() -> None:
    from rag.multihop.retrieve import _select_with_graph_seeds

    ranked = [
        {"id": "g1", "retrieval_source": "graph", "seed_id": "s1"},
        {"id": "g2", "retrieval_source": "graph", "seed_id": "s2"},
        {"id": "g3", "retrieval_source": "graph", "seed_id": "s1"},
        {"id": "s2", "retrieval_source": "seed"},
        {"id": "s1", "retrieval_source": "seed"},
    ]
    assert [r["id"] for r in _select_with_graph_seeds(ranked, 4)] == [
        "g1", "g2", "s2", "s1"
    ]
    # An unfittable second bundle must not displace the first anchor.
    assert [r["id"] for r in _select_with_graph_seeds(ranked, 3)] == [
        "g1", "g3", "s1"
    ]


def test_graph_seed_selection_tight_budget_and_missing_anchor() -> None:
    from rag.multihop.retrieve import _select_with_graph_seeds

    ranked = [
        {"id": "orphan", "retrieval_source": "graph", "seed_id": "missing"},
        {"id": "g", "retrieval_source": "graph", "seed_id": "s"},
        {"id": "best-standalone", "retrieval_source": "fallback"},
        {"id": "s", "retrieval_source": "seed"},
    ]
    assert _select_with_graph_seeds(ranked, 0) == []
    assert [r["id"] for r in _select_with_graph_seeds(ranked, 1)] == ["best-standalone"]
    assert [r["id"] for r in _select_with_graph_seeds(ranked, 2)] == ["g", "s"]


def test_graph_seed_selection_applies_without_reranking_and_on_failure(monkeypatch) -> None:
    from rag.multihop.retrieve import _finalize
    from rag.opensource.rerank import RerankError

    candidates = [
        {"id": "g", "retrieval_source": "graph", "seed_id": "s"},
        {"id": "other", "retrieval_source": "fallback"},
        {"id": "s", "retrieval_source": "seed"},
    ]
    def fail(*args, **kwargs):
        raise RerankError("unavailable")

    monkeypatch.setattr("rag.multihop.retrieve.bedrock_rerank", fail)
    for model in ("", "test-model"):
        selected = _finalize(
            "question", candidates, top_k=2, ensure_graph_seed=True,
            bedrock_reranker_model=model,
        )
        assert [r["id"] for r in selected] == ["g", "s"]
        assert all("rerank_score" not in r for r in selected)
