#!/usr/bin/env python3
"""Run the deployed travel-RAG retrieval path against a local prepared corpus.

This exists so a corpus can be judged before it is ingested. It reuses the real
Lambda helpers for intent routing, filter construction, deduplication, and
reranking, and replaces only the two pieces that live in AWS:

  * Pinecone approximate search becomes exact cosine over locally cached vectors
  * the Bedrock Knowledge Base metadata filter is interpreted in process

Embeddings still come from the same Bedrock model the Knowledge Base uses
(Titan Text v2 at 512 dimensions, normalized), so scores are comparable with
`aws bedrock-agent-runtime retrieve`. Exact search makes local results a slight
upper bound on what Pinecone returns.

Examples:
  uv run python scripts/rag_local_search.py embed \
      --prepared ~/wikivoyage-corpus/wikivoyage-pilot-v3 \
      --cache ~/wikivoyage-corpus/v3-vectors.npz

  uv run python scripts/rag_local_search.py search \
      --cache ~/wikivoyage-corpus/v3-vectors.npz \
      --destination Paris --query "museums and art galleries" \
      --rerank cohere.rerank-v3-5:0

  uv run python scripts/rag_local_search.py evaluate \
      --cache ~/wikivoyage-corpus/v3-vectors.npz \
      --queries evaluations/datasets/travel_rag_queries.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "infrastructure" / "gateway"))

import travel_rag_lambda as rag  # noqa: E402

EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS = 512
MAX_EMBED_CHARS = 8_000
DEFAULT_REGION = "eu-central-1"


def _bedrock_runtime(region: str):
    import boto3

    return boto3.client("bedrock-runtime", region_name=region)


def _embed_one(client, text: str) -> list[float]:
    """Embed one string, retrying the throttling the Bedrock endpoint applies."""
    body = json.dumps(
        {
            "inputText": text[:MAX_EMBED_CHARS],
            "dimensions": EMBEDDING_DIMENSIONS,
            "normalize": True,
        }
    )
    last: Exception | None = None
    for attempt in range(6):
        try:
            response = client.invoke_model(modelId=EMBEDDING_MODEL, body=body)
            return json.loads(response["body"].read())["embedding"]
        except Exception as error:  # noqa: BLE001 - retried below, re-raised at the end
            last = error
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Embedding failed after retries: {last}")


def _load_chunks(prepared: Path, destinations: set[str] | None) -> list[dict]:
    path = prepared / "chunks.jsonl"
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if destinations:
        records = [r for r in records if r["metadata"]["destination_key"] in destinations]
    if not records:
        raise SystemExit(f"No chunks selected from {path}")
    return records


def _embed(args: argparse.Namespace) -> None:
    prepared = Path(args.prepared).expanduser()
    cache = Path(args.cache).expanduser()
    destinations = (
        {rag._normalize_filter_value(d) for d in args.destinations.split(",")}
        if args.destinations
        else None
    )
    records = _load_chunks(prepared, destinations)
    print(f"embedding {len(records)} chunks with {EMBEDDING_MODEL} @ {EMBEDDING_DIMENSIONS}d")

    client = _bedrock_runtime(args.region)
    started = time.time()
    done = 0

    def run(record: dict) -> list[float]:
        nonlocal done
        vector = _embed_one(client, record["embedding_text"])
        done += 1
        if done % 2000 == 0:
            rate = done / max(time.time() - started, 1e-9)
            print(f"  {done}/{len(records)}  {rate:.0f}/s", flush=True)
        return vector

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        vectors = list(pool.map(run, records))

    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        cache,
        vectors=np.asarray(vectors, dtype=np.float32),
        metadata=np.asarray([json.dumps(r["metadata"]) for r in records], dtype=object),
        texts=np.asarray([r["embedding_text"] for r in records], dtype=object),
    )
    elapsed = time.time() - started
    approximate_tokens = sum(len(r["embedding_text"]) for r in records) / 4
    print(
        f"wrote {cache} in {elapsed:.0f}s "
        f"(~{approximate_tokens / 1e6:.2f}M tokens, ~${approximate_tokens / 1e6 * 0.02:.2f})"
    )


class LocalIndex:
    """Exact cosine search over cached vectors with Bedrock filter semantics."""

    def __init__(self, cache: Path):
        loaded = np.load(cache, allow_pickle=True)
        self.vectors: np.ndarray = loaded["vectors"]
        self.metadata: list[dict] = [json.loads(m) for m in loaded["metadata"]]
        self.texts: list[str] = list(loaded["texts"])

    @staticmethod
    def _matches(metadata: dict, criteria: dict) -> bool:
        """Interpret the same filter dict the Lambda sends to Bedrock."""
        if "andAll" in criteria:
            return all(LocalIndex._matches(metadata, c) for c in criteria["andAll"])
        if "orAll" in criteria:
            return any(LocalIndex._matches(metadata, c) for c in criteria["orAll"])
        if "equals" in criteria:
            clause = criteria["equals"]
            return metadata.get(clause["key"]) == clause["value"]
        raise ValueError(f"Unsupported filter operator: {sorted(criteria)}")

    def search(self, query_vector: np.ndarray, criteria: dict, limit: int) -> list[dict]:
        keep = np.array(
            [i for i, m in enumerate(self.metadata) if self._matches(m, criteria)],
            dtype=np.int64,
        )
        if keep.size == 0:
            return []
        scores = self.vectors[keep] @ query_vector
        order = np.argsort(-scores)[:limit]
        return [
            {
                "metadata": self.metadata[keep[i]],
                "content": {"text": self.texts[keep[i]]},
                "score": float(scores[i]),
            }
            for i in order
        ]


def _retrieve_locally(
    index: LocalIndex, client, query: str, destination: str, top_k: int, country: str = ""
) -> tuple[list[dict], dict]:
    """Mirror lambda_handler's routing, filtering, dedup, and ranking."""
    route = rag._intent_route(query)
    if route:
        intent, category_context, sections = route
        focused = rag._enrich_query(query, destination, country, category_context)
    else:
        intent, sections = None, ()
        focused = rag._focus_query(query, destination, country)

    destination_key = rag._normalize_filter_value(destination)
    category_keys: tuple[str, ...] = ()
    if intent and os.environ.get("RAG_CATEGORY_FILTER_KEY"):
        category_keys = (
            ("attractions", "museums", "landmarks") if intent == "attractions" else (intent,)
        )

    criteria = rag._retrieval_filter(destination_key, sections, category_keys)
    candidate_count = rag._candidate_count(top_k)
    query_vector = np.asarray(_embed_one(client, focused), dtype=np.float32)
    candidates = index.search(query_vector, criteria, candidate_count)

    route_fallback = False
    if intent and not candidates:
        route_fallback = True
        focused = rag._focus_query(query, destination, country)
        query_vector = np.asarray(_embed_one(client, focused), dtype=np.float32)
        candidates = index.search(
            query_vector, rag._retrieval_filter(destination_key, (), ()), candidate_count
        )

    deduped = rag._deduplicate_entities(candidates)
    try:
        ranked, reranked = rag._rerank(query, deduped, top_k)
        ranking = "BEDROCK_RERANK" if reranked else "SEMANTIC"
    except Exception as error:  # noqa: BLE001 - mirrors the Lambda's fallback
        print(f"  ! rerank failed ({error}); using semantic order", file=sys.stderr)
        ranked, ranking = deduped[:top_k], "SEMANTIC_FALLBACK"

    return ranked, {
        "intent": intent,
        "focused_query": focused,
        "ranking": ranking,
        "route_fallback": route_fallback,
        "candidates": len(candidates),
        "deduped": len(deduped),
    }


