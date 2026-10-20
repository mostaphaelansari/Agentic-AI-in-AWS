from scripts.rag_kb_bm25_hybrid_search import _fill_missing_semantic_scores, _rows


def test_rows_project_hybrid_scores_and_arm_ranks() -> None:
    results = [
        {
            "metadata": {"entity_name": "The Louvre"},
            "fusion_score": 0.75,
            "semantic_score": 0.64,
            "semantic_rank": 1,
            "bm25_score": 12.5,
            "bm25_rank": 3,
        }
    ]

    assert _rows(results) == [
        {
            "rank": 1,
            "museum": "The Louvre",
            "rrf": 0.75,
            "semantic_score": 0.64,
            "semantic_rank": 1,
            "bm25_score": 12.5,
            "bm25_rank": 3,
        }
    ]


def test_bm25_only_winner_gets_a_semantic_score_without_a_fake_rank() -> None:
    class Client:
        def retrieve(self, **kwargs):
            assert kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"] == {
                "equals": {"key": "chunk_id", "value": "pompidou"}
            }
            return {"retrievalResults": [{"score": 0.51}]}

    results = [
        {
            "metadata": {"chunk_id": "pompidou"},
            "semantic_score": None,
            "semantic_rank": None,
        }
    ]

    _fill_missing_semantic_scores(Client(), "KB123", "museum query", results)

    assert results[0]["semantic_score"] == 0.51
    assert results[0]["semantic_rank"] is None
