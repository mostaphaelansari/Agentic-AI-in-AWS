"""Unit tests for the open-source travel retrieval pipeline.

Follows the conventions in tests/unit/test_wikivoyage_rag.py: no mock library,
tiny local fakes defined in the test that needs them, monkeypatch for env and
module constants, and long descriptive names that document the contract.

Qdrant is exercised for real through its in-memory mode rather than faked. It
runs the same filter and fusion code paths as the server, so the tests verify
behaviour instead of asserting that particular arguments were passed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "infrastructure" / "gateway"))

import travel_rag_lambda as rag_lambda  # noqa: E402

from rag.opensource import bm25 as rag_bm25  # noqa: E402
from rag.opensource import query as rag_query  # noqa: E402
from rag.opensource import rerank as rag_rerank  # noqa: E402
from rag.opensource import store as rag_store  # noqa: E402
from rag.opensource.search import RetrievalConfig, TravelRetriever, unavailable  # noqa: E402

_CLI_SPEC = importlib.util.spec_from_file_location(
    "opensource_rag_cli", ROOT / "scripts" / "opensource_rag.py"
)
assert _CLI_SPEC and _CLI_SPEC.loader
opensource_rag_cli = importlib.util.module_from_spec(_CLI_SPEC)
_CLI_SPEC.loader.exec_module(opensource_rag_cli)

DIMENSION = 8


class StubEmbedder:
    """Deterministic embeddings without an Ollama round trip.

    Vectors are keyed off the words present in the text, so semantically
    similar strings land near each other and retrieval order is predictable.
    """

    provider = "stub"
    model = "stub-embed"

    def __init__(self) -> None:
        self.dimension = DIMENSION
        self.calls: list[str] = []

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * DIMENSION
        for token in rag_bm25.tokenize(text):
            vector[rag_bm25.term_index(token) % DIMENSION] += 1.0
        if not any(vector):
            vector[0] = 1.0
        return vector

    def embed(self, texts) -> list[list[float]]:
        self.calls.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def _record(chunk_id, text, *, destination="paris", category="museums", entity="", section="See"):
    return {
        "chunk_id": chunk_id,
        "embedding_text": text,
        "metadata": {
            "destination_key": destination,
            "category_key": category,
            "entity_name": entity,
            "section": section,
            "title": destination.title(),
            "document_type": "listing" if entity else "section_prose",
            "source_url": f"https://example.invalid/{chunk_id}",
        },
    }


CORPUS = [
    _record("c1", "The Louvre is the largest art museum in Paris", entity="Musee du Louvre"),
    _record("c2", "Orsay holds impressionist painting collections", entity="Musee d'Orsay"),
    _record(
        "c3",
        "Restaurant cambodgien authentique a Paris",
        category="restaurants",
        entity="Le Cambodge",
        section="Eat",
    ),
    _record("c4", "Aquarium and oceanarium exhibits", destination="lisbon", category="attractions"),
    # Same attraction as c1, from a district subpage: must be deduplicated.
    _record("c5", "The Louvre palace holds an immense art collection", entity="Musee du Louvre"),
]


def _build_retriever(config: RetrievalConfig | None = None):
    texts = [record["embedding_text"] for record in CORPUS]
    embedder = StubEmbedder()
    bm25 = rag_bm25.BM25Index.build(texts)
    index = rag_store.TravelIndex.connect(":memory:", "travel-rag-test")
    index.ensure_collection(DIMENSION)
    index.upsert(
        rag_store.build_points(
            CORPUS,
            embedder.embed(texts),
            [bm25.encode_document(text) for text in texts],
            corpus_version="test-v1",
        )
    )
    return TravelRetriever(
        index=index,
        embedder=embedder,
        bm25=bm25,
        config=config or RetrievalConfig(corpus_version="test-v1"),
    )


def test_cli_rejects_a_manifest_for_a_different_collection(tmp_path):
    manifest_path = tmp_path / "index-manifest.json"
    manifest_path.write_text(
        json.dumps({"collection": "travel-rag-eval"}), encoding="utf-8"
    )

    with pytest.raises(
        opensource_rag_cli.IngestError,
        match="belongs to collection 'travel-rag-eval'",
    ):
        opensource_rag_cli._retriever(
            SimpleNamespace(
                index_manifest=manifest_path,
                collection="travel-rag",
            )
        )


# --------------------------------------------------------------------------
# BM25
# --------------------------------------------------------------------------


def test_bm25_term_index_is_stable_across_processes():
    # Python's built-in hash is salted per process, which would make an index
    # written by one run unqueryable by the next.
    assert rag_bm25.term_index("louvre") == rag_bm25.term_index("louvre")
    assert rag_bm25.term_index("louvre") != rag_bm25.term_index("orsay")


def test_bm25_folds_accents_so_unaccented_queries_match_french_listings():
    assert rag_bm25.tokenize("Musee") == rag_bm25.tokenize("Musée")
    index = rag_bm25.BM25Index.build(["musée du louvre", "restaurant cambodgien"])
    assert index.encode_query("musee") == index.encode_query("musée")


def test_bm25_manifest_roundtrip_reproduces_identical_encodings():
    index = rag_bm25.BM25Index.build(["the louvre museum", "a paris restaurant"])
    restored = rag_bm25.BM25Index.from_manifest(json.loads(json.dumps(index.to_manifest())))
    assert restored.encode_document("louvre museum") == index.encode_document("louvre museum")
    assert restored.encode_query("louvre museum") == index.encode_query("louvre museum")


def test_bm25_rejects_a_malformed_manifest_section():
    with pytest.raises(rag_bm25.BM25Error, match="Malformed BM25 manifest"):
        rag_bm25.BM25Index.from_manifest({"document_count": 3})


def test_bm25_query_encoding_is_not_length_normalized():
    # Document encoding applies length normalization; query encoding must not,
    # or longer enriched queries would be penalized for their length.
    index = rag_bm25.BM25Index.build(["alpha beta", "beta gamma"])
    _, values = index.encode_query("beta beta")
    assert values == [2.0]


def test_bm25_empty_text_encodes_to_an_empty_sparse_vector():
    index = rag_bm25.BM25Index.build(["alpha beta"])
    assert index.encode_document("") == ([], [])
    assert index.encode_query("   ") == ([], [])


# --------------------------------------------------------------------------
# Query planning
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("museums and art galleries", "museums"),
        ("Where should I eat?", "restaurants"),
        ("where can we stay tonight", "accommodation"),
        ("top attracions in town", "attractions"),
        ("best bars and nightlife", "drinks"),
        ("what is the weather", None),
    ],
)
def test_intent_routing_matches_the_deployed_lambda(query, expected):
    route = rag_query.intent_route(query)
    assert (route[0] if route else None) == expected


def test_attraction_intent_expands_to_the_narrower_category_keys():
    # Sightseeing listings are filed under museums/landmarks at ingest, so a
    # query routed to "attractions" that filtered on that key alone would miss
    # most of the corpus.
    assert rag_query.category_keys_for_intent("attractions") == (
        "attractions",
        "museums",
        "landmarks",
    )
    assert rag_query.category_keys_for_intent("restaurants") == ("restaurants",)


def test_focus_query_leaves_a_query_that_already_names_the_destination():
    assert rag_query.focus_query("museums in Paris", "Paris", "France") == "museums in Paris"


def test_focus_query_preserves_trailing_punctuation():
    assert (
        rag_query.focus_query("Where to eat?", "Paris", "France")
        == "Where to eat in Paris, France?"
    )


def test_candidate_count_overfetches_so_deduplication_can_still_fill_top_k():
    assert rag_query.candidate_count(5, rerank_enabled=False) == 10
    assert rag_query.candidate_count(80, rerank_enabled=False) == rag_query.MAX_RERANK_CANDIDATES


def test_candidate_count_uses_the_rerank_budget_when_a_reranker_is_configured():
    assert rag_query.candidate_count(5, rerank_enabled=True, rerank_candidates=20) == 20
    assert rag_query.candidate_count(30, rerank_enabled=True, rerank_candidates=20) == 30
    with pytest.raises(rag_query.QueryError, match="rerank_candidates"):
        rag_query.candidate_count(5, rerank_enabled=True, rerank_candidates=0)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": "", "destination": "Paris"}, "query"),
        ({"query": "x" * 501, "destination": "Paris"}, "query"),
        ({"query": "ok", "destination": ""}, "destination"),
        ({"query": "ok", "destination": "x" * 201}, "destination"),
        ({"query": "ok", "destination": "Paris", "top_k": 0}, "top_k"),
        ({"query": "ok", "destination": "Paris", "top_k": 11}, "top_k"),
        ({"query": "ok", "destination": "Paris", "top_k": True}, "top_k"),
    ],
)
def test_input_bounds_are_rejected(kwargs, message):
    with pytest.raises(rag_query.QueryError, match=message):
        rag_query.validate_inputs(**kwargs)


def test_retrieval_filter_uses_destination_alone_when_nothing_is_routed():
    condition = rag_query.retrieval_filter("paris").must
    assert len(condition) == 1
    assert condition[0].key == "destination_key"


def test_retrieval_filter_prefers_category_over_section():
    query_filter = rag_query.retrieval_filter("paris", ("See > Museums",), ("museums",))
    keys = [condition.key for condition in query_filter.must]
    assert keys == ["destination_key", "category_key"]


def test_plan_reports_the_enrichment_it_applied():
    plan = rag_query.plan("museums", "Paris", "France")
    assert plan["intent"] == "museums"
    assert plan["query_enriched"] is True
    assert "Category: Museum or art gallery" in plan["retrieval_query"]
    assert plan["category_filters"] == ("museums",)


def test_plan_can_disable_category_filtering_for_ablation_runs():
    plan = rag_query.plan("museums", "Paris", category_filter_enabled=False)
    assert plan["category_filters"] == ()
    assert plan["section_filters"]


# --------------------------------------------------------------------------
# Parity with the deployed AWS Lambda
# --------------------------------------------------------------------------


def test_intent_route_table_matches_the_lambda_exactly():
    """The route table is duplicated on purpose; this stops it drifting.

    rag/opensource/query.py and infrastructure/gateway/travel_rag_lambda.py are
    separate deployables, so they each carry the table. Retrieval quality
    depends on the two staying byte-identical.
    """
    ours = [
        (name, pattern.pattern, rewrite, sections)
        for name, pattern, rewrite, sections in rag_query.INTENT_ROUTES
    ]
    theirs = [
        (name, pattern.pattern, rewrite, sections)
        for name, pattern, rewrite, sections in rag_lambda._INTENT_ROUTES
    ]
    assert ours == theirs


def test_shared_bounds_match_the_lambda():
    assert rag_query.MAX_QUERY_CHARS == rag_lambda.MAX_QUERY_CHARS
    assert rag_query.MAX_LOCATION_CHARS == rag_lambda.MAX_LOCATION_CHARS
    assert rag_query.MAX_FOCUSED_QUERY_CHARS == rag_lambda.MAX_FOCUSED_QUERY_CHARS
    assert rag_query.MAX_TOP_K == rag_lambda.MAX_TOP_K
    assert rag_query.MAX_RERANK_CANDIDATES == rag_lambda.MAX_RERANK_CANDIDATES
    assert rag_query.FRESHNESS_WARNING == rag_lambda.FRESHNESS_WARNING


def test_query_enrichment_matches_the_lambda_wording():
    # The corpus was embedded against this exact header format.
    assert rag_query.enrich_query(
        "museums", "Paris", "France", "Museum"
    ) == rag_lambda._enrich_query("museums", "Paris", "France", "Museum")


def test_category_rewrites_match_the_datatourisme_labels():
    from rag.datatourisme import classify_poi

    rewrites = {name: rewrite for name, _pattern, rewrite, _sections in rag_query.INTENT_ROUTES}
    assert classify_poi(("Museum",))[1] == rewrites["museums"]


# --------------------------------------------------------------------------
# Store and end-to-end retrieval
# --------------------------------------------------------------------------


def test_point_ids_are_deterministic_so_reruns_overwrite_instead_of_duplicating():
    assert rag_store.point_id("abc") == rag_store.point_id("abc")
    assert rag_store.point_id("abc") != rag_store.point_id("abd")


def test_ensure_collection_refuses_a_silent_embedding_width_change():
    index = rag_store.TravelIndex.connect(":memory:", "dimension-guard")
    index.ensure_collection(DIMENSION)
    with pytest.raises(rag_store.StoreError, match="Re-run with --recreate"):
        index.ensure_collection(DIMENSION + 1)
    index.ensure_collection(DIMENSION + 1, recreate=True)


def test_existing_chunk_ids_lets_an_interrupted_index_resume():
    retriever = _build_retriever()
    assert retriever.index.existing_chunk_ids() == {"c1", "c2", "c3", "c4", "c5"}


def test_retrieval_filters_out_other_destinations():
    response = _build_retriever().search("aquarium and oceanarium", "Paris", top_k=5)
    assert all(result["destination_key"] == "paris" for result in response["results"])


def test_repeat_listings_for_one_attraction_are_collapsed():
    response = _build_retriever().search("museums", "Paris", top_k=5)
    entities = [result["entity_name"] for result in response["results"]]
    assert entities.count("Musee du Louvre") == 1


def test_scores_are_reported_on_their_own_scales():
    response = _build_retriever().search("museums", "Paris", top_k=3)
    result = response["results"][0]
    # Fused RRF values are reciprocal ranks, not similarities; the cosine score
    # is what the 0.60 target and the AWS comparison are expressed in.
    assert 0.0 <= result["score"] <= 1.0000001
    assert result["fusion_score"] > 0
    assert result["meets_score_target"] == (result["score"] >= rag_query.SCORE_TARGET)


def test_response_carries_the_documented_contract():
    response = _build_retriever().search("museums", "Paris", "France", top_k=2)
    assert set(response) == {"corpus", "results", "warnings", "score_target", "retrieval"}
    assert response["warnings"] == [rag_query.FRESHNESS_WARNING]
    assert response["retrieval"]["search_type"] == "HYBRID"
    assert response["retrieval"]["ranking"] == "HYBRID_RRF"


def test_route_fallback_retries_without_the_category_filter():
    # "shopping" routes to a category with no listings for Lisbon in this
    # corpus, so the strict pass returns nothing and the fallback runs.
    response = _build_retriever().search("shopping and markets", "Lisbon", top_k=3)
    assert response["retrieval"]["route_fallback"] is True
    assert response["results"]


def test_an_all_stopword_query_still_searches_with_the_dense_arm_alone():
    # Qdrant rejects an empty sparse vector, so a query that encodes to nothing
    # must not be sent as a sparse prefetch.
    response = _build_retriever().search("...", "Paris", top_k=3)
    assert response["retrieval"]["search_type"] == "HYBRID"


# --------------------------------------------------------------------------
# Reranking
# --------------------------------------------------------------------------


def _results(count: int) -> list[dict]:
    return [
        {"payload": {"text": f"passage {i}", "entity_name": f"e{i}"}, "score": 0.5}
        for i in range(count)
    ]


def test_rerank_is_skipped_without_a_configured_url():
    results = _results(3)
    assert rag_rerank.rerank("q", results, 2, url="") == results[:2]


def test_rerank_reorders_by_cross_encoder_score(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps([{"index": 2, "score": 0.9}, {"index": 0, "score": 0.4}]).encode()

        return Response()

    monkeypatch.setattr(rag_rerank.urllib.request, "urlopen", fake_urlopen)
    ranked = rag_rerank.rerank("original question", _results(3), 2, url="http://reranker")
    assert [result["payload"]["entity_name"] for result in ranked] == ["e2", "e0"]
    assert ranked[0]["rerank_score"] == 0.9
    # The cross-encoder must score against the user's question, not the
    # synthetic enriched retrieval query.
    assert captured["body"]["query"] == "original question"


def test_rerank_rejects_an_out_of_range_index(monkeypatch):
    def fake_urlopen(request, timeout=None):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps([{"index": 99, "score": 0.9}]).encode()

        return Response()

    monkeypatch.setattr(rag_rerank.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(rag_rerank.RerankError, match="out-of-range"):
        rag_rerank.rerank("q", _results(2), 2, url="http://reranker")


def test_an_unreachable_reranker_degrades_to_fused_order(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise rag_rerank.urllib.error.URLError("connection refused")

    monkeypatch.setattr(rag_rerank.urllib.request, "urlopen", fake_urlopen)
    retriever = _build_retriever(
        RetrievalConfig(corpus_version="test-v1", reranker_url="http://reranker")
    )
    response = retriever.search("museums", "Paris", top_k=3)
    assert response["retrieval"]["ranking"] == "HYBRID_FALLBACK"
    assert response["results"]


# --------------------------------------------------------------------------
# Unavailable path
# --------------------------------------------------------------------------


def test_unavailable_returns_empty_results_rather_than_raising():
    response = unavailable("qdrant is down")
    assert response["error"] == "retrieval_unavailable"
    assert response["results"] == []
    assert "qdrant is down" in response["message"]


def test_tool_docstring_documents_every_argument():
    # FastMCP derives the tool schema description from this docstring, so a
    # missing argument becomes a missing hint for the model.
    sys.path.insert(0, str(ROOT / "opensource" / "mcp-tools"))
    import server  # noqa: PLC0415

    docstring = server.searchTravelGuides.__doc__ or ""
    for argument in ("query:", "destination:", "country:", "top_k:"):
        assert argument in docstring
    assert re.search(r"snapshot", docstring)


# --------------------------------------------------------------------------
# Multi-corpus indexing
# --------------------------------------------------------------------------


def _write_prepared(tmp_path: Path, name: str) -> Path:
    """Build a real prepared corpus with the shared Wikivoyage code."""
    from rag.wikivoyage import Article, write_prepared_corpus  # noqa: PLC0415

    wikitext = (
        "'''Paris''' is the capital of France.\n\n"
        "==See==\n"
        "{{see|name=" + name + " Museum|address=Rue de Rivoli|content=A large art "
        "museum with paintings and sculpture collections worth a long visit.}}\n\n"
        "Paris neighbourhoods each have their own character and are worth exploring.\n"
    )
    output = tmp_path / name
    write_prepared_corpus(
        [Article(page_id=1, revision_id=10, title="Paris", wikitext=wikitext)],
        output,
        corpus_version=name,
        snapshot_date="2026-08-25",
        source_url="https://en.wikivoyage.org/",
        source_checksum="0" * 64,
    )
    return output


def test_indexer_reads_each_corpus_prefix_from_its_own_manifest(tmp_path):
    # Wikivoyage and DATAtourisme declare different prefixes, so a multi-corpus
    # run cannot take one from the command line.
    from rag.opensource.index import read_prefix  # noqa: PLC0415

    assert read_prefix(_write_prepared(tmp_path, "alpha-v1")) == "wikivoyage"


def test_bm25_table_covers_every_corpus_indexed_into_one_collection(tmp_path):
    """One shared table, built over the union, is the whole point.

    BM25 weights depend on corpus-wide document frequencies. Indexing a second
    corpus in a separate run would overwrite the manifest with a table computed
    over only that corpus, leaving the first corpus's stored sparse weights
    keyed to a table nothing uses any more — a silent ranking regression.
    """
    from rag.opensource.index import build_bm25, load_corpora  # noqa: PLC0415

    paths = [_write_prepared(tmp_path, "alpha-v1"), _write_prepared(tmp_path, "beta-v1")]
    corpora = load_corpora(paths)
    assert len(corpora) == 2

    every_record = [record for _manifest, records in corpora for record in records]
    shared = build_bm25(every_record)
    alpha_only = build_bm25(corpora[0][1])

    assert shared.document_count == len(every_record)
    assert shared.document_count > alpha_only.document_count
    # The shared table must know terms that appear only in the second corpus.
    beta_terms = set(shared.document_frequencies) - set(alpha_only.document_frequencies)
    assert beta_terms


def test_dry_run_validates_every_corpus_without_embedding(tmp_path):
    from rag.opensource.index import index_prepared  # noqa: PLC0415

    class ExplodingEmbedder:
        provider = "stub"
        model = "should-not-be-called"
        dimension = None

        def embed(self, texts):
            raise AssertionError("dry run must not embed")

    result = index_prepared(
        [_write_prepared(tmp_path, "alpha-v1"), _write_prepared(tmp_path, "beta-v1")],
        collection="unused",
        qdrant_url=":memory:",
        embedder=ExplodingEmbedder(),
        manifest_path=tmp_path / "index-manifest.json",
        dry_run=True,
    )
    assert result["corpus_versions"] == ["alpha-v1", "beta-v1"]
    assert result["indexed_count"] == 0
    assert len(result["sources"]) == 2


def test_index_manifest_records_the_measured_width_not_the_bedrock_declaration(tmp_path):
    """The prepared manifest declares Titan/512; this index is neither."""
    from rag.opensource.index import index_prepared  # noqa: PLC0415

    prepared = _write_prepared(tmp_path, "alpha-v1")
    source_manifest = json.loads((prepared / "manifest.json").read_text(encoding="utf-8"))
    assert source_manifest["embedding_dimensions"] == 512

    embedder = StubEmbedder()
    result = index_prepared(
        [prepared],
        collection="measured-width",
        qdrant_url=":memory:",
        embedder=embedder,
        manifest_path=tmp_path / "index-manifest.json",
    )
    assert result["embedding_dimensions"] == DIMENSION
    assert result["embedding_provider"] == "stub"
    assert result["sources"][0]["corpus_version"] == "alpha-v1"
    assert (tmp_path / "index-manifest.json").exists()


# --------------------------------------------------------------------------
# Searching every destination (local exploration only)
# --------------------------------------------------------------------------


def test_the_tool_contract_still_requires_a_destination():
    # The MCP tool must keep matching the AWS Lambda's contract: semantic
    # similarity alone does not separate "museums in Paris" from "museums in
    # Barcelona", so the filter is not optional for real answers.
    with pytest.raises(rag_query.QueryError, match="destination"):
        rag_query.validate_inputs("museums", "")


def test_local_exploration_can_opt_out_of_the_destination_requirement():
    query, destination, _country, _top_k = rag_query.validate_inputs(
        "museums", "", require_destination=False
    )
    assert (query, destination) == ("museums", "")


def test_an_empty_destination_produces_no_filter_rather_than_an_empty_one():
    # Qdrant treats a Filter with an empty clause list as matching nothing,
    # so "no constraints" has to be expressed as no filter at all.
    assert rag_query.retrieval_filter("") is None
    scoped = rag_query.retrieval_filter("", (), ("museums",))
    assert [condition.key for condition in scoped.must] == ["category_key"]


def test_enrichment_omits_the_destination_line_when_there_is_none():
    enriched = rag_query.enrich_query("museums", "", "", "Museum or art gallery")
    assert "Destination:" not in enriched
    assert "Category: Museum or art gallery" in enriched


def test_searching_every_destination_returns_more_than_one_city():
    # "what to see" routes to attractions, which expands to the
    # {attractions, museums, landmarks} union, so the category filter is not
    # what limits the result here -- the destination filter is.
    retriever = _build_retriever()
    scoped = retriever.search("what to see", "Paris", top_k=5)
    every = retriever.search("what to see", "", top_k=5, require_destination=False)

    assert {r["destination_key"] for r in scoped["results"]} == {"paris"}
    assert "lisbon" in {r["destination_key"] for r in every["results"]}
    assert len(every["results"]) > len(scoped["results"])


def test_route_fallback_marks_results_that_do_not_match_the_intent():
    """A dropped category filter must be visible per result.

    The corpus has no Lisbon accommodation, so an accommodation query for
    Lisbon falls back to destination-only and returns whatever Lisbon has.
    Those passages are not answers, and the caller has to be able to tell.
    """
    retriever = _build_retriever()
    response = retriever.search("where can we stay", "Lisbon", top_k=3)

    assert response["retrieval"]["route_fallback"] is True
    assert response["results"]
    assert all(result["matches_intent"] is False for result in response["results"])


def test_results_matching_the_routed_category_are_not_flagged():
    response = _build_retriever().search("museums", "Paris", top_k=3)
    assert response["retrieval"]["route_fallback"] is False
    assert all(result["matches_intent"] is True for result in response["results"])


def test_an_unrouted_query_never_flags_results_as_off_intent():
    # With no intent there is nothing to be off-category from.
    response = _build_retriever().search("tell me about this city", "Paris", top_k=3)
    assert response["retrieval"]["intent"] is None
    assert all(result["matches_intent"] is True for result in response["results"])


# --------------------------------------------------------------------------
# Single-arm ablation
# --------------------------------------------------------------------------


def _mode(name: str) -> RetrievalConfig:
    return RetrievalConfig(corpus_version="test-v1", retrieval_mode=name)


def test_sparse_only_search_never_calls_the_embedding_backend():
    """BM25 must be exercisable with no model running.

    Fusing a single ranker would also replace its real scores with reciprocal
    ranks, hiding the very thing an ablation inspects.
    """
    retriever = _build_retriever(_mode("bm25"))
    retriever.embedder.calls.clear()  # drop the corpus embeddings from indexing
    response = retriever.search("Cambodian restaurant", "Paris", top_k=3)

    assert retriever.embedder.calls == []  # nothing embedded at query time
    assert response["retrieval"]["search_type"] == "BM25"
    assert response["retrieval"]["ranking"] == "BM25"
    assert response["results"][0]["entity_name"] == "Le Cambodge"


def test_bm25_scores_are_not_judged_against_the_cosine_target():
    # SCORE_TARGET is a cosine threshold; a raw BM25 score is unbounded.
    result = _build_retriever(_mode("bm25")).search("Cambodian", "Paris", top_k=2)["results"][0]
    assert result["score_type"] == "bm25"
    assert result["meets_score_target"] is None


def test_dense_only_search_reports_a_cosine_and_skips_the_lexical_arm():
    response = _build_retriever(_mode("semantic")).search("art museum", "Paris", top_k=3)
    assert response["retrieval"]["search_type"] == "SEMANTIC"
    assert response["retrieval"]["ranking"] == "SEMANTIC"
    for result in response["results"]:
        assert result["score_type"] == "cosine"
        assert result["fusion_score"] == 0.0


def test_hybrid_reports_a_cosine_score_alongside_the_fusion_rank():
    # Regression: the store labels a fused hit "rrf", but `score` is
    # overwritten with a recovered cosine, so the label must follow the value.
    result = _build_retriever().search("art museum", "Paris", top_k=3)["results"][0]
    assert result["score_type"] == "cosine"
    assert 0.0 <= result["score"] <= 1.0000001
    assert result["fusion_score"] > 0.0
    assert result["meets_score_target"] in (True, False)


def test_invalid_retrieval_mode_is_rejected_at_configuration_time():
    with pytest.raises(ValueError, match="retrieval_mode"):
        RetrievalConfig(retrieval_mode="magic")
