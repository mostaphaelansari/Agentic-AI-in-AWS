from __future__ import annotations

import bz2
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

import rag.ingest as rag_ingest  # noqa: E402
from rag.ingest import (  # noqa: E402
    IngestError,
    add_filter_metadata,
    bedrock_metadata,
    ingest_prepared,
    load_prepared,
    source_prefix,
    validate_backfill_options,
    validate_ingest_options,
    wait_for_ingestion_job,
)
from rag.wikivoyage import (  # noqa: E402
    Article,
    article_chunks,
    category_filter_value,
    chunk_words,
    clean_wikitext,
    destination_filter_value,
    iter_articles,
    normalize_filter_value,
    select_articles,
    validate_chunking_parameters,
    write_prepared_corpus,
)
from scripts.wikivoyage_rag import _title_selection  # noqa: E402


def _context(tool_name: str = "travel-rag___search_travel_guides") -> SimpleNamespace:
    return SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": tool_name})
    )


def _prepared_corpus(path: Path) -> Path:
    article = Article(1, 2, "Example", "== See ==\n" + "Useful travel details. " * 100)
    write_prepared_corpus(
        [article],
        path,
        corpus_version="pilot-v1",
        snapshot_date="2026-08-20",
        source_url="https://example.test/dump",
        source_checksum="abc",
    )
    return path


def test_clean_wikitext_preserves_listing_content_and_link_labels() -> None:
    source = """
== Eat ==
{{eat|name=Café Example|address=1 Main St|content=Regional food.|price=€€}}
Try [[French cuisine|local cuisine]] and [https://example.com the official site].
[[Category:Example]]
"""
    cleaned = clean_wikitext(source)
    assert "Café Example" in cleaned
    assert "Regional food" in cleaned
    assert "local cuisine" in cleaned
    assert "the official site" in cleaned
    assert "Category:" not in cleaned
    assert "{{" not in cleaned


def test_article_chunks_are_deterministic_and_section_aware() -> None:
    text = "== See ==\n" + "Museum information and local context. " * 100
    article = Article(page_id=1, revision_id=2, title="Example City", wikitext=text)
    first = article_chunks(article, snapshot_date="2026-08-20")
    second = article_chunks(article, snapshot_date="2026-08-20")
    assert first
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]
    assert all(chunk.section_path == "See" for chunk in first)
    assert all(chunk.embedding_text.startswith("Destination: Example City") for chunk in first)
    assert all(chunk.document_type == "section_prose" for chunk in first)


def test_article_chunks_preserve_atomic_listings_without_synthetic_summaries() -> None:
    article = Article(
        page_id=1,
        revision_id=2,
        title="Paris/7th arrondissement",
        wikitext="""
== See ==
=== Museums and Galleries ===
{{see|name=Musée d'Orsay|address=1 rue de la Légion d'Honneur|content=Impressionist art by Monet and Renoir.|hours=Tu-Su}}
{{see|name=Musée Rodin|address=79 rue de Varenne|content=Sculptures by Auguste Rodin.|price=€10}}
""",
    )
    chunks = article_chunks(article, snapshot_date="2026-08-24")
    listings = [chunk for chunk in chunks if chunk.document_type == "listing"]
    assert {chunk.document_type for chunk in chunks} == {"listing"}
    assert {chunk.entity_name for chunk in listings} == {"Musée d'Orsay", "Musée Rodin"}
    orsay = next(chunk for chunk in listings if chunk.entity_name == "Musée d'Orsay")
    assert "Attraction: Musée d'Orsay" in orsay.text
    assert "Impressionist art" in orsay.text
    assert "Musée Rodin" not in orsay.text
    assert orsay.category_key == "museums"
    assert "Category:" not in orsay.text
    assert "Document type:" not in orsay.text
    assert "Area:" not in orsay.text


def test_chunk_words_caps_lexical_words_even_without_spaces() -> None:
    text = ",".join(f"coordinate{index}" for index in range(25))
    chunks = list(chunk_words(text, target_words=10, overlap_words=2))
    assert len(chunks) == 3
    assert all(len(re.findall(r"\b[\w'-]+\b", chunk)) <= 10 for chunk in chunks)


