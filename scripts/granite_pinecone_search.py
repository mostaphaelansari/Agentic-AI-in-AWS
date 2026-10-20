#!/usr/bin/env python3
"""Copy Granite vectors from Qdrant to Pinecone and search them directly.

This is an evaluation utility, not the Bedrock Knowledge Base runtime path.
Bedrock KB would embed queries with Titan, so it cannot provide a controlled
Granite-on-Qdrant versus Granite-on-Pinecone comparison.

The existing evaluation Pinecone index is 1024-dimensional while Granite is
768-dimensional. ``--pinecone-dim 1024`` appends zeros to every document and
query vector. Zero-padding leaves the dot product and both vector norms
unchanged, and therefore preserves cosine similarity exactly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.opensource import query as planner  # noqa: E402
from rag.opensource.embeddings import (  # noqa: E402
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_HOST,
    OllamaEmbedder,
)
from rag.opensource.store import DENSE_VECTOR  # noqa: E402

DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_COLLECTION = "travel-rag-eval"
DEFAULT_MANIFEST = ROOT / "opensource" / "compose" / "data" / "rag" / "index-manifest.json"
DEFAULT_NAMESPACE = "wikivoyage-eval-v1-granite-768-padded"
PINECONE_API_VERSION = "2026-04"
METADATA_FIELDS = (
    "chunk_id",
    "title",
    "destination_key",
    "category_key",
    "document_type",
    "entity_name",
    "section",
    "source_url",
    "corpus_version",
)


class ExperimentError(RuntimeError):
    """Raised when the controlled comparison contract is not satisfied."""


def _manifest(path: Path, collection: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"Cannot read index manifest {path}: {exc}") from exc
    if payload.get("collection") != collection:
        raise ExperimentError(
            f"Manifest belongs to {payload.get('collection')!r}, not {collection!r}"
        )
    model = str(payload.get("embedding_model") or "")
    if "granite" not in model.casefold():
        raise ExperimentError(f"Manifest embedding model is not Granite: {model!r}")
    return payload


def _pad(vector: list[float], target_dimension: int) -> list[float]:
    if len(vector) > target_dimension:
        raise ExperimentError(
            f"Cannot fit a {len(vector)}-dimension vector into {target_dimension} dimensions"
        )
    return vector + [0.0] * (target_dimension - len(vector))


def _host(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ExperimentError("Pass --pinecone-host or set PINECONE_INDEX_HOST")
    return value if "://" in value else f"https://{value}"


def _pinecone_request(
    host: str,
    api_key: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{_host(host)}{path}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Api-Key": api_key,
            "Content-Type": "application/json",
            "X-Pinecone-Api-Version": PINECONE_API_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ExperimentError(f"Pinecone {path} failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise ExperimentError(f"Pinecone {path} failed: {exc}") from exc
    return json.loads(raw.decode("utf-8")) if raw else {}


def _api_key() -> str:
    value = os.environ.get("PINECONE_API_KEY", "").strip()
    if not value:
        raise ExperimentError("Set PINECONE_API_KEY before running this command")
    return value


def _metadata(payload: dict[str, Any]) -> dict[str, str | int | float | bool | list[str]]:
    result: dict[str, str | int | float | bool | list[str]] = {}
    for field in METADATA_FIELDS:
        value = payload.get(field)
        if isinstance(value, (str, int, float, bool)):
            result[field] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            result[field] = value
    return result


def sync(args: argparse.Namespace) -> int:
    manifest = _manifest(args.index_manifest, args.collection)
    native_dimension = int(manifest.get("embedding_dimensions") or 0)
    if native_dimension <= 0:
        raise ExperimentError("Manifest has no valid embedding_dimensions")

    client = QdrantClient(url=args.qdrant_url, timeout=60)
    offset = None
    batch: list[dict[str, Any]] = []
    total = 0

    def flush() -> None:
        nonlocal total
        if not batch:
            return
        response = _pinecone_request(
            args.pinecone_host,
            _api_key(),
            "/vectors/upsert",
            {"namespace": args.namespace, "vectors": batch},
        )
        total += int(response.get("upsertedCount") or len(batch))
        print(f"upserted={total}")
        batch.clear()

    while True:
        points, offset = client.scroll(
            args.collection,
            limit=args.scroll_batch,
            offset=offset,
            with_payload=True,
            with_vectors=[DENSE_VECTOR],
        )
        for point in points:
            vectors = point.vector if isinstance(point.vector, dict) else {}
            vector = list(vectors.get(DENSE_VECTOR) or [])
            if len(vector) != native_dimension:
                raise ExperimentError(
                    f"Qdrant point {point.id} has dimension {len(vector)}; "
                    f"manifest declares {native_dimension}"
                )
            payload = dict(point.payload or {})
            vector_id = str(payload.get("chunk_id") or point.id)
            batch.append(
                {
                    "id": vector_id,
                    "values": _pad([float(value) for value in vector], args.pinecone_dim),
                    "metadata": _metadata(payload),
                }
            )
            if len(batch) >= args.upsert_batch:
                flush()
        if offset is None:
            break
    flush()
    print(
        f"namespace={args.namespace} model={manifest.get('embedding_model')} "
        f"dimensions={native_dimension}->{args.pinecone_dim} total={total}"
    )
    return 0


def _filter(plan: dict[str, Any]) -> dict[str, Any] | None:
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


def _query(
    args: argparse.Namespace,
    vector: list[float],
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "namespace": args.namespace,
        "topK": planner.candidate_count(args.top_k, rerank_enabled=False),
        "vector": _pad(vector, args.pinecone_dim),
        "includeValues": False,
        "includeMetadata": True,
    }
    query_filter = _filter(plan)
    if query_filter:
        payload["filter"] = query_filter
    response = _pinecone_request(
        args.pinecone_host,
        _api_key(),
        "/query",
        payload,
    )
    return [
        {
            "id": match.get("id"),
            "score": float(match.get("score") or 0.0),
            "payload": dict(match.get("metadata") or {}),
        }
        for match in response.get("matches") or []
    ]


def search(args: argparse.Namespace) -> int:
    manifest = _manifest(args.index_manifest, args.collection)
    native_dimension = int(manifest.get("embedding_dimensions") or 0)
    embedder = OllamaEmbedder(
        host=args.ollama_host,
        model=str(manifest.get("embedding_model") or DEFAULT_EMBED_MODEL),
        batch_size=1,
    )
    plan = planner.plan(
        args.query,
        args.destination,
        args.country,
        category_filter_enabled=not args.no_category_filter,
    )
    started = time.monotonic()
    vector = embedder.embed_one(plan["retrieval_query"])
    if len(vector) != native_dimension:
        raise ExperimentError(
            f"Granite returned {len(vector)} dimensions; manifest declares {native_dimension}"
        )
    matches = _query(args, vector, plan)
    fallback = False
    if not matches and plan["intent"]:
        fallback = True
        fallback_plan = dict(plan)
        fallback_plan["retrieval_query"] = planner.focus_query(
            args.query, args.destination, args.country
        )
        fallback_plan["category_filters"] = ()
        fallback_plan["section_filters"] = ()
        vector = embedder.embed_one(fallback_plan["retrieval_query"])
        matches = _query(args, vector, fallback_plan)
        plan = fallback_plan
    results = planner.deduplicate_entities(matches)[: args.top_k]
    elapsed_ms = int((time.monotonic() - started) * 1000)

    rows = [
        {
            "rank": rank,
            "museum": str(result["payload"].get("entity_name") or result["payload"].get("title") or ""),
            "score": result["score"],
            "chunk_id": str(result["payload"].get("chunk_id") or result["id"] or ""),
        }
        for rank, result in enumerate(results, start=1)
    ]
    diagnostics = {
        "intent": plan["intent"],
        "ranking": "GRANITE_PINECONE_COSINE",
        "candidates": len(matches),
        "fallback": fallback,
        "latency_ms": elapsed_ms,
        "retrieval_query": plan["retrieval_query"],
        "native_dimensions": native_dimension,
        "pinecone_dimensions": args.pinecone_dim,
        "namespace": args.namespace,
    }
    if args.output == "json":
        print(json.dumps({"retrieval": diagnostics, "results": rows}, indent=2, ensure_ascii=False))
        return 0

    print(
        f"intent={diagnostics['intent']} ranking={diagnostics['ranking']} "
        f"candidates={diagnostics['candidates']} fallback={fallback} {elapsed_ms}ms"
    )
    print(f"query -> {plan['retrieval_query']!r}")
    print(f"dimensions={native_dimension}->{args.pinecone_dim} namespace={args.namespace}")
    for row in rows:
        print(f"{row['rank']}. {row['museum']} cos={row['score']:.4f} chunk={row['chunk_id']}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--pinecone-host",
        default=os.environ.get("PINECONE_INDEX_HOST", ""),
        help="Pinecone data-plane index host (or PINECONE_INDEX_HOST)",
    )
    result.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    result.add_argument("--pinecone-dim", type=int, default=1024)
    result.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    result.add_argument("--collection", default=DEFAULT_COLLECTION)
    result.add_argument("--index-manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = result.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="copy Granite Qdrant vectors to Pinecone")
    sync_parser.add_argument("--scroll-batch", type=int, default=256)
    sync_parser.add_argument("--upsert-batch", type=int, default=50)
    sync_parser.set_defaults(func=sync)

    search_parser = subparsers.add_parser("search", help="query Granite vectors in Pinecone")
    search_parser.add_argument("--ollama-host", default=DEFAULT_OLLAMA_HOST)
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
    if args.pinecone_dim <= 0:
        raise SystemExit("--pinecone-dim must be positive")
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExperimentError as exc:
        raise SystemExit(str(exc)) from exc
