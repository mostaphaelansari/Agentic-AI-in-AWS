#!/usr/bin/env python3
"""Build, query, and score the open-source travel retrieval index.

Counterpart of ``scripts/wikivoyage_rag.py`` (which prepares and ships a corpus
to Bedrock) for the self-hosted Qdrant + Ollama pipeline. Corpus preparation is
shared: run ``wikivoyage_rag.py prepare`` or ``datatourisme_rag.py prepare``
first, then point ``index`` at the prepared directory.

    scripts/opensource_rag.py index    --prepared DIR [DIR ...]
    scripts/opensource_rag.py import-s3 --bucket BUCKET --corpus-version VERSION --output DIR
    scripts/opensource_rag.py search   --query "museums" --destination Paris
    scripts/opensource_rag.py evaluate --queries evaluations/datasets/...json

``evaluate`` deliberately emits the same metric names as
``scripts/rag_kb_evaluate.py`` so open-source and Bedrock numbers can be read
side by side.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import unicodedata
from pathlib import Path

from dotenv import load_dotenv
from qdrant_client.http.exceptions import ResponseHandlingException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.ingest import IngestError  # noqa: E402
from rag.opensource import query as planner  # noqa: E402
from rag.opensource.bm25 import BM25Index, fold  # noqa: E402
from rag.opensource.embeddings import (  # noqa: E402
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_HOST,
    DEFAULT_TITAN_DIMENSIONS,
    DEFAULT_TITAN_MODEL,
    SUPPORTED_TITAN_DIMENSIONS,
    BedrockTitanEmbedder,
    Embedder,
    EmbeddingError,
    OllamaEmbedder,
)
from rag.opensource.index import (  # noqa: E402
    index_prepared,
    load_index_manifest,
)
from rag.opensource.s3_corpus import import_bedrock_s3_corpus  # noqa: E402
from rag.opensource.search import RetrievalConfig, TravelRetriever  # noqa: E402
from rag.opensource.store import StoreError, TravelIndex  # noqa: E402
from rag.wikivoyage import category_filter_value  # noqa: E402

DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_COLLECTION = "travel-rag"
# Where the compose stack mounts the index manifest for the MCP server. Making
# it the default means indexing and querying agree without a flag, and the tool
# container reads exactly the file the CLI just wrote.
DEFAULT_INDEX_MANIFEST = ROOT / "opensource" / "compose" / "data" / "rag" / "index-manifest.json"


# Wikivoyage prose uses typographic punctuation; the evaluation datasets were
# hand-typed with ASCII. NFKD does not unify the two, so without this table
# "Cité des sciences et de l’industrie" (U+2019) never matches the dataset's
# "Cité des sciences et de l'industrie" (U+0027) and a rank-1 hit is scored as
# a miss. Kept byte-identical to scripts/rag_kb_evaluate.py so neither stack is
# advantaged by its acceptance logic.
_PUNCTUATION_FOLD = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "ʼ": "'",
        "´": "'",
        "`": "'",
        "“": '"',
        "”": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
    }
)


def _normalize(value: object) -> str:
    """Accent-fold for entity matching, identical to scripts/rag_kb_evaluate.py."""
    folded = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join(
        "".join(character for character in folded if not unicodedata.combining(character))
        .translate(_PUNCTUATION_FOLD)
        .casefold()
        .split()
    )


def _matches(case: dict, result: dict) -> bool:
    """Acceptance logic ported verbatim from scripts/rag_kb_evaluate.py."""
    accepted_category = case.get("accept_category")
    result_category = result.get("category_key") or category_filter_value(
        str(result.get("section") or "")
    )
    if accepted_category and result_category == accepted_category:
        return True
    entity = _normalize(result.get("entity_name"))
    text = _normalize(result.get("text"))
    return any(
        (bool(entity) and (accepted in entity or entity in accepted)) or accepted in text
        for accepted in (_normalize(value) for value in case.get("accept") or [])
        if accepted and (entity or text)
    )


def _infer_destination(query: str, known: list[str]) -> str | None:
    """Pick the indexed destination the query names, if any.

    Matching is restricted to destinations that exist in the collection, so
    this cannot invent a place; it is a lookup, not entity recognition. The
    longest match wins so "new york" beats a hypothetical "york".

    This is a convenience for driving the CLI by hand. The MCP tool still
    requires an explicit destination, because the agent has the conversation
    and can resolve "there" or "that city" properly.
    """
    folded = f" {fold(query)} "
    matches = [name for name in known if f" {fold(name)} " in folded]
    return max(matches, key=len) if matches else None


def _resolve_destination(args: argparse.Namespace, index: TravelIndex) -> str:
    """Return the destination to filter on, or "" to search every destination."""
    if args.destination:
        return args.destination
    if getattr(args, "all_destinations", False):
        return ""

    known = index.destinations()
    inferred = _infer_destination(args.query, known) if getattr(args, "query", None) else None
    if inferred:
        print(f"destination: {inferred} (inferred from the query)")
        return inferred
    if len(known) == 1:
        print(f"destination: {known[0]} (the only one in this index)")
        return known[0]
    raise StoreError(
        "No --destination given, and the query does not name one.\n"
        f"Indexed destinations: {', '.join(sorted(known)) or '(none)'}\n"
        "Pass --destination NAME, or --all-destinations to search every one "
        "(results will mix cities)."
    )


def _embedding_provider(manifest: dict | None, configured: str) -> str:
    if manifest:
        declared = str(manifest.get("embedding_provider") or "").strip().lower()
        if declared:
            return declared
        model = str(manifest.get("embedding_model") or "")
        if model.startswith("amazon.titan-embed-"):
            return "titan"
    return configured


def _embedder(args: argparse.Namespace, manifest: dict | None = None) -> Embedder:
    provider = _embedding_provider(manifest, args.embedding_provider)
    if provider == "titan":
        model = str((manifest or {}).get("embedding_model") or args.titan_model)
        dimensions = int(
            (manifest or {}).get("embedding_dimensions") or args.titan_dimensions
        )
        return BedrockTitanEmbedder(
            region=args.titan_region,
            model=model,
            dimensions=dimensions,
            workers=args.titan_workers,
        )
    if provider != "ollama":
        raise EmbeddingError(f"Unsupported embedding provider: {provider}")
    model = str((manifest or {}).get("embedding_model") or args.embed_model)
    return OllamaEmbedder(
        host=args.ollama_host,
        model=model,
        batch_size=args.embed_batch_size,
    )


def _retriever(args: argparse.Namespace) -> TravelRetriever:
    """Open the index and rebuild the exact BM25 encoder it was written with."""
    if not args.index_manifest.exists():
        raise IngestError(
            f"No index manifest at {args.index_manifest}.\n"
            "Build an index first:\n"
            "  scripts/opensource_rag.py index --prepared <prepared-corpus-dir>"
        )
    manifest = load_index_manifest(args.index_manifest)
    manifest_collection = str(manifest.get("collection") or "").strip()
    if manifest_collection and manifest_collection != args.collection:
        raise IngestError(
            f"Index manifest {args.index_manifest} belongs to collection "
            f"{manifest_collection!r}, but --collection is {args.collection!r}.\n"
            "Pass the matching --collection and --index-manifest together."
        )
    bm25 = BM25Index.from_manifest(manifest["bm25"])
    embedder = _embedder(args, manifest)
    index = TravelIndex.connect(args.qdrant_url, args.collection)
    if not index.exists():
        raise StoreError(
            f"Collection {args.collection!r} does not exist at {args.qdrant_url}.\n"
            "Start Qdrant and build an index:\n"
            "  (cd opensource/compose && docker compose up -d qdrant ollama)\n"
            "  scripts/opensource_rag.py index --prepared <prepared-corpus-dir>"
        )
    return TravelRetriever(
        index=index,
        embedder=embedder,
        bm25=bm25,
        config=RetrievalConfig(
            corpus_version=", ".join(manifest.get("corpus_versions") or []),
            reranker_url="" if args.no_rerank else args.reranker_url,
            rerank_candidates=args.rerank_candidates,
            category_filter_enabled=not args.no_category_filter,
            retrieval_mode=args.retrieval_mode,
        ),
    )


def command_index(args: argparse.Namespace) -> int:
    result = index_prepared(
        args.prepared,
        collection=args.collection,
        qdrant_url=args.qdrant_url,
        embedder=_embedder(args),
        manifest_path=args.index_manifest,
        recreate=args.recreate,
        resume=not args.no_resume,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        progress=print,
    )
    print(
        json.dumps({k: v for k, v in result.items() if k != "bm25"}, indent=2, ensure_ascii=False)
    )
    return 0


def command_import_s3(args: argparse.Namespace) -> int:
    """Materialize the exact AWS Knowledge Base chunks for local indexing."""
    result = import_bedrock_s3_corpus(
        bucket=args.bucket,
        prefix=args.prefix,
        corpus_version=args.corpus_version,
        output=args.output,
        region=args.region,
        workers=args.workers,
        progress=print,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def command_search(args: argparse.Namespace) -> int:
    retriever = _retriever(args)
    destination = _resolve_destination(args, retriever.index)
    if not destination:
        print("destination: ALL (no filter - results mix cities)")
    response = retriever.search(
        args.query,
        destination,
        args.country,
        top_k=args.top_k,
        require_destination=False,
    )
    retrieval = response["retrieval"]
    print(
        f"intent={retrieval['intent']} ranking={retrieval['ranking']} "
        f"candidates={retrieval['candidate_count']} fallback={retrieval['route_fallback']} "
        f"{retrieval['latency_ms']}ms"
    )
    print(f"query -> {retrieval['retrieval_query']!r}")
    if retrieval["route_fallback"]:
        print(
            f"WARNING: no {retrieval['intent']!r} listings for this destination. "
            "The category filter was dropped and these results are off-category; "
            "rows marked [!] do not match what you asked for."
        )
    for position, result in enumerate(response["results"], start=1):
        marker = "*" if result["meets_score_target"] else " "
        if result["meets_score_target"] is None:
            marker = " "
        if not result.get("matches_intent", True):
            marker = "!"
        rerank = result.get("rerank_score")
        rerank_text = f" rerank={rerank:.4f}" if rerank is not None else ""
        label = {"cosine": "cos", "bm25": "bm25"}[result["score_type"]]
        fusion = f" rrf={result['fusion_score']:.4f}" if result["fusion_score"] else ""
        print(
            f"{marker}{position}. {result['entity_name'] or result['title']} "
            f"[{result['category_key']}] {label}={result['score']:.4f}"
            f"{fusion}{rerank_text}"
        )
        if args.show_text:
            print(f"     {result['text'][:300]}")
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    payload = json.loads(args.queries.read_text(encoding="utf-8"))
    cases = payload["queries"]
    retriever = _retriever(args)

    rows = []
    for case in cases:
        response = retriever.search(
            case["query"],
            case["destination"],
            case.get("country", ""),
            top_k=args.top_k,
        )
        results = response["results"]
        rank = next(
            (position for position, r in enumerate(results, start=1) if _matches(case, r)),
            None,
        )
        top = results[0] if results else {}
        entity_scores = [r["score"] for r in results if r.get("entity_name")]
        rows.append(
            {
                "query": case["query"],
                "category": case.get("category_key"),
                "rank": rank,
                "top_entity": top.get("entity_name"),
                "top_score": float(top.get("score") or 0.0),
                "best_entity_score": max(entity_scores) if entity_scores else None,
                "latency_ms": response["retrieval"]["latency_ms"],
            }
        )

    top_scores = [row["top_score"] for row in rows]
    entity_scores = [
        row["best_entity_score"] for row in rows if row["best_entity_score"] is not None
    ]
    score_type = "bm25" if args.retrieval_mode == "bm25" else "cosine"
    latencies = [row["latency_ms"] for row in rows]
    ordered_latencies = sorted(latencies)

    def percentile(values: list[int], fraction: float) -> int:
        return values[round((len(values) - 1) * fraction)]

    summary = {
        "dataset": payload.get("name"),
        "corpus_version": retriever.config.corpus_version,
        "search_type": {
            "hybrid": "HYBRID",
            "semantic": "SEMANTIC",
            "bm25": "BM25",
        }[args.retrieval_mode],
        "rerank": not args.no_rerank and bool(args.reranker_url),
        "comparison_contract": {
            "query_planner": "rag.opensource.query",
            "category_filter": not args.no_category_filter,
            "rerank": not args.no_rerank and bool(args.reranker_url),
            "deduplicate_entities": True,
            "candidate_count": planner.candidate_count(
                args.top_k,
                rerank_enabled=not args.no_rerank and bool(args.reranker_url),
                rerank_candidates=args.rerank_candidates,
            ),
            "top_k": args.top_k,
        },
        "queries": len(rows),
        f"hit_at_{args.top_k}": sum(row["rank"] is not None for row in rows) / len(rows),
        "mrr": statistics.fmean((1 / row["rank"] if row["rank"] else 0) for row in rows),
        "top1_named_entity_rate": sum(bool(row["top_entity"]) for row in rows) / len(rows),
        "top1_score_type": score_type,
        "top1_score_mean": statistics.fmean(top_scores),
        "top1_score_ge_0_60": (
            sum(score >= 0.60 for score in top_scores) / len(rows)
            if score_type == "cosine"
            else None
        ),
        "best_named_entity_score_mean": (
            statistics.fmean(entity_scores) if entity_scores else None
        ),
        "latency_ms_mean": statistics.fmean(latencies),
        "latency_ms_p50": percentile(ordered_latencies, 0.50),
        "latency_ms_p95": percentile(ordered_latencies, 0.95),
    }
    for row in rows:
        print(
            f"  rank={str(row['rank'] or '-'):>3}  "
            f"{score_type}={row['top_score']:.4f}  "
            f"{row['latency_ms']:>5}ms  {row['query'][:60]}"
        )
    print(json.dumps(summary, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL", DEFAULT_QDRANT_URL))
    parser.add_argument(
        "--collection", default=os.environ.get("QDRANT_COLLECTION", DEFAULT_COLLECTION)
    )
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST))
    parser.add_argument(
        "--embedding-provider",
        choices=("ollama", "titan"),
        default=os.environ.get("RAG_EMBEDDING_PROVIDER", "ollama"),
        help="dense embedding backend used while indexing; query-time uses the manifest",
    )
    parser.add_argument(
        "--embed-model", default=os.environ.get("OLLAMA_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL)
    )
    parser.add_argument(
        "--titan-model",
        default=os.environ.get("TITAN_EMBED_MODEL_ID", DEFAULT_TITAN_MODEL),
    )
    parser.add_argument(
        "--titan-dimensions",
        type=int,
        choices=SUPPORTED_TITAN_DIMENSIONS,
        default=int(os.environ.get("TITAN_EMBED_DIMENSIONS", DEFAULT_TITAN_DIMENSIONS)),
    )
    parser.add_argument(
        "--titan-region",
        default=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")),
    )
    parser.add_argument(
        "--titan-workers",
        type=int,
        default=int(os.environ.get("TITAN_EMBED_WORKERS", "8")),
    )
    parser.add_argument(
        "--index-manifest",
        type=Path,
        default=Path(os.environ.get("RAG_INDEX_MANIFEST", DEFAULT_INDEX_MANIFEST)),
        help=f"BM25 table and index provenance (default: {DEFAULT_INDEX_MANIFEST})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="embed prepared corpora into Qdrant")
    # Every corpus sharing a collection must be indexed in one run: BM25
    # weights depend on corpus-wide document frequencies, and a later run over
    # a subset would overwrite the table the earlier points were written with.
    index_parser.add_argument(
        "--prepared",
        type=Path,
        nargs="+",
        required=True,
        metavar="DIR",
        help="prepared corpus directories; pass all of them in one run",
    )
    index_parser.add_argument("--batch-size", type=int, default=256)
    index_parser.add_argument("--embed-batch-size", type=int, default=16)
    index_parser.add_argument("--recreate", action="store_true", help="drop and rebuild")
    index_parser.add_argument("--no-resume", action="store_true", help="re-embed existing chunks")
    index_parser.add_argument("--dry-run", action="store_true", help="validate only")
    index_parser.set_defaults(func=command_index)

    import_parser = subparsers.add_parser(
        "import-s3",
        help="download the exact Bedrock Knowledge Base chunks for local indexing",
    )
    import_parser.add_argument("--bucket", required=True)
    import_parser.add_argument("--prefix", default="wikivoyage")
    import_parser.add_argument("--corpus-version", required=True)
    import_parser.add_argument("--output", required=True, type=Path)
    import_parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-central-1"))
    import_parser.add_argument("--workers", type=int, default=8)
    import_parser.set_defaults(func=command_import_s3)

    search_parser = subparsers.add_parser("search", help="query the live index")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument(
        "--destination",
        default="",
        help="destination to filter on; inferred from the query when omitted",
    )
    search_parser.add_argument(
        "--all-destinations",
        action="store_true",
        help="search every destination (results will mix cities)",
    )
    search_parser.add_argument("--country", default="")
    search_parser.add_argument("--top-k", type=int, default=5)
    search_parser.add_argument("--show-text", action="store_true")
    search_parser.set_defaults(func=command_search)

    evaluate_parser = subparsers.add_parser("evaluate", help="score the index on a query set")
    evaluate_parser.add_argument("--queries", type=Path, required=True)
    evaluate_parser.add_argument("--top-k", type=int, default=5)
    evaluate_parser.set_defaults(func=command_evaluate)

    for retrieval_parser in (search_parser, evaluate_parser):
        retrieval_parser.add_argument("--reranker-url", default=os.environ.get("RERANKER_URL", ""))
        retrieval_parser.add_argument("--rerank-candidates", type=int, default=20)
        retrieval_parser.add_argument("--no-rerank", action="store_true")
        retrieval_parser.add_argument("--no-category-filter", action="store_true")
        retrieval_parser.add_argument(
            "--retrieval-mode",
            choices=("hybrid", "semantic", "bm25"),
            default="hybrid",
            help="retrieval strategy to evaluate; 'bm25' needs no embedding "
            "backend, 'semantic' uses only embeddings, and 'hybrid' fuses both",
        )
        retrieval_parser.add_argument("--embed-batch-size", type=int, default=16)

    return parser


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except (IngestError, StoreError, EmbeddingError) as exc:
        raise SystemExit(str(exc)) from exc
    except ResponseHandlingException as exc:
        # An unreachable Qdrant is the most common first-run failure; a raw
        # client traceback tells the user nothing about what to do next.
        raise SystemExit(
            f"Cannot reach Qdrant at {args.qdrant_url}: {exc}\n"
            "Start it with:\n"
            "  (cd opensource/compose && docker compose up -d qdrant ollama)"
        ) from exc


if __name__ == "__main__":
    raise SystemExit(main())