@pytest.mark.parametrize(
    ("section", "category"),
    [
        ("See > Museums and Galleries", "museums"),
        # Wikivoyage capitalizes and pluralizes these headings inconsistently;
        # the Louvre sits under the lowercase spelling.
        ("See > Museums and galleries", "museums"),
        ("See > Museum", "museums"),
        # "Buy > The galleries" is a row of shopping arcades, not art galleries.
        ("Buy > The galleries", "shopping"),
        ("See > Landmarks", "landmarks"),
        ("Do > Festivals", "activities"),
        ("Eat > Budget", "restaurants"),
        ("Sleep > Mid-range", "accommodation"),
    ],
)
def test_category_filter_value_normalizes_section_variants(section: str, category: str) -> None:
    assert category_filter_value(section) == category


def test_iter_articles_ignores_redirects_and_non_main_namespaces(tmp_path: Path) -> None:
    xml = """<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">
      <page><title>Kept</title><ns>0</ns><id>1</id><revision><id>10</id><text>Useful article text.</text></revision></page>
      <page><title>Redirect</title><ns>0</ns><id>2</id><redirect title="Kept"/><revision><id>20</id><text>#REDIRECT [[Kept]]</text></revision></page>
      <page><title>Talk:Ignored</title><ns>1</ns><id>3</id><revision><id>30</id><text>Ignored.</text></revision></page>
    </mediawiki>"""
    dump = tmp_path / "sample.xml.bz2"
    dump.write_bytes(bz2.compress(xml.encode()))
    articles = list(iter_articles(dump))
    assert [(article.page_id, article.title) for article in articles] == [(1, "Kept")]


def test_select_articles_reservoir_is_reproducible() -> None:
    articles = [Article(index, index, f"City {index}", "word " * 100) for index in range(1, 21)]
    first, _ = select_articles(articles, limit=5, seed=7)
    second, _ = select_articles(articles, limit=5, seed=7)
    assert [article.page_id for article in first] == [article.page_id for article in second]
    assert len(first) == 5


def test_select_articles_supports_descendant_title_prefixes() -> None:
    articles = [
        Article(1, 1, "Paris", "word " * 100),
        Article(2, 2, "Paris/7th arrondissement", "word " * 100),
        Article(3, 3, "Paris/La Défense", "word " * 100),
        Article(4, 4, "Paris, Texas", "word " * 100),
    ]
    selected, missing = select_articles(
        articles,
        limit=None,
        titles={"Paris"},
        title_prefixes={"Paris"},
    )
    assert [article.title for article in selected] == [
        "Paris",
        "Paris/7th arrondissement",
        "Paris/La Défense",
    ]
    assert missing == set()


def test_select_articles_reports_unmatched_title_prefix() -> None:
    selected, missing = select_articles(
        [Article(1, 1, "Paris", "word " * 100)],
        limit=None,
        title_prefixes={"Rome"},
    )
    assert selected == []
    assert missing == {"Rome/*"}


def test_titles_file_accepts_exact_titles_and_descendant_patterns(tmp_path: Path) -> None:
    titles_file = tmp_path / "destinations.txt"
    titles_file.write_text("# Target corpus\nParis\nParis/*\nRome/*\n", encoding="utf-8")
    titles, prefixes = _title_selection(str(titles_file))
    assert titles == {"Paris"}
    assert prefixes == {"Paris", "Rome"}


def test_shipped_titles_file_covers_the_evaluation_destinations() -> None:
    titles, prefixes = _title_selection(str(ROOT / "rag" / "titles" / "wikivoyage-pilot-v3.txt"))
    assert titles is not None and prefixes is not None
    # The evaluation scenarios in evaluations/agentcore_dataset.json ask about
    # these four cities, so the corpus must select each one plus its districts.
    assert {"Paris", "Lisbon", "Barcelona", "Casablanca"} <= titles
    assert {"Paris", "Lisbon", "Barcelona"} <= prefixes
    # Casablanca has no subpages upstream, and prepare() fails on a selector
    # that matches nothing, so it must stay exact-only.
    assert "Casablanca" not in prefixes
    # Every `Name/*` needs its parent listed too; the pattern excludes the parent.
    assert prefixes <= titles


