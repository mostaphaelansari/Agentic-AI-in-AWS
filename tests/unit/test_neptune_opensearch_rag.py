import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rag.neptune_opensearch import index as dual_index  # noqa: E402
from rag.neptune_opensearch.neptune import NeptuneAnalyticsGraphStore  # noqa: E402
from rag.neptune_opensearch.opensearch import (  # noqa: E402
    OpenSearchGraphStore,
    index_mapping,
)
from rag.neptune_opensearch.retrieve import expand_seed_context  # noqa: E402


def _hit(chunk_id: str, name: str, score: float) -> dict:
    return {
        "_id": chunk_id,
        "_score": score,
        "_source": {
            "chunk_id": chunk_id,
            "entity_name": name,
            "destination_key": "paris",
            "category_key": "museums",
            "text": f"Passage about {name}",
        },
    }


def test_opensearch_mapping_has_filterable_metadata_and_cosine_vector() -> None:
    mapping = index_mapping(768)
    properties = mapping["mappings"]["properties"]

    assert mapping["settings"]["index"]["knn"] is True
    assert properties["embedding"]["dimension"] == 768
    assert properties["embedding"]["space_type"] == "cosinesimil"
    assert properties["destination_key"]["type"] == "keyword"
    assert properties["text"]["type"] == "text"


def test_hybrid_search_fuses_vector_and_bm25_and_pushes_filters(monkeypatch) -> None:
    store = object.__new__(OpenSearchGraphStore)
    calls = []
    responses = [
        [_hit("orsay", "Musée d'Orsay", 0.93), _hit("louvre", "The Louvre", 0.91)],
        [_hit("louvre", "The Louvre", 8.2), _hit("carnavalet", "Carnavalet", 6.1)],
    ]

    def fake_hits(payload):
        calls.append(payload)
        return responses[len(calls) - 1]

    monkeypatch.setattr(store, "_hits", fake_hits)

    results = store.hybrid_search(
        "museums and art galleries",
        [0.1, 0.2],
        destination_key="paris",
        category_keys=["museums"],
        limit=3,
        rank_constant=1,
    )

    assert [result["node"]["id"] for result in results] == [
        "louvre",
        "orsay",
        "carnavalet",
    ]
    assert results[0]["vector_rank"] == 2
    assert results[0]["lexical_rank"] == 1
    vector_filters = calls[0]["query"]["knn"]["embedding"]["filter"]["bool"]["filter"]
    assert {"term": {"destination_key": "paris"}} in vector_filters
    assert {"terms": {"category_key": ["museums"]}} in vector_filters


def test_neptune_rows_are_namespaced_and_payload_is_decoded() -> None:
    captured = []

    class Client:
        def execute_query(self, **kwargs):
            captured.append(kwargs)
            return {"payload": b'{"results":[{"id":"chunk-1"}]}'}

    store = NeptuneAnalyticsGraphStore(
        "graph-1", region="eu-central-1", namespace="pilot", client=Client()
    )
    rows = store._execute("RETURN 1", value=1)
    decorated = store._decorate_rows(
        [
            {
                "chunk_id": "chunk-1",
                "page_key": "v1:42",
                "destination_key": "paris",
                "category_key": "museums",
                "place_key": "paris:louvre",
            }
        ]
    )

    assert rows == [{"id": "chunk-1"}]
    assert captured[0]["parameters"] == {"value": 1}
    assert decorated[0]["chunk_graph_key"] == "pilot:chunk:chunk-1"
    assert decorated[0]["place_graph_key"] == "pilot:place:paris:louvre"


def test_expand_seed_context_hydrates_neptune_ids_from_opensearch() -> None:
    seeds = [
        {
            "score": 0.03,
            "node": {"id": "louvre", "entity_name": "The Louvre", "text": "Louvre"},
        }
    ]
    graph_store = SimpleNamespace(
        expand_ids=lambda ids, per_kind: {
            "louvre": {"adjacent": ["orsay"], "related": ["louvre-alt"]}
        }
    )
    search_store = SimpleNamespace(
        get_documents=lambda ids: {
            "orsay": {"id": "orsay", "text": "Musée d'Orsay"},
            "louvre-alt": {"id": "louvre-alt", "text": "Another Louvre description"},
        }
    )

    results = expand_seed_context(
        seeds,
        search_store=search_store,
        graph_store=graph_store,
    )

    assert results[0]["adjacent"][0]["id"] == "orsay"
    assert results[0]["related"][0]["id"] == "louvre-alt"


def _record(chunk_id: str, ordinal: int) -> dict:
    return {
        "chunk_id": chunk_id,
        "embedding_text": f"Destination: Paris\nAttraction: {chunk_id}",
        "metadata": {
            "page_id": "42",
            "title": "Paris",
            "destination_key": "paris",
            "category_key": "museums",
            "entity_name": chunk_id,
            "chunk_ordinal": ordinal,
        },
    }


def test_dual_index_resume_repairs_each_store_independently(monkeypatch) -> None:
    monkeypatch.setattr(
        dual_index,
        "load_corpora",
        lambda paths: [({"corpus_version": "pilot-v1"}, [_record("first", 1), _record("second", 2)])],
    )

    class Embedder:
        provider = "test"
        model = "tiny"
        dimension = 2

        def embed(self, texts):
            return [[float(len(text)), 1.0] for text in texts]

    class SearchStore:
        documents = []

        def ensure_index(self, dimension):
            assert dimension == 2
            return False

        def existing_chunk_ids(self, ids):
            return {"first"}

        def upsert_documents(self, rows):
            self.documents.extend(dict(row) for row in rows)

        def count(self):
            return 2

    class GraphStore:
        chunks = []
        relationships = []

        def existing_chunk_ids(self, ids):
            return {"second"}

        def upsert_chunks(self, rows):
            self.chunks.extend(dict(row) for row in rows)

        def upsert_next(self, rows):
            self.relationships.extend(rows)

        def counts(self):
            return {"chunks": 2, "places": 2}

    search = SearchStore()
    graph = GraphStore()
    result = dual_index.index_prepared(
        [Path("unused")],
        search_store=search,
        graph_store=graph,
        embedder=Embedder(),
    )

    assert [row["chunk_id"] for row in search.documents] == ["second"]
    assert len(search.documents[0]["embedding"]) == 2
    assert [row["chunk_id"] for row in graph.chunks] == ["first"]
    assert graph.relationships == [{"previous_id": "first", "current_id": "second"}]
    assert result["opensearch_indexed_now"] == 1
    assert result["neptune_indexed_now"] == 1
