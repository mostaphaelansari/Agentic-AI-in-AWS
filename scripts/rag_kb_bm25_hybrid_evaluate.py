#!/usr/bin/env python3
"""Evaluate temporary Titan-semantic + local-BM25 hybrid retrieval.

Bedrock Knowledge Bases backed by Pinecone cannot run a lexical BM25 arm.
This experiment keeps the existing KB unchanged: it fetches Titan semantic
candidates from Bedrock, fetches BM25 candidates from the byte-identical local
Qdrant collection, fuses the two ranked lists with reciprocal-rank fusion,
deduplicates entities, and emits the same Hit@k/MRR metrics as the other RAG
evaluators.

The local collection and the KB must contain the same ``corpus_version`` and
stable ``chunk_id`` values.  This is suitable for evaluation and for proving a
Lambda-side fusion design; it is not intended to make a laptop-hosted Qdrant
service a production dependency of Lambda.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.opensource import query as planner  # noqa: E402
from rag.opensource.bm25 import BM25Index  # noqa: E402
from rag.opensource.index import load_index_manifest  # noqa: E402
from rag.opensource.store import TravelIndex  # noqa: E402
from scripts.rag_kb_evaluate import (  # noqa: E402
    _deduplicate_entities,
    _matches,
    _retrieval_filter,
)

DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_COLLECTION = "travel-rag-eval"
DEFAULT_INDEX_MANIFEST = (
    ROOT / "opensource" / "compose" / "data" / "rag" / "index-manifest.json"
)


def _chunk_id(result: dict[str, Any]) -> str:
    return str((result.get("metadata") or {}).get("chunk_id") or "").strip()


def reciprocal_rank_fuse(
    semantic: list[dict[str, Any]],
    bm25: list[dict[str, Any]],
    *,
    rank_constant: int = 1,
) -> list[dict[str, Any]]:
    """Fuse two result lists by stable chunk ID.

    Qdrant's RRF contribution is equivalent to ``1 / (rank + 1)`` for a
    one-based rank.  Keeping the constant configurable makes it possible to
    test the more conservative conventional RRF constants without changing
    code.
    """
    if rank_constant < 0:
        raise ValueError("rank_constant must be non-negative")

    fused: dict[str, dict[str, Any]] = {}
    insertion_order = 0
    for arm, results in (("semantic", semantic), ("bm25", bm25)):
        for rank, result in enumerate(results, start=1):
            chunk_id = _chunk_id(result)
            if not chunk_id:
                raise RuntimeError(f"{arm} result at rank {rank} has no chunk_id")
            entry = fused.get(chunk_id)
            if entry is None:
                entry = {
                    "result": dict(result),
                    "fusion_score": 0.0,
                    "semantic_rank": None,
                    "semantic_score": None,
                    "bm25_rank": None,
                    "bm25_score": None,
                    "order": insertion_order,
                }
                fused[chunk_id] = entry
                insertion_order += 1
            entry["fusion_score"] += 1.0 / (rank + rank_constant)
            entry[f"{arm}_rank"] = rank
            entry[f"{arm}_score"] = float(result.get("score") or 0.0)

    ordered = sorted(
        fused.values(),
        key=lambda entry: (-entry["fusion_score"], entry["order"]),
    )
    output = []
    for entry in ordered:
        result = entry.pop("result")
        entry.pop("order")
        result.update(entry)
        # Keep the standard score field meaningful for ordering while
        # retaining both native arm scores in separate fields.
        result["score"] = result["fusion_score"]
        output.append(result)
    return output


class LocalBM25:
    """Sparse retrieval over the exact Qdrant corpus used for comparison."""

    def __init__(self, qdrant_url: str, collection: str, manifest_path: Path) -> None:
        manifest = load_index_manifest(manifest_path)
        manifest_collection = str(manifest.get("collection") or "")
        if manifest_collection and manifest_collection != collection:
            raise RuntimeError(
                f"Index manifest belongs to {manifest_collection!r}, not {collection!r}"
            )
        self.corpus_versions = tuple(str(v) for v in manifest.get("corpus_versions") or [])
        self.encoder = BM25Index.from_manifest(manifest["bm25"])
        self.index = TravelIndex.connect(qdrant_url, collection)
        if not self.index.exists():
            raise RuntimeError(f"Qdrant collection does not exist: {collection}")

    def retrieve(
        self,
        query: str,
        limit: int,
        destination_key: str,
        sections: tuple[str, ...],
        category_keys: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        indices, values = self.encoder.encode_query(query)
        hits = self.index.hybrid_search(
            dense_vector=None,
            sparse_indices=list(indices),
            sparse_values=list(values),
            query_filter=planner.retrieval_filter(
                destination_key,
                sections,
                category_keys,
            ),
            limit=limit,
        )
        return [
            {
                "score": float(hit.get("native_score") or 0.0),
                "content": {"text": (hit.get("payload") or {}).get("text", "")},
                "metadata": dict(hit.get("payload") or {}),
            }
            for hit in hits
        ]


def _semantic_retrieve(
    client,
    knowledge_base_id: str,
    query: str,
    limit: int,
    destination_key: str,
    sections: tuple[str, ...],
    category_keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    response = client.retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": limit,
                "overrideSearchType": "SEMANTIC",
                "filter": _retrieval_filter(destination_key, category_keys, sections),
            }
        },
    )
    return list(response.get("retrievalResults") or [])


def evaluate_hybrid(
    client,
    bm25: LocalBM25,
    knowledge_base_id: str,
    cases: list[dict[str, Any]],
    *,
    top_k: int,
    fusion_depth: int,
    expected_corpus_version: str,
    category_filter_enabled: bool = True,
    rank_constant: int = 1,
) -> dict[str, Any]:
    rows = []
    for case in cases:
        plan = planner.plan(
            case["query"],
            case["destination"],
            case.get("country", ""),
            category_filter_enabled=category_filter_enabled,
        )
        query = plan["retrieval_query"]
        semantic = _semantic_retrieve(
            client,
            knowledge_base_id,
            query,
            fusion_depth,
            plan["destination_key"],
            plan["section_filters"],
            plan["category_filters"],
        )
        sparse = bm25.retrieve(
            query,
            fusion_depth,
            plan["destination_key"],
            plan["section_filters"],
            plan["category_filters"],
        )

        route_fallback = False
        if not semantic and not sparse and plan["intent"]:
            route_fallback = True
            query = planner.focus_query(
                case["query"], case["destination"], case.get("country", "")
            )
            semantic = _semantic_retrieve(
                client,
                knowledge_base_id,
                query,
                fusion_depth,
                plan["destination_key"],
                (),
                (),
            )
            sparse = bm25.retrieve(
                query,
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

        fused = reciprocal_rank_fuse(
            semantic,
            sparse,
            rank_constant=rank_constant,
        )
        candidate_count = planner.candidate_count(top_k, rerank_enabled=False)
        results = _deduplicate_entities(fused[:candidate_count])[:top_k]
        rank = next(
            (
                position
                for position, result in enumerate(results, start=1)
                if _matches(case, result)
            ),
            None,
        )
        top = results[0] if results else {}
        rows.append(
            {
                "query": case["query"],
                "category": case.get("category_key") or plan["intent"],
                "retrieval_query": query,
                "route_fallback": route_fallback,
                "rank": rank,
                "top_entity": (top.get("metadata") or {}).get("entity_name"),
                "top_fusion_score": float(top.get("fusion_score") or 0.0),
                "top_semantic_rank": top.get("semantic_rank"),
                "top_bm25_rank": top.get("bm25_rank"),
            }
        )

    return {
        "search_type": "SEMANTIC_BM25_RRF",
        "corpus_version": expected_corpus_version or None,
        "comparison_contract": {
            "query_planner": "rag.opensource.query",
            "category_filter": category_filter_enabled,
            "rerank": False,
            "deduplicate_entities": True,
            "fusion": "RRF",
            "rank_constant": rank_constant,
            "fusion_depth_per_arm": fusion_depth,
            "candidate_count_after_fusion": planner.candidate_count(
                top_k, rerank_enabled=False
            ),
            "top_k": top_k,
        },
        "queries": len(rows),
        f"hit_at_{top_k}": sum(row["rank"] is not None for row in rows) / len(rows),
        "mrr": statistics.fmean((1 / row["rank"] if row["rank"] else 0) for row in rows),
        "top1_named_entity_rate": sum(bool(row["top_entity"]) for row in rows) / len(rows),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--fusion-depth", type=int, default=50)
    parser.add_argument("--rank-constant", type=int, default=1)
    parser.add_argument("--corpus-version", required=True)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--index-manifest", type=Path, default=DEFAULT_INDEX_MANIFEST)
    parser.add_argument("--no-category-filter", action="store_true")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="omit per-query rows from the JSON output",
    )
    args = parser.parse_args()
    if not 1 <= args.top_k <= 100:
        parser.error("--top-k must be between 1 and 100")
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
    payload = json.loads(args.queries.read_text(encoding="utf-8"))
    client = boto3.client("bedrock-agent-runtime", region_name=args.region)
    report = evaluate_hybrid(
        client,
        bm25,
        args.knowledge_base_id,
        payload["queries"],
        top_k=args.top_k,
        fusion_depth=args.fusion_depth,
        expected_corpus_version=args.corpus_version,
        category_filter_enabled=not args.no_category_filter,
        rank_constant=args.rank_constant,
    )
    if args.summary_only:
        report.pop("rows", None)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
