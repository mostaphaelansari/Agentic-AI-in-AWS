#!/usr/bin/env python3
"""Embed a controlled corpus slice with Bedrock Cohere and search Pinecone.

This temporary evaluation path bypasses Bedrock Knowledge Bases so the
embedding model, corpus, Pinecone namespace, filters, and query text are all
explicit. It is intended to answer one question: how does Cohere's cosine
scale and ranking compare with Titan and Granite for the Paris museum query?
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.opensource import query as planner  # noqa: E402
from scripts.granite_pinecone_search import (  # noqa: E402
    ExperimentError,
    _api_key,
    _metadata,
    _pinecone_request,
)

DEFAULT_MODEL = "cohere.embed-multilingual-v3"
DEFAULT_REGION = "eu-central-1"
DEFAULT_DIMENSION = 1024
DEFAULT_NAMESPACE = "wikivoyage-eval-v1-cohere-multilingual-v3-paris-museums"
DEFAULT_PREPARED = Path("/tmp/wikivoyage-eval-v1-prepared")
MAX_COHERE_TEXT_CHARS = 2048
MAX_COHERE_BATCH = 96


class CohereBedrockEmbedder:
    """Small Bedrock Cohere v3 client with explicit retrieval input types."""

    def __init__(self, *, region: str, model: str, client: Any | None = None) -> None:
        self.region = region
        self.model = model
        self.client = client or boto3.client("bedrock-runtime", region_name=region)

    def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        if not texts:
            return []
        if input_type not in {"search_document", "search_query"}:
            raise ExperimentError(f"Unsupported Cohere input_type: {input_type}")
        if len(texts) > MAX_COHERE_BATCH:
            raise ExperimentError(
                f"Cohere accepts at most {MAX_COHERE_BATCH} texts per request"
            )
        for text in texts:
            if not text.strip():
                raise ExperimentError("Cohere input text must not be empty")
            if len(text) > MAX_COHERE_TEXT_CHARS:
                raise ExperimentError(
                    f"Cohere input has {len(text)} characters; maximum is "
                    f"{MAX_COHERE_TEXT_CHARS}"
                )
        response = self.client.invoke_model(
            modelId=self.model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "texts": texts,
                    "input_type": input_type,
                    "truncate": "NONE",
                    "embedding_types": ["float"],
                }
            ),
        )
        raw = response["body"].read()
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        embeddings = payload.get("embeddings")
        if isinstance(embeddings, dict):
            embeddings = embeddings.get("float")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise ExperimentError(
                f"Cohere returned {len(embeddings or [])} embeddings for {len(texts)} texts"
            )
        vectors = [[float(value) for value in vector] for vector in embeddings]
        dimensions = {len(vector) for vector in vectors}
        if dimensions != {DEFAULT_DIMENSION}:
            raise ExperimentError(
                f"Cohere returned dimensions {sorted(dimensions)}; expected {DEFAULT_DIMENSION}"
            )
        return vectors


def _records(prepared: Path, destination_key: str, category_key: str) -> list[dict[str, Any]]:
    manifest_path = prepared / "manifest.json"
    chunks_path = prepared / "chunks.jsonl"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records = [
            json.loads(line)
            for line in chunks_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"Cannot read prepared corpus {prepared}: {exc}") from exc
    expected = int(manifest.get("chunk_count") or 0)
    if len(records) != expected:
        raise ExperimentError(f"Prepared corpus has {len(records)} records; expected {expected}")
    selected = [
        record
        for record in records
        if str((record.get("metadata") or {}).get("destination_key") or "")
        == destination_key
        and str((record.get("metadata") or {}).get("category_key") or "") == category_key
    ]
    if not selected:
        raise ExperimentError(
            f"No prepared records match destination={destination_key!r}, "
            f"category={category_key!r}"
        )
    for record in selected:
        record["corpus_version"] = str(manifest.get("corpus_version") or "")
    return selected


def sync(args: argparse.Namespace) -> int:
    records = _records(args.prepared, args.destination_key, args.category_key)
    embedder = CohereBedrockEmbedder(region=args.region, model=args.model)
    total = 0
    for start in range(0, len(records), args.batch_size):
        batch = records[start : start + args.batch_size]
        texts = [str(record.get("embedding_text") or "") for record in batch]
        vectors = embedder.embed(texts, input_type="search_document")
        pinecone_vectors = []
        for record, vector in zip(batch, vectors, strict=True):
            payload = {
                **dict(record.get("metadata") or {}),
                "chunk_id": str(record["chunk_id"]),
                "corpus_version": str(record.get("corpus_version") or ""),
            }
            pinecone_vectors.append(
                {
                    "id": str(record["chunk_id"]),
                    "values": vector,
                    "metadata": _metadata(payload),
                }
            )
        response = _pinecone_request(
            args.pinecone_host,
            _api_key(),
            "/vectors/upsert",
            {"namespace": args.namespace, "vectors": pinecone_vectors},
        )
        total += int(response.get("upsertedCount") or len(pinecone_vectors))
        print(f"embedded_and_upserted={total}/{len(records)}")
    print(
        f"namespace={args.namespace} model={args.model} dimensions={DEFAULT_DIMENSION} "
        f"destination={args.destination_key} category={args.category_key} total={total}"
    )
    return 0


def _pinecone_filter(plan: dict[str, Any]) -> dict[str, Any] | None:
    conditions: list[dict[str, Any]] = []
    if plan["destination_key"]:
        conditions.append({"destination_key": {"$eq": plan["destination_key"]}})
    if plan["category_filters"]:
        conditions.append({"category_key": {"$in": list(plan["category_filters"])}})
    elif plan["section_filters"]:
        conditions.append({"section": {"$in": list(plan["section_filters"])}})
    if not conditions:
        return None
    return conditions[0] if len(conditions) == 1 else {"$and": conditions}


def search(args: argparse.Namespace) -> int:
    plan = planner.plan(
        args.query,
        args.destination,
        args.country,
        category_filter_enabled=not args.no_category_filter,
    )
    embedder = CohereBedrockEmbedder(region=args.region, model=args.model)
    started = time.monotonic()
    query_vector = embedder.embed(
        [plan["retrieval_query"]], input_type="search_query"
    )[0]
    request: dict[str, Any] = {
        "namespace": args.namespace,
        "topK": planner.candidate_count(args.top_k, rerank_enabled=False),
        "vector": query_vector,
        "includeValues": False,
        "includeMetadata": True,
    }
    query_filter = _pinecone_filter(plan)
    if query_filter:
        request["filter"] = query_filter
    response = _pinecone_request(
        args.pinecone_host,
        _api_key(),
        "/query",
        request,
    )
    matches = [
        {
            "id": match.get("id"),
            "score": float(match.get("score") or 0.0),
            "payload": dict(match.get("metadata") or {}),
        }
        for match in response.get("matches") or []
    ]
    results = planner.deduplicate_entities(matches)[: args.top_k]
    elapsed_ms = int((time.monotonic() - started) * 1000)
    rows = [
        {
            "rank": rank,
            "entity": str(
                result["payload"].get("entity_name")
                or result["payload"].get("title")
                or ""
            ),
            "score": result["score"],
            "chunk_id": str(result["payload"].get("chunk_id") or result["id"] or ""),
        }
        for rank, result in enumerate(results, start=1)
    ]
    diagnostics = {
        "intent": plan["intent"],
        "ranking": "COHERE_PINECONE_COSINE",
        "candidates": len(matches),
        "latency_ms": elapsed_ms,
        "retrieval_query": plan["retrieval_query"],
        "model": args.model,
        "dimensions": DEFAULT_DIMENSION,
        "namespace": args.namespace,
    }
    if args.output == "json":
        print(json.dumps({"retrieval": diagnostics, "results": rows}, indent=2, ensure_ascii=False))
        return 0
    print(
        f"intent={diagnostics['intent']} ranking={diagnostics['ranking']} "
        f"candidates={diagnostics['candidates']} {elapsed_ms}ms"
    )
    print(f"query -> {plan['retrieval_query']!r}")
    print(
        f"model={args.model} dimensions={DEFAULT_DIMENSION} namespace={args.namespace}"
    )
    for row in rows:
        print(f"{row['rank']}. {row['entity']} cos={row['score']:.4f} chunk={row['chunk_id']}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--pinecone-host",
        default=os.environ.get("PINECONE_INDEX_HOST", ""),
        help="Pinecone data-plane index host (or PINECONE_INDEX_HOST)",
    )
    result.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    result.add_argument("--region", default=DEFAULT_REGION)
    result.add_argument("--model", default=DEFAULT_MODEL)
    subparsers = result.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser(
        "sync", help="embed and upsert the controlled prepared-corpus slice"
    )
    sync_parser.add_argument("--prepared", type=Path, default=DEFAULT_PREPARED)
    sync_parser.add_argument("--destination-key", default="paris")
    sync_parser.add_argument("--category-key", default="museums")
    sync_parser.add_argument("--batch-size", type=int, default=32)
    sync_parser.set_defaults(func=sync)

    search_parser = subparsers.add_parser("search", help="query Cohere vectors in Pinecone")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--destination", required=True)
    search_parser.add_argument("--country", default="")
    search_parser.add_argument("--top-k", type=int, default=5)
    search_parser.add_argument("--no-category-filter", action="store_true")
    search_parser.add_argument("--output", choices=("table", "json"), default="table")
    search_parser.set_defaults(func=search)
    return result


def main() -> int:
    args = parser().parse_args()
    if hasattr(args, "batch_size") and not 1 <= args.batch_size <= MAX_COHERE_BATCH:
        raise SystemExit(f"--batch-size must be between 1 and {MAX_COHERE_BATCH}")
    if hasattr(args, "top_k") and not 1 <= args.top_k <= 10:
        raise SystemExit("--top-k must be between 1 and 10")
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExperimentError as exc:
        raise SystemExit(str(exc)) from exc