def _configure_rerank(model_id: str | None, region: str) -> None:
    import boto3

    # The Lambda builds its client without a region because it inherits one from
    # the execution environment. Locally there may be no default region, so seed
    # the module global it caches rather than depending on the shell.
    rag._bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=region)
    if model_id:
        os.environ["BEDROCK_RERANKING_MODEL_ARN"] = (
            f"arn:aws:bedrock:{region}::foundation-model/{model_id}"
        )
    else:
        os.environ.pop("BEDROCK_RERANKING_MODEL_ARN", None)


def _search(args: argparse.Namespace) -> None:
    _configure_rerank(args.rerank, args.region)
    if args.category_filter:
        os.environ["RAG_CATEGORY_FILTER_KEY"] = "1"
    index = LocalIndex(Path(args.cache).expanduser())
    client = _bedrock_runtime(args.region)
    results, info = _retrieve_locally(
        index, client, args.query, args.destination, args.top_k, args.country
    )
    print(json.dumps(info, ensure_ascii=False))
    for position, item in enumerate(results, start=1):
        metadata = item["metadata"]
        score = item.get("_rerank_score")
        label = "rerank" if score is not None else "vector"
        score = score if score is not None else item["score"]
        print(
            f"{position}. [{label} {score:.4f}] "
            f"{metadata.get('entity_name') or metadata.get('section')}  "
            f"({metadata['title']})"
        )
        if args.show_text:
            print(f"     {item['content']['text'][:300]}...")


