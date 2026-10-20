#!/usr/bin/env python3
"""Search one query with Titan semantic retrieval plus local BM25 RRF."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.opensource import query as planner  # noqa: E402
from scripts.rag_kb_bm25_hybrid_evaluate import (  # noqa: E402
    DEFAULT_COLLECTION,
    DEFAULT_INDEX_MANIFEST,
    DEFAULT_QDRANT_URL,
    LocalBM25,
    _semantic_retrieve,
    reciprocal_rank_fuse,
)
from scripts.rag_kb_evaluate import _deduplicate_entities  # noqa: E402


def _fill_missing_semantic_scores(
    client,
    knowledge_base_id: str,
    retrieval_query: str,
    results: list[dict[str, Any]],
) -> None:
    """Score BM25-only winners with Titan without inventing a semantic rank."""
    for result in results:
        if result.get("semantic_score") is not None:
            continue
        chunk_id = str((result.get("metadata") or {}).get("chunk_id") or "")
        if not chunk_id:
            continue
        response = client.retrieve(
            knowledgeBaseId=knowledge_base_id,
            retrievalQuery={"text": retrieval_query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": 1,
                    "overrideSearchType": "SEMANTIC",
                    "filter": {
                        "equals": {
                            "key": "chunk_id",
                            "value": chunk_id,
                        }
                    },
                }
            },
        )
        scored = list(response.get("retrievalResults") or [])
        if scored:
            result["semantic_score"] = float(scored[0].get("score") or 0.0)


def hybrid_search(
    client,
    bm25: LocalBM25,
    *,
    knowledge_base_id: str,
    query: str,
    destination: str,
    country: str,
    top_k: int,
    fusion_depth: int,
    category_filter_enabled: bool,
    rank_constant: int,
    expected_corpus_version: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    plan = planner.plan(
        query,
        destination,
        country,
        category_filter_enabled=category_filter_enabled,
    )
    retrieval_query = plan["retrieval_query"]
    semantic = _semantic_retrieve(
        client,
        knowledge_base_id,
        retrieval_query,
        fusion_depth,
        plan["destination_key"],
        plan["section_filters"],
        plan["category_filters"],
    )
    sparse = bm25.retrieve(
        retrieval_query,
        fusion_depth,
        plan["destination_key"],
        plan["section_filters"],
        plan["category_filters"],
    )

    route_fallback = False
    if not semantic and not sparse and plan["intent"]:
        route_fallback = True
        retrieval_query = planner.focus_query(query, destination, country)
        semantic = _semantic_retrieve(
            client,
            knowledge_base_id,
            retrieval_query,
            fusion_depth,
            plan["destination_key"],
            (),
            (),
        )
        sparse = bm25.retrieve(
            retrieval_query,
            fusion_depth,
            plan["destination_key"],
            (),
            (),
        )

    versions = {
        str((result.get("metadata") or {}).get("corpus_version") or "")
        for result in [*semantic, *sparse]
    }
    if expected_corpus_version and versions - {expected_corpus_version}:
        raise RuntimeError(
            f"Hybrid arms returned corpus versions {sorted(versions)!r}; "
            f"expected only {expected_corpus_version!r}"
        )

    fused = reciprocal_rank_fuse(semantic, sparse, rank_constant=rank_constant)
    candidate_count = planner.candidate_count(top_k, rerank_enabled=False)
    results = _deduplicate_entities(fused[:candidate_count])[:top_k]
    _fill_missing_semantic_scores(
        client,
        knowledge_base_id,
        retrieval_query,
        results,
    )
    return results, {
        "retrieval_query": retrieval_query,
        "intent": plan["intent"],
        "route_fallback": route_fallback,
        "semantic_candidates": len(semantic),
        "bm25_candidates": len(sparse),
        "candidate_count_after_fusion": candidate_count,
    }


def _rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rank": rank,
            "museum": str((result.get("metadata") or {}).get("entity_name") or ""),
            "rrf": float(result.get("fusion_score") or 0.0),
            "semantic_score": result.get("semantic_score"),
            "semantic_rank": result.get("semantic_rank"),
            "bm25_score": result.get("bm25_score"),
            "bm25_rank": result.get("bm25_rank"),
        }
        for rank, result in enumerate(results, start=1)
    ]


def _print_table(rows: list[dict[str, Any]]) -> None:
    headers = (
        "rank",
        "museum",
        "rrf",
        "semantic_score",
        "semantic_rank",
        "bm25_score",
        "bm25_rank",
    )
    display = [
        {
            "rank": str(row["rank"]),
            "museum": row["museum"],
            "rrf": f"{row['rrf']:.4f}",
            "semantic_score": (
                f"{row['semantic_score']:.4f}"
                if row["semantic_score"] is not None
                else "-"
            ),
            "semantic_rank": str(row["semantic_rank"] or "-"),
            "bm25_score": (
                f"{row['bm25_score']:.4f}" if row["bm25_score"] is not None else "-"
            ),
            "bm25_rank": str(row["bm25_rank"] or "-"),
        }
        for row in rows
    ]
    widths = {
        header: max(len(header), *(len(row[header]) for row in display))
        for header in headers
    }
    separator = "+-" + "-+-".join("-" * widths[header] for header in headers) + "-+"
    print(separator)
    print("| " + " | ".join(header.ljust(widths[header]) for header in headers) + " |")
    print(separator)
    for row in display:
        print("| " + " | ".join(row[header].ljust(widths[header]) for header in headers) + " |")
    print(separator)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--query", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--country", default="")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--fusion-depth", type=int, default=50)
    parser.add_argument("--rank-constant", type=int, default=1)
    parser.add_argument("--corpus-version", required=True)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--index-manifest", type=Path, default=DEFAULT_INDEX_MANIFEST)
    parser.add_argument("--no-category-filter", action="store_true")
    parser.add_argument("--output", choices=("table", "json"), default="table")
    args = parser.parse_args()
    if not 1 <= args.top_k <= 10:
        parser.error("--top-k must be between 1 and 10")
    if not args.top_k <= args.fusion_depth <= 100:
        parser.error("--fusion-depth must be between top-k and 100")
    if args.rank_constant < 0:
        parser.error("--rank-constant must be non-negative")

    bm25 = LocalBM25(args.qdrant_url, args.collection, args.index_manifest)
    if bm25.corpus_versions and bm25.corpus_versions != (args.corpus_version,):
        parser.error(
            "local manifest corpus versions do not exactly match --corpus-version: "
            f"{bm25.corpus_versions!r}"
        )
    client = boto3.client("bedrock-agent-runtime", region_name=args.region)
    results, diagnostics = hybrid_search(
        client,
        bm25,
        knowledge_base_id=args.knowledge_base_id,
        query=args.query,
        destination=args.destination,
        country=args.country,
        top_k=args.top_k,
        fusion_depth=args.fusion_depth,
        category_filter_enabled=not args.no_category_filter,
        rank_constant=args.rank_constant,
        expected_corpus_version=args.corpus_version,
    )
    rows = _rows(results)
    if args.output == "json":
        print(json.dumps({"retrieval": diagnostics, "results": rows}, indent=2, ensure_ascii=False))
    else:
        print(
            f"intent={diagnostics['intent']} ranking=SEMANTIC_BM25_RRF "
            f"semantic={diagnostics['semantic_candidates']} "
            f"bm25={diagnostics['bm25_candidates']} "
            f"fallback={diagnostics['route_fallback']}"
        )
        print(f"query -> {diagnostics['retrieval_query']!r}")
        _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
