#!/usr/bin/env python3
"""Index and search travel GraphRAG with OpenSearch plus Neptune Analytics.

The two stores join on the prepared corpus ``chunk_id``. OpenSearch performs
filtered vector + BM25 retrieval, Neptune expands relationships, and the shared
cross-encoder reranker orders the graph-enriched passages.

Examples:
  uv run python scripts/neptune_opensearch_rag.py index \
    --prepared /path/to/wikivoyage-pilot-v3 \
    --destination Paris --category museums

  uv run python scripts/neptune_opensearch_rag.py search \
    --destination Paris --query "museums and art galleries" --show-context
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.neo4j.retrieve import deduplicate_seeds, finalize_results  # noqa: E402
from rag.neptune_opensearch import (  # noqa: E402
    NeptuneAnalyticsGraphStore,
    NeptuneStoreError,
    OpenSearchGraphStore,
    OpenSearchStoreError,
    expand_seed_context,
    index_prepared,
)
from rag.opensource import query as planner  # noqa: E402
from rag.opensource.embeddings import (  # noqa: E402
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_HOST,
    DEFAULT_TITAN_DIMENSIONS,
    DEFAULT_TITAN_MODEL,
    BedrockTitanEmbedder,
    Embedder,
    EmbeddingError,
    OllamaEmbedder,
)


def _graph_store(args: argparse.Namespace) -> NeptuneAnalyticsGraphStore:
    return NeptuneAnalyticsGraphStore(
        args.neptune_graph_id,
        region=args.region,
        namespace=args.graph_namespace,
    )


def _search_store(args: argparse.Namespace) -> OpenSearchGraphStore:
    return OpenSearchGraphStore(
        args.opensearch_endpoint,
        args.opensearch_index,
        region=args.region,
        service=args.opensearch_service,
    )


def _embedder(args: argparse.Namespace) -> Embedder:
    if args.embedding_provider == "titan":
        return BedrockTitanEmbedder(
            region=args.region,
            model=args.titan_model,
            dimensions=args.titan_dimensions,
        )
    return OllamaEmbedder(
        host=args.ollama_host,
        model=args.embed_model,
        batch_size=args.embed_batch_size,
    )


def command_index(args: argparse.Namespace) -> int:
    with _search_store(args) as search_store:
        result = index_prepared(
            args.prepared,
            search_store=search_store,
            graph_store=_graph_store(args),
            embedder=_embedder(args),
            batch_size=args.batch_size,
            resume=not args.no_resume,
            destinations=args.destination,
            categories=args.category,
            progress=print,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def command_status(args: argparse.Namespace) -> int:
    with _search_store(args) as search_store:
        result = {
            "opensearch_documents": search_store.count(),
            **{f"neptune_{key}": value for key, value in _graph_store(args).counts().items()},
        }
    print(json.dumps(result, indent=2))
    return 0


def command_search(args: argparse.Namespace) -> int:
    query, destination, country, top_k = planner.validate_inputs(
        args.query,
        args.destination,
        args.country,
        args.top_k,
        require_destination=not args.all_destinations,
    )
    plan = planner.plan(
        query,
        destination,
        country,
        category_filter_enabled=not args.no_category_filter,
    )
    reranker_url = "" if args.no_rerank else args.reranker_url
    candidate_count = planner.candidate_count(
        top_k,
        rerank_enabled=bool(reranker_url),
        rerank_candidates=args.rerank_candidates,
    )
    embedder = _embedder(args)
    vector = embedder.embed_one(plan["retrieval_query"])
    graph_store = _graph_store(args)
    with _search_store(args) as search_store:
        seeds = search_store.hybrid_search(
            plan["retrieval_query"],
            vector,
            destination_key=plan["destination_key"],
            category_keys=plan["category_filters"],
            limit=candidate_count,
            fusion_depth=args.fusion_depth,
            rank_constant=args.rank_constant,
        )
        route_fallback = False
        if not seeds and plan["intent"]:
            route_fallback = True
            fallback = planner.focus_query(query, destination, country)
            vector = embedder.embed_one(fallback)
            seeds = search_store.hybrid_search(
                fallback,
                vector,
                destination_key=plan["destination_key"],
                limit=candidate_count,
                fusion_depth=args.fusion_depth,
                rank_constant=args.rank_constant,
            )
        unique_seeds = deduplicate_seeds(seeds)
        expanded = expand_seed_context(
            unique_seeds,
            search_store=search_store,
            graph_store=graph_store,
        )

    results, ranking = finalize_results(
        query,
        expanded,
        top_k,
        reranker_url=reranker_url,
        reranker_timeout=args.reranker_timeout,
    )
    print(
        json.dumps(
            {
                "intent": plan["intent"],
                "retrieval_query": plan["retrieval_query"],
                "route_fallback": route_fallback,
                "seed_ranking": "OPENSEARCH_VECTOR_BM25_RRF",
                "final_ranking": ranking,
                "candidate_count": len(seeds),
                "deduplicated_count": len(unique_seeds),
                "result_count": len(results),
            },
            ensure_ascii=False,
        )
    )
    for position, result in enumerate(results, start=1):
        node = result["node"]
        rerank_score = result.get("rerank_score")
        rerank_text = f" rerank={rerank_score:.4f}" if rerank_score is not None else ""
        vector_rank = result.get("vector_rank") or "-"
        lexical_rank = result.get("lexical_rank") or "-"
        print(
            f"{position}. {node.get('entity_name') or node.get('section') or node.get('id')} "
            f"[{node.get('category_key')}] rrf={result['score']:.4f} "
            f"vector_rank={vector_rank} bm25_rank={lexical_rank}{rerank_text} "
            f"graph_context={len(result['adjacent']) + len(result['related'])}"
        )
        if args.show_context:
            print(f"   seed: {str(node.get('text') or '')[:400]}")
            for label in ("adjacent", "related"):
                for context in result[label]:
                    print(f"   {label}: {str(context.get('text') or '')[:300]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-central-1"))
    parser.add_argument(
        "--neptune-graph-id",
        default=os.environ.get("NEPTUNE_GRAPH_ID", ""),
        required=not bool(os.environ.get("NEPTUNE_GRAPH_ID")),
    )
    parser.add_argument(
        "--graph-namespace",
        default=os.environ.get("NEPTUNE_GRAPH_NAMESPACE", "travel-custom-v1"),
    )
    parser.add_argument(
        "--opensearch-endpoint",
        default=os.environ.get("OPENSEARCH_ENDPOINT", ""),
        required=not bool(os.environ.get("OPENSEARCH_ENDPOINT")),
    )
    parser.add_argument(
        "--opensearch-index",
        default=os.environ.get("OPENSEARCH_GRAPH_INDEX", "travel-rag-neptune-v1"),
    )
    parser.add_argument(
        "--opensearch-service",
        choices=("aoss", "es"),
        default=os.environ.get("OPENSEARCH_SERVICE", "aoss"),
    )
    parser.add_argument(
        "--embedding-provider",
        choices=("ollama", "titan"),
        default=os.environ.get("GRAPH_EMBEDDING_PROVIDER", "ollama"),
    )
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST))
    parser.add_argument(
        "--embed-model", default=os.environ.get("OLLAMA_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL)
    )
    parser.add_argument("--embed-batch-size", type=int, default=16)
    parser.add_argument("--titan-model", default=DEFAULT_TITAN_MODEL)
    parser.add_argument("--titan-dimensions", type=int, default=DEFAULT_TITAN_DIMENSIONS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    index = subparsers.add_parser("index", help="embed and dual-write prepared corpora")
    index.add_argument("--prepared", type=Path, nargs="+", required=True)
    index.add_argument("--batch-size", type=int, default=100)
    index.add_argument("--no-resume", action="store_true")
    index.add_argument("--destination", action="append", default=[])
    index.add_argument("--category", action="append", default=[])
    index.set_defaults(func=command_index)

    status = subparsers.add_parser("status", help="show document and graph counts")
    status.set_defaults(func=command_status)

    search = subparsers.add_parser("search", help="run hybrid search plus graph expansion")
    search.add_argument("--query", required=True)
    search.add_argument("--destination", default="")
    search.add_argument("--country", default="")
    search.add_argument("--all-destinations", action="store_true")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--fusion-depth", type=int, default=50)
    search.add_argument("--rank-constant", type=int, default=60)
    search.add_argument("--no-category-filter", action="store_true")
    search.add_argument(
        "--reranker-url", default=os.environ.get("RERANKER_URL", "http://localhost:8082")
    )
    search.add_argument("--rerank-candidates", type=int, default=20)
    search.add_argument("--reranker-timeout", type=int, default=120)
    search.add_argument("--no-rerank", action="store_true")
    search.add_argument("--show-context", action="store_true")
    search.set_defaults(func=command_search)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except (EmbeddingError, NeptuneStoreError, OpenSearchStoreError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