def test_write_prepared_corpus_separates_chunk_text_from_vector_metadata(
    tmp_path: Path,
) -> None:
    article = Article(1, 2, "Example", "== See ==\n" + "Useful travel details. " * 100)
    manifest = write_prepared_corpus(
        [article],
        tmp_path,
        corpus_version="pilot-v1",
        snapshot_date="2026-08-20",
        source_url="https://example.test/dump",
        source_checksum="abc",
    )
    vector_record = json.loads((tmp_path / "chunks.jsonl").read_text().splitlines()[0])
    page_record = json.loads(next((tmp_path / "pages").glob("*.json")).read_text())
    assert manifest["article_count"] == 1
    assert manifest["selected_article_count"] == 1
    assert manifest["chunk_count"] >= 1
    assert manifest["s3_prefix"] == "wikivoyage"
    assert "embedding_text" in vector_record
    assert "text" not in vector_record["metadata"]
    assert page_record["chunks"][0]["text"]


def test_lambda_returns_bedrock_knowledge_base_evidence(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_KNOWLEDGE_BASE_ID", "KB12345678")
    monkeypatch.setenv("RAG_CORPUS_VERSION", "pilot-v1")
    monkeypatch.setenv("RAG_SCORE_THRESHOLD", "0.5")
    rerank_queries: list[str] = []
    retrieval_calls: list[tuple[str, int, str, tuple[str, ...], tuple[str, ...]]] = []

    def retrieve(query, top_k, destination_key, sections=(), category_keys=()):
        retrieval_calls.append((query, top_k, destination_key, sections, category_keys))
        return {
            "retrievalResults": [
                {
                    "content": {"text": "Evidence"},
                    "score": 0.1,
                    "location": {"s3Location": {"uri": "s3://bucket/chunk.txt"}},
                    "metadata": {
                        "chunk_id": "chunk-1",
                        "destination_key": "paris",
                        "category_key": "museums",
                        "document_type": "listing",
                        "entity_name": "Example museum",
                        "title": "Paris",
                        "section": "See",
                        "source_url": "https://en.wikivoyage.org/wiki/Paris",
                        "history_url": "https://example.test/history",
                        "revision_id": 1,
                        "snapshot_date": "2026-08-20",
                        "license": "CC BY-SA 4.0",
                    },
                }
            ],
        }

    monkeypatch.setattr(
        rag_lambda,
        "_retrieve",
        retrieve,
    )
    monkeypatch.setattr(
        rag_lambda,
        "_rerank",
        lambda query, results, top_k: (rerank_queries.append(query) or results[:top_k], True),
    )
    response = rag_lambda.lambda_handler(
        {"query": "museums", "destination": "Paris", "top_k": 5}, _context()
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["corpus"] == "pilot-v1"
    assert body["results"][0]["text"] == "Evidence"
    assert body["results"][0]["source_url"] == "https://en.wikivoyage.org/wiki/Paris"
    assert body["results"][0]["destination_key"] == "paris"
    assert body["results"][0]["category_key"] == "museums"
    assert body["results"][0]["document_type"] == "listing"
    assert body["results"][0]["entity_name"] == "Example museum"
    assert body["results"][0]["score"] == 0.1
    assert "meets_score_target" not in body["results"][0]
    assert body["retrieval"]["search_type"] == "SEMANTIC"
    assert "score_target" not in body["retrieval"]
    assert body["retrieval"]["intent"] == "museums"
    assert body["retrieval"]["query_rewritten"] is True
    assert body["retrieval"]["query_enriched"] is True
    assert "See > Museums and Galleries" in body["retrieval"]["section_filters"]
    assert retrieval_calls[0][0] == (
        "museums in Paris\n\n"
        "Destination: Paris\n"
        "Category: Museum or art gallery; cultural attraction"
    )
    assert body["retrieval"]["retrieval_query"] == retrieval_calls[0][0]
    assert retrieval_calls[0][2] == "paris"
    assert rerank_queries == ["museums"]


def test_lambda_filters_semantic_bedrock_retrieval_by_destination(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_KNOWLEDGE_BASE_ID", "KB12345678")
    calls: list[dict] = []

    class Client:
        def retrieve(self, **kwargs):
            calls.append(kwargs)
            return {"retrievalResults": []}

    monkeypatch.setattr(rag_lambda, "_client", lambda: Client())
    rag_lambda._retrieve("museums", 7, "paris")
    assert calls[0]["knowledgeBaseId"] == "KB12345678"
    vector = calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert vector == {
        "numberOfResults": 7,
        "overrideSearchType": "SEMANTIC",
        "filter": {"equals": {"key": "destination_key", "value": "paris"}},
    }


def test_lambda_uses_configured_hybrid_search(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_KNOWLEDGE_BASE_ID", "KB12345678")
    monkeypatch.setenv("RAG_SEARCH_TYPE", "HYBRID")
    calls: list[dict] = []

    class Client:
        def retrieve(self, **kwargs):
            calls.append(kwargs)
            return {"retrievalResults": []}

    monkeypatch.setattr(rag_lambda, "_client", lambda: Client())
    rag_lambda._retrieve("Cambodian restaurant", 5, "paris")
    vector = calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert vector["overrideSearchType"] == "HYBRID"


def test_lambda_rejects_invalid_search_type(monkeypatch) -> None:
    monkeypatch.setenv("RAG_SEARCH_TYPE", "BM25_ONLY")
    with pytest.raises(RuntimeError, match="RAG_SEARCH_TYPE"):
        rag_lambda._search_type()


def test_lambda_combines_destination_and_exact_section_filters(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_KNOWLEDGE_BASE_ID", "KB12345678")
    calls: list[dict] = []

    class Client:
        def retrieve(self, **kwargs):
            calls.append(kwargs)
            return {"retrievalResults": []}

    monkeypatch.setattr(rag_lambda, "_client", lambda: Client())
    sections = ("See > Museums and Galleries", "See > Museums")
    rag_lambda._retrieve("Paris museums and art galleries", 5, "paris", sections)
    vector_filter = calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]
    assert vector_filter == {
        "andAll": [
            {"equals": {"key": "destination_key", "value": "paris"}},
            {
                "orAll": [
                    {
                        "equals": {
                            "key": "section",
                            "value": "See > Museums and Galleries",
                        }
                    },
                    {"equals": {"key": "section", "value": "See > Museums"}},
                ]
            },
        ]
    }


def test_lambda_uses_v2_category_filter(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_KNOWLEDGE_BASE_ID", "KB12345678")
    calls: list[dict] = []

    class Client:
        def retrieve(self, **kwargs):
            calls.append(kwargs)
            return {"retrievalResults": []}

    monkeypatch.setattr(rag_lambda, "_client", lambda: Client())
    rag_lambda._retrieve(
        "Paris museums and art galleries",
        5,
        "paris",
        ("See > Museums",),
        ("museums",),
    )
    vector_filter = calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]
    assert vector_filter == {
        "andAll": [
            {"equals": {"key": "destination_key", "value": "paris"}},
            {"equals": {"key": "category_key", "value": "museums"}},
        ]
    }


def test_lambda_uses_category_union_for_general_attractions(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_KNOWLEDGE_BASE_ID", "KB12345678")
    calls: list[dict] = []

    class Client:
        def retrieve(self, **kwargs):
            calls.append(kwargs)
            return {"retrievalResults": []}

    monkeypatch.setattr(rag_lambda, "_client", lambda: Client())
    rag_lambda._retrieve(
        "Paris tourist attractions museums landmarks and places to visit",
        5,
        "paris",
        (),
        ("attractions", "museums", "landmarks"),
    )
    vector_filter = calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]
    assert vector_filter == {
        "andAll": [
            {"equals": {"key": "destination_key", "value": "paris"}},
            {
                "orAll": [
                    {"equals": {"key": "category_key", "value": "attractions"}},
                    {"equals": {"key": "category_key", "value": "museums"}},
                    {"equals": {"key": "category_key", "value": "landmarks"}},
                ]
            },
        ]
    }


def test_lambda_reranks_candidates_and_preserves_vector_score(monkeypatch) -> None:
    monkeypatch.setenv(
        "BEDROCK_RERANKING_MODEL_ARN",
        "arn:aws:bedrock:eu-central-1::foundation-model/amazon.rerank-v1:0",
    )

    class Client:
        def rerank(self, **kwargs):
            assert kwargs["sources"][0]["inlineDocumentSource"]["type"] == "JSON"
            return {"results": [{"index": 1, "relevanceScore": 0.8}]}

    monkeypatch.setattr(rag_lambda, "_client", lambda: Client())
    candidates = [
        {"score": 0.7, "content": {"text": "First"}, "metadata": {}},
        {"score": 0.6, "content": {"text": "Second"}, "metadata": {}},
    ]
    ranked, used = rag_lambda._rerank("museums", candidates, 1)
    assert used is True
    assert ranked[0]["score"] == 0.6
    assert ranked[0]["_rerank_score"] == 0.8


def test_lambda_rejects_unknown_tool_and_invalid_queries() -> None:
    unknown = rag_lambda.lambda_handler({"query": "x"}, _context("other___tool"))
    missing = rag_lambda.lambda_handler({}, _context())
    invalid_top_k = rag_lambda.lambda_handler(
        {"query": "x", "destination": "Paris", "top_k": "many"}, _context()
    )
    assert unknown["statusCode"] == 400
    assert missing["statusCode"] == 400
    assert invalid_top_k["statusCode"] == 400


@pytest.mark.parametrize(
    "event,error",
    [
        ({"query": "x", "destination": "d" * 201}, "invalid_destination"),
        ({"query": "x", "country": "c" * 201}, "invalid_country"),
        ({"query": "q" * 500, "destination": "d" * 200, "country": "c" * 200}, "invalid_query"),
        ({"query": "x", "destination": 12}, "invalid_destination"),
        ({"query": "x", "destination": "Paris", "top_k": True}, "invalid_top_k"),
        ({"query": "x", "destination": "Paris", "top_k": 0}, "invalid_top_k"),
        ({"query": "x", "destination": "Paris", "top_k": 11}, "invalid_top_k"),
    ],
)
def test_lambda_bounds_retrieval_input(event: dict, error: str) -> None:
    response = rag_lambda.lambda_handler(event, _context())
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == error


def test_lambda_collapses_repeat_listings_for_one_attraction() -> None:
    # The Paris overview page and the 7th arrondissement page both list Orsay.
    candidates = [
        {"metadata": {"entity_name": "Musée d'Orsay", "title": "Paris"}},
        {"metadata": {"entity_name": "Musée du Louvre", "title": "Paris"}},
        {"metadata": {"entity_name": "  MUSÉE D'ORSAY ", "title": "Paris/7th arrondissement"}},
        {"metadata": {"title": "Paris/7th arrondissement"}},
        {"metadata": {"entity_name": "", "title": "Paris"}},
    ]
    unique = rag_lambda._deduplicate_entities(candidates)
    assert [item["metadata"].get("entity_name") for item in unique] == [
        "Musée d'Orsay",
        "Musée du Louvre",
        None,
        "",
    ]


def test_candidate_count_overfetches_so_deduplication_can_still_fill_top_k(monkeypatch) -> None:
    monkeypatch.delenv("BEDROCK_RERANKING_MODEL_ARN", raising=False)
    assert rag_lambda._candidate_count(5) == 10
    monkeypatch.setenv("BEDROCK_RERANKING_MODEL_ARN", "arn:aws:bedrock:eu-central-1::x/rerank")
    monkeypatch.setenv("RAG_RERANK_CANDIDATES", "20")
    assert rag_lambda._candidate_count(5) == 20


def test_lambda_rejects_non_object_input() -> None:
    response = rag_lambda.lambda_handler(["not", "an", "object"], _context())
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "invalid_input"


def test_filter_normalization_matches_ingestion_and_lambda() -> None:
    assert destination_filter_value("  PARIS/7th arrondissement  ") == "paris"
    assert normalize_filter_value("  Café\u00a0City ") == "café city"
    assert rag_lambda._normalize_filter_value("  Café\u00a0City ") == "café city"


def test_lambda_adds_destination_to_query_naturally_without_duplication() -> None:
    assert (
        rag_lambda._focus_query("What museums should I visit?", "Paris", "France")
        == "What museums should I visit in Paris, France?"
    )
    assert rag_lambda._focus_query("Museums in Paris", "Paris", "France") == "Museums in Paris"


def test_lambda_enrichment_preserves_specific_user_constraints() -> None:
    enriched = rag_lambda._enrich_query(
        "I would like Cambodian food. Can you recommend somewhere to eat?",
        "Paris",
        "France",
        "Restaurant; dining and places to eat",
    )
    assert "Cambodian food" in enriched
    assert "somewhere to eat" in enriched
    assert "Destination: Paris" in enriched
    assert "Country: France" in enriched
    assert "Category: Restaurant; dining and places to eat" in enriched


@pytest.mark.parametrize(
    ("query", "expected_intent"),
    [
        ("Which museums should I visit?", "museums"),
        ("Is there a science museum nearby?", "museums"),
        ("Show me historical monuments", "landmarks"),
        ("I would like to see a Gothic cathedral", "landmarks"),
        ("Which attracions should I visit?", "attractions"),
        ("What are the best things to do?", "activities"),
        ("Recommend local restaurants", "restaurants"),
        ("Where should I eat?", "restaurants"),
        ("Can you recommend somewhere to eat?", "restaurants"),
        ("where to eat in Lisbon", "restaurants"),
        ("Where are the best bars?", "drinks"),
        ("Can we find a nightclub?", "drinks"),
        ("Find budget hotels", "accommodation"),
        ("Where can I stay for two nights?", "accommodation"),
        ("Where should I go shopping?", "shopping"),
        ("I am looking for a bookshop", "shopping"),
    ],
)
def test_lambda_routes_broad_travel_intents(query: str, expected_intent: str) -> None:
    route = rag_lambda._intent_route(query)
    assert route is not None
    assert route[0] == expected_intent
    assert route[1]
    assert route[2]


def test_lambda_leaves_specific_or_unknown_intent_unrouted() -> None:
    assert rag_lambda._intent_route("Tell me about Musée d'Orsay") is None
    assert rag_lambda._intent_route("How do I reach the airport?") is None


def test_lambda_retries_without_section_filter_when_route_has_no_results(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_KNOWLEDGE_BASE_ID", "KB12345678")
    monkeypatch.setenv("RAG_CORPUS_VERSION", "pilot-v1")
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    def retrieve(query, _top_k, destination_key, sections=(), _category_key=None):
        calls.append((query, destination_key, sections))
        return {"retrievalResults": [] if sections else [{"metadata": {}, "content": {}}]}

    monkeypatch.setattr(rag_lambda, "_retrieve", retrieve)
    response = rag_lambda.lambda_handler(
        {"query": "Which museums should I visit?", "destination": "Nowhere"},
        _context(),
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert len(calls) == 2
    assert calls[0][0] == (
        "Which museums should I visit in Nowhere?\n\n"
        "Destination: Nowhere\n"
        "Category: Museum or art gallery; cultural attraction"
    )
    assert calls[0][2]
    assert calls[1] == ("Which museums should I visit in Nowhere?", "nowhere", ())
    assert body["retrieval"]["query_rewritten"] is False
    assert body["retrieval"]["query_enriched"] is False
    assert body["retrieval"]["retrieval_query"] == "Which museums should I visit in Nowhere?"
    assert body["retrieval"]["section_filters"] == []
    assert body["retrieval"]["route_fallback"] is True


@pytest.mark.parametrize(
    ("target", "overlap", "minimum"),
    [
        (0, 0, 0),
        (8_001, 0, 1),
        (100, -1, 20),
        (100, 100, 20),
        (100, 10, 0),
        (100, 10, 101),
    ],
)
def test_chunking_configuration_is_validated(target: int, overlap: int, minimum: int) -> None:
    with pytest.raises(ValueError):
        validate_chunking_parameters(
            target_tokens=target,
            overlap_tokens=overlap,
            minimum_tokens=minimum,
        )


@pytest.mark.parametrize("workers", [0, -1, 33])
def test_ingestion_configuration_is_validated(workers: int) -> None:
    with pytest.raises(IngestError):
        validate_ingest_options(workers=workers)


@pytest.mark.parametrize("workers", [0, -1, 65])
def test_metadata_backfill_configuration_is_validated(workers: int) -> None:
    with pytest.raises(IngestError):
        validate_backfill_options(workers=workers)


def test_prepared_corpus_validation_passes_for_consistent_files(tmp_path: Path) -> None:
    prepared = _prepared_corpus(tmp_path / "prepared")
    manifest, records = load_prepared(
        prepared,
        prefix="wikivoyage",
        namespace="pilot-v1",
    )
    assert len(records) == manifest["chunk_count"]


def test_prepared_corpus_requires_namespace_to_equal_version(tmp_path: Path) -> None:
    prepared = _prepared_corpus(tmp_path / "prepared")
    with pytest.raises(IngestError, match="namespace must equal corpus_version"):
        load_prepared(prepared, prefix="wikivoyage", namespace="another-version")


def test_prepared_corpus_rejects_manifest_count_mismatch(tmp_path: Path) -> None:
    prepared = _prepared_corpus(tmp_path / "prepared")
    manifest_path = prepared / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["chunk_count"] += 1
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(IngestError, match="chunk count mismatch"):
        load_prepared(prepared, prefix="wikivoyage", namespace="pilot-v1")


def test_prepared_corpus_rejects_inconsistent_s3_key(tmp_path: Path) -> None:
    prepared = _prepared_corpus(tmp_path / "prepared")
    chunks_path = prepared / "chunks.jsonl"
    records = [json.loads(line) for line in chunks_path.read_text().splitlines()]
    records[0]["metadata"]["s3_key"] = "wikivoyage/pilot-v1/pages/other.json"
    chunks_path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    with pytest.raises(IngestError, match="inconsistent S3 metadata"):
        load_prepared(prepared, prefix="wikivoyage", namespace="pilot-v1")


def test_bedrock_metadata_keeps_citations_without_embedding_metadata(
    tmp_path: Path,
) -> None:
    prepared = _prepared_corpus(tmp_path / "prepared")
    page = json.loads(next((prepared / "pages").glob("*.json")).read_text())
    metadata = bedrock_metadata(page["chunks"][0], "pilot-v1")["metadataAttributes"]
    assert metadata["title"]["includeForEmbedding"] is False
    assert metadata["title_normalized"]["value"]["stringValue"] == "example"
    assert metadata["destination_key"]["value"]["stringValue"] == "example"
    assert metadata["category_key"]["includeForEmbedding"] is False
    assert metadata["document_type"]["value"]["stringValue"] == "section_prose"
    assert "entity_name" not in metadata
    assert metadata["section"]["includeForEmbedding"] is False
    assert metadata["source_url"]["includeForEmbedding"] is False
    assert metadata["corpus_version"]["value"]["stringValue"] == "pilot-v1"
    assert source_prefix("wikivoyage", "pilot-v1") == ("wikivoyage/pilot-v1/kb-documents/")


def test_bedrock_metadata_rejects_required_empty_string_attributes(tmp_path: Path) -> None:
    prepared = _prepared_corpus(tmp_path / "prepared")
    page = json.loads(next((prepared / "pages").glob("*.json")).read_text())
    page["chunks"][0]["section_path"] = ""
    with pytest.raises(IngestError, match="STRING metadata attributes cannot be empty"):
        bedrock_metadata(page["chunks"][0], "pilot-v1")


def test_existing_bedrock_sidecar_backfills_exact_filter_metadata(tmp_path: Path) -> None:
    prepared = _prepared_corpus(tmp_path / "prepared")
    page = json.loads(next((prepared / "pages").glob("*.json")).read_text())
    document = bedrock_metadata(page["chunks"][0], "pilot-v1")
    attributes = document["metadataAttributes"]
    attributes.pop("title_normalized")
    attributes.pop("destination_key")

    updated, changed = add_filter_metadata(json.dumps(document).encode())
    decoded = json.loads(updated)
    assert changed is True
    assert decoded["metadataAttributes"]["destination_key"]["value"]["stringValue"] == "example"
    _, changed_again = add_filter_metadata(updated)
    assert changed_again is False


def test_dry_run_rejects_oversized_bedrock_metadata(monkeypatch, tmp_path: Path) -> None:
    prepared = _prepared_corpus(tmp_path / "prepared")
    monkeypatch.setattr(rag_ingest, "MAX_METADATA_BYTES", 1)
    with pytest.raises(IngestError, match="metadata.*exceeds"):
        ingest_prepared(
            prepared,
            prefix="wikivoyage",
            bucket="bucket",
            namespace="pilot-v1",
            knowledge_base_id="KB123",
            data_source_id="DS123",
            region="eu-central-1",
            dry_run=True,
        )


def test_wait_for_bedrock_ingestion_job(monkeypatch) -> None:
    statuses = iter(("STARTING", "IN_PROGRESS", "COMPLETE"))

    class BedrockAgent:
        def get_ingestion_job(self, **_kwargs):
            status = next(statuses)
            return {
                "ingestionJob": {
                    "ingestionJobId": "JOB123",
                    "status": status,
                    "statistics": {"numberOfNewDocumentsIndexed": 3},
                }
            }

    monkeypatch.setattr(rag_ingest.time, "sleep", lambda _seconds: None)
    job = wait_for_ingestion_job(
        BedrockAgent(),
        knowledge_base_id="KB123",
        data_source_id="DS123",
        ingestion_job_id="JOB123",
        attempts=3,
    )
    assert job["status"] == "COMPLETE"


def test_ingestion_uploads_bedrock_documents_and_starts_sync(monkeypatch, tmp_path: Path) -> None:
    prepared = _prepared_corpus(tmp_path / "prepared")
    uploads: list[dict] = []

    class S3:
        def put_object(self, **kwargs):
            uploads.append(kwargs)
            return {}

    class BedrockAgent:
        def start_ingestion_job(self, **kwargs):
            assert kwargs["knowledgeBaseId"] == "KB123"
            assert kwargs["dataSourceId"] == "DS123"
            return {
                "ingestionJob": {
                    "ingestionJobId": "JOB123",
                    "status": "STARTING",
                }
            }

    class Session:
        def client(self, name: str, **_kwargs):
            return S3() if name == "s3" else BedrockAgent()

    monkeypatch.setattr("boto3.Session", lambda **_kwargs: Session())
    result = ingest_prepared(
        prepared,
        prefix="wikivoyage",
        bucket="bucket",
        namespace="pilot-v1",
        knowledge_base_id="KB123",
        data_source_id="DS123",
        region="eu-central-1",
        wait=False,
    )
    document_uploads = [item for item in uploads if item["Key"].endswith(".txt")]
    metadata_uploads = [item for item in uploads if item["Key"].endswith(".txt.metadata.json")]
    assert len(document_uploads) == len(metadata_uploads) == result["uploaded_documents"]
    assert all("/kb-documents/" in item["Key"] for item in document_uploads)
    assert result["ingestion_job_id"] == "JOB123"
    assert result["ingestion_status"] == "STARTING"
