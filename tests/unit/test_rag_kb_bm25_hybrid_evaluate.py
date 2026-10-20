from scripts.rag_kb_bm25_hybrid_evaluate import reciprocal_rank_fuse


def _result(chunk_id: str, score: float) -> dict:
    return {
        "score": score,
        "metadata": {"chunk_id": chunk_id, "entity_name": chunk_id},
        "content": {"text": chunk_id},
    }


def test_rrf_rewards_chunks_found_by_both_arms() -> None:
    semantic = [_result("semantic-only", 0.9), _result("shared", 0.8)]
    bm25 = [_result("bm25-only", 12.0), _result("shared", 10.0)]

    fused = reciprocal_rank_fuse(semantic, bm25)

    assert [result["metadata"]["chunk_id"] for result in fused] == [
        "shared",
        "semantic-only",
        "bm25-only",
    ]
    assert fused[0]["fusion_score"] == 2 / 3
    assert fused[0]["semantic_rank"] == 2
    assert fused[0]["bm25_rank"] == 2
    assert fused[0]["semantic_score"] == 0.8
    assert fused[0]["bm25_score"] == 10.0


def test_rrf_rejects_results_without_stable_chunk_ids() -> None:
    result = {"score": 0.9, "metadata": {}, "content": {"text": "missing id"}}

    try:
        reciprocal_rank_fuse([result], [])
    except RuntimeError as exc:
        assert "no chunk_id" in str(exc)
    else:
        raise AssertionError("RRF accepted a result without a chunk_id")