def _evaluate(args: argparse.Namespace) -> None:
    _configure_rerank(args.rerank, args.region)
    if args.category_filter:
        os.environ["RAG_CATEGORY_FILTER_KEY"] = "1"
    index = LocalIndex(Path(args.cache).expanduser())
    client = _bedrock_runtime(args.region)
    cases = json.loads(Path(args.queries).expanduser().read_text(encoding="utf-8"))["queries"]

    hits = 0
    reciprocal = 0.0
    rows: list[tuple[str, int | None, str]] = []
    for case in cases:
        results, _ = _retrieve_locally(
            index, client, case["query"], case["destination"], args.top_k
        )
        accepted = {rag._normalize_filter_value(name) for name in case.get("accept") or []}
        # Some questions ("where should I eat") have no single right answer: any
        # listing of the right kind is correct. Naming a handful of restaurants
        # would score the corpus on which ones the author happened to pick, so
        # those cases assert the category instead.
        accepted_category = case.get("accept_category")
        rank: int | None = None
        for position, item in enumerate(results, start=1):
            metadata = item.get("metadata") or {}
            entity = rag._normalize_filter_value(str(metadata.get("entity_name") or ""))
            if not entity:
                continue
            if any(a in entity or entity in a for a in accepted):
                rank = position
                break
            if accepted_category and metadata.get("category_key") == accepted_category:
                rank = position
                break
        if rank is not None:
            hits += 1
            reciprocal += 1 / rank
        top = results[0]["metadata"].get("entity_name") if results else "-"
        rows.append((case["query"], rank, top or "-"))

    total = len(cases)
    print(f"{'rank':>5}  {'query':<52} top-1 result")
    print("-" * 100)
    for query, rank, top in rows:
        print(f"{(rank if rank else '-'):>5}  {query[:52]:<52} {top}")
    print("-" * 100)
    print(
        f"hit@{args.top_k}: {hits}/{total} ({hits / total:.0%})   "
        f"MRR: {reciprocal / total:.3f}   "
        f"ranking: {'BEDROCK_RERANK' if args.rerank else 'SEMANTIC'}"
    )


