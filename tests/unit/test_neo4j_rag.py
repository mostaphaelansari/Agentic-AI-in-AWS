import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rag.neo4j import retrieve as graph_retrieve  # noqa: E402
from rag.neo4j.index import next_relationships, record_row  # noqa: E402


def _record(chunk_id: str, ordinal: int, *, entity_name: str = "", page_id: int = 42) -> dict:
    return {
        "chunk_id": chunk_id,
        "embedding_text": f"Destination: Paris\nChunk {chunk_id}",
        "metadata": {
            "page_id": page_id,
            "title": "Paris/7th arrondissement",
            "destination_key": "paris",
            "category_key": "museums",
            "document_type": "listing",
            "entity_name": entity_name,
            "section": "See > Museums",
            "chunk_ordinal": ordinal,
            "source_url": "https://example.test/paris",
            "history_url": "https://example.test/paris/history",
            "snapshot_date": "2026-08-25",
            "license": "CC BY-SA 4.0",
        },
    }


def test_record_row_builds_stable_graph_keys() -> None:
    row = record_row(_record("chunk-1", 3, entity_name="Musée d'Orsay"), "pilot-v1")

    assert row["page_key"] == "pilot-v1:42"
    assert row["destination"] == "Paris"
    assert row["place_key"] == "paris:musée d'orsay"
    assert row["ordinal"] == 3


def test_next_relationships_orders_chunks_within_each_page() -> None:
    rows = [
        record_row(_record("third", 3), "pilot-v1"),
        record_row(_record("first", 1), "pilot-v1"),
        record_row(_record("second", 2), "pilot-v1"),
    ]

    assert next_relationships(rows) == [
        {"previous_id": "first", "current_id": "second"},
        {"previous_id": "second", "current_id": "third"},
    ]


def test_next_relationships_does_not_bridge_filtered_out_chunks() -> None:
    rows = [
        record_row(_record("first", 1), "pilot-v1"),
        record_row(_record("third", 3), "pilot-v1"),
        record_row(_record("other-page", 2, page_id=99), "pilot-v1"),
    ]

    assert next_relationships(rows) == []


def _seed(
    chunk_id: str,
    entity_name: str,
    score: float,
    *,
    text: str | None = None,
) -> dict:
    return {
        "score": score,
        "node": {
            "id": chunk_id,
            "destination_key": "paris",
            "entity_name": entity_name,
            "text": text or f"Passage about {entity_name or chunk_id}",
        },
    }


def test_deduplicate_seeds_keeps_the_best_normalized_place() -> None:
    seeds = [
        _seed("best", "Musée des Arts décoratifs", 0.92),
        _seed("duplicate", "MUSÉE DES ARTS DÉCORATIFS", 0.90),
        _seed("louvre", "The Louvre", 0.89),
    ]

    unique = graph_retrieve.deduplicate_seeds(seeds)

    assert [seed["node"]["id"] for seed in unique] == ["best", "louvre"]


def test_deduplicate_seeds_keeps_distinct_section_prose() -> None:
    seeds = [_seed("section-1", "", 0.9), _seed("section-2", "", 0.8)]

    assert graph_retrieve.deduplicate_seeds(seeds) == seeds


def test_graph_document_contains_seed_and_unique_traversed_context() -> None:
    result = {
        **_seed("louvre", "The Louvre", 0.9, text="The Louvre museum"),
        "adjacent": [
            {"text": "Musée d'Orsay"},
            {"text": "  Musée d'Orsay  "},
        ],
        "related": [{"text": "Another Louvre description"}],
    }

    document = graph_retrieve.graph_document(result)

    assert document.count("Musée d'Orsay") == 1
    assert "Seed:\nThe Louvre museum" in document
    assert "Related place context:\nAnother Louvre description" in document


def test_finalize_results_reranks_graph_documents(monkeypatch) -> None:
    results = [
        {**_seed("first", "First", 0.95), "adjacent": [], "related": []},
        {**_seed("second", "Second", 0.90), "adjacent": [], "related": []},
    ]
    captured = SimpleNamespace(query="", documents=[])

    def fake_rerank(query, candidates, top_k, *, url, timeout):
        captured.query = query
        captured.documents = [candidate["payload"]["text"] for candidate in candidates]
        assert timeout == 120
        reranked = dict(candidates[1])
        reranked["rerank_score"] = 0.88
        return [reranked][:top_k]

    monkeypatch.setattr(graph_retrieve, "rerank", fake_rerank)

    ranked, ranking = graph_retrieve.finalize_results(
        "best museum", results, 1, reranker_url="http://reranker"
    )

    assert ranking == "CROSS_ENCODER"
    assert ranked[0]["node"]["id"] == "second"
    assert ranked[0]["rerank_score"] == 0.88
    assert "payload" not in ranked[0]
    assert captured.query == "best museum"
    assert captured.documents[0].startswith("Seed:")
    assert len(captured.documents[0]) <= graph_retrieve.MAX_GRAPH_RERANK_CHARS


def test_finalize_results_falls_back_when_reranker_fails(monkeypatch) -> None:
    results = [
        {**_seed("first", "First", 0.95), "adjacent": [], "related": []},
        {**_seed("second", "Second", 0.90), "adjacent": [], "related": []},
    ]

    def fail(*_args, **_kwargs):
        raise graph_retrieve.RerankError("offline")

    monkeypatch.setattr(graph_retrieve, "rerank", fail)

    ranked, ranking = graph_retrieve.finalize_results(
        "museum", results, 1, reranker_url="http://reranker"
    )

    assert ranking == "SEMANTIC_FALLBACK"
    assert ranked == results[:1]
