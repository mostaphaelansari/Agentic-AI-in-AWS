#!/usr/bin/env python3
"""Index and search the prepared travel corpus with local Neo4j GraphRAG.

Examples:
  uv run python scripts/neo4j_rag.py index \
    --prepared /home/a953083/wikivoyage-corpus/wikivoyage-pilot-v3

  uv run python scripts/neo4j_rag.py search \
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

from rag.neo4j import (  # noqa: E402
    Neo4jGraphStore,
    Neo4jStoreError,
    deduplicate_seeds,
    finalize_results,
    index_prepared,
)
from rag.opensource import query as planner  # noqa: E402
from rag.opensource.embeddings import (  # noqa: E402
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_HOST,
    OllamaEmbedder,
)


def _store(args: argparse.Namespace) -> Neo4jGraphStore:
    return Neo4jGraphStore.connect(
        args.neo4j_uri,
        args.neo4j_user,
        args.neo4j_password,
        database=args.neo4j_database,
    )


def _embedder(args: argparse.Namespace) -> OllamaEmbedder:
    return OllamaEmbedder(
        host=args.ollama_host,
        model=args.embed_model,
        batch_size=args.embed_batch_size,
    )


def command_index(args: argparse.Namespace) -> int:
    with _store(args) as store:
        result = index_prepared(
            args.prepared,
            store=store,
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
    with _store(args) as store:
        print(json.dumps({**store.counts(), "destination_keys": store.destinations()}, indent=2))
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
    vector = _embedder(args).embed_one(plan["retrieval_query"])
    with _store(args) as store:
        seeds = store.search_seeds(
            vector,
            destination_key=plan["destination_key"],
            category_keys=plan["category_filters"],
            limit=candidate_count,
        )
        route_fallback = False
        if not seeds and plan["intent"]:
            route_fallback = True
            fallback = planner.focus_query(query, destination, country)
            vector = _embedder(args).embed_one(fallback)
            seeds = store.search_seeds(
                vector,
                destination_key=plan["destination_key"],
                limit=candidate_count,
            )
        unique_seeds = deduplicate_seeds(seeds)
        expanded = store.expand_context(unique_seeds)

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
                "ranking": ranking,
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
        print(
            f"{position}. {node.get('entity_name') or node.get('section') or node.get('id')} "
            f"[{node.get('category_key')}] cosine={result['score']:.4f}{rerank_text} "
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
    parser.add_argument(
        "--neo4j-uri", default=os.environ.get("NEO4J_URI", "neo4j://localhost:7687")
    )
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument(
        "--neo4j-password",
        default=os.environ.get("NEO4J_PASSWORD", "local-graph-rag"),
    )
    parser.add_argument("--neo4j-database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    parser.add_argument(
        "--ollama-host",
        default=os.environ.get("OLLAMA_HOST", "http://localhost:11435"),
        help=f"Ollama endpoint (library default is {DEFAULT_OLLAMA_HOST})",
    )
    parser.add_argument(
        "--embed-model",
        default=os.environ.get("OLLAMA_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL),
    )
    parser.add_argument("--embed-batch-size", type=int, default=16)
    subparsers = parser.add_subparsers(dest="command", required=True)

    index = subparsers.add_parser("index", help="embed and load prepared corpora")
    index.add_argument("--prepared", type=Path, nargs="+", required=True)
    index.add_argument("--batch-size", type=int, default=100)
    index.add_argument("--no-resume", action="store_true")
    index.add_argument(
        "--destination",
        action="append",
        default=[],
        help="index only this destination; repeat for several (pilot helper)",
    )
    index.add_argument(
        "--category",
        action="append",
        default=[],
        help="index only this category; repeat for several (pilot helper)",
    )
    index.set_defaults(func=command_index)

    status = subparsers.add_parser("status", help="show graph counts and destinations")
    status.set_defaults(func=command_status)

    search = subparsers.add_parser("search", help="run vector-plus-graph retrieval")
    search.add_argument("--query", required=True)
    search.add_argument("--destination", default="")
    search.add_argument("--country", default="")
    search.add_argument("--all-destinations", action="store_true")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--no-category-filter", action="store_true")
    search.add_argument(
        "--reranker-url",
        default=os.environ.get("RERANKER_URL", "http://localhost:8082"),
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
    except (Neo4jStoreError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