def _probe(args: argparse.Namespace) -> None:
    """Show where a cosine score sits in the corpus, instead of reading it raw.

    A cosine of 0.38 looks weak until you know the corpus median for that query
    is 0.07 and the best chunk anywhere is 0.60. This prints the distribution so
    a score can be read as a position rather than a fraction of 1.0.
    """
    index = LocalIndex(Path(args.cache).expanduser())
    client = _bedrock_runtime(args.region)
    vectors, metadata, texts = index.vectors, index.metadata, index.texts

    query_vector = np.asarray(_embed_one(client, args.query), dtype=np.float32)
    scores = vectors @ query_vector
    print(f"query: {args.query!r}")
    print(f"corpus: {len(vectors):,} chunks\n")
    print("  cosine distribution over the whole corpus")
    for percentile in (50, 75, 99, 99.9):
        print(f"    p{percentile:<6} {np.percentile(scores, percentile): .4f}")
    print(f"    max     {scores.max(): .4f}")
    print(f"    mean    {scores.mean(): .4f}   std {scores.std():.4f}")

    print("\n  unrelated text, for the zero point")
    for junk in (
        "def parse(x): return json.loads(x)",
        "The mitochondrion is the powerhouse of the cell.",
    ):
        value = float(np.asarray(_embed_one(client, junk), dtype=np.float32) @ query_vector)
        print(f"    {value: .4f}  {junk[:52]}")

    order = np.argsort(-scores)[: args.top_k]
    print(f"\n  top {args.top_k} for this query")
    for position, i in enumerate(order, start=1):
        percentile = float((scores < scores[i]).mean() * 100)
        name = metadata[i].get("entity_name") or metadata[i].get("section")
        print(
            f"    {position}. {scores[i]: .4f}  p{percentile:.3f}  {name}  ({metadata[i]['title']})"
        )

    if not args.entity:
        return

    matches = [
        i
        for i, m in enumerate(metadata)
        if args.entity.casefold() in str(m.get("entity_name") or "").casefold()
    ]
    if not matches:
        raise SystemExit(f"No chunk has an entity_name containing {args.entity!r}")
    target = matches[0]
    document, document_vector = texts[target], vectors[target]
    print(f"\n  document pinned by --entity {args.entity!r}:")
    print("  " + "-" * 74)
    for line in document.splitlines():
        print(f"  {line}")
    print("  " + "-" * 74)

    def against(text: str) -> float:
        return float(np.asarray(_embed_one(client, text), dtype=np.float32) @ document_vector)

    words = document.split()
    sentences = document.split(". ")
    print("\n  cosine against that document as the query approaches it")
    ladder = [
        ("the whole chunk, verbatim", document),
        ("first half of it", " ".join(words[: len(words) // 2])),
        ("one sentence from the middle", sentences[1] if len(sentences) > 1 else document[:120]),
        ("your query", args.query),
    ]
    for label, text in ladder:
        print(f"    {against(text): .4f}  {label}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=DEFAULT_REGION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser(
        "probe", help="Show where a cosine score sits in the corpus distribution"
    )
    probe.add_argument("--cache", required=True)
    probe.add_argument("--query", required=True)
    probe.add_argument("--top-k", type=int, default=5)
    probe.add_argument("--entity", help="Pin one document by entity name and show the score ladder")
    probe.set_defaults(func=_probe)

    embed = subparsers.add_parser("embed", help="Embed a prepared corpus into a local cache")
    embed.add_argument("--prepared", required=True)
    embed.add_argument("--cache", required=True)
    embed.add_argument("--destinations", help="Comma-separated destination_key allowlist")
    embed.add_argument("--workers", type=int, default=16)
    embed.set_defaults(func=_embed)

    search = subparsers.add_parser("search", help="Run one query through the retrieval path")
    search.add_argument("--cache", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--destination", required=True)
    search.add_argument("--country", default="")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--rerank", help="Reranker model id, e.g. cohere.rerank-v3-5:0")
    search.add_argument("--category-filter", action="store_true")
    search.add_argument("--show-text", action="store_true")
    search.set_defaults(func=_search)

    evaluate = subparsers.add_parser("evaluate", help="Score a query set against the cache")
    evaluate.add_argument("--cache", required=True)
    evaluate.add_argument("--queries", required=True)
    evaluate.add_argument("--top-k", type=int, default=5)
    evaluate.add_argument("--rerank", help="Reranker model id, e.g. cohere.rerank-v3-5:0")
    evaluate.add_argument("--category-filter", action="store_true")
    evaluate.set_defaults(func=_evaluate)
    return parser


def main() -> int:
    args = _parser().parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
