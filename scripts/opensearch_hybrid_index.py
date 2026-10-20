#!/usr/bin/env python3
"""Create or inspect the OpenSearch Serverless index used by hybrid travel RAG."""

from __future__ import annotations

import argparse
import json
from typing import Any

import boto3
from botocore.exceptions import ClientError

VECTOR_FIELD = "bedrock-knowledge-base-default-vector"
TEXT_FIELD = "AMAZON_BEDROCK_TEXT_CHUNK"
METADATA_FIELD = "AMAZON_BEDROCK_METADATA"
DEFAULT_SPACE_TYPE = "cosinesimil"


def _collection(client, collection_name: str) -> dict[str, Any]:
    details = client.batch_get_collection(names=[collection_name]).get("collectionDetails") or []
    if not details:
        raise RuntimeError(f"OpenSearch Serverless collection does not exist: {collection_name}")
    collection = details[0]
    if collection.get("status") != "ACTIVE":
        raise RuntimeError(
            f"OpenSearch Serverless collection is not ACTIVE: {collection.get('status')}"
        )
    if not collection.get("id") or not collection.get("collectionEndpoint"):
        raise RuntimeError("OpenSearch Serverless collection has no endpoint")
    return collection


def _mapping(
    dimensions: int, space_type: str = DEFAULT_SPACE_TYPE
) -> dict[str, Any]:
    return {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                VECTOR_FIELD: {
                    "type": "knn_vector",
                    "dimension": dimensions,
                    "space_type": space_type,
                    "method": {
                        "name": "hnsw",
                        "engine": "faiss",
                        "parameters": {"ef_construction": 128, "m": 24},
                    },
                },
                TEXT_FIELD: {"type": "text", "index": True},
                METADATA_FIELD: {"type": "text", "index": False},
            }
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "describe", "delete"))
    parser.add_argument("--collection-name", required=True)
    parser.add_argument("--index-name", required=True)
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--dimensions", type=int, default=512)
    parser.add_argument(
        "--space-type",
        choices=("cosinesimil", "l2", "innerproduct"),
        default=DEFAULT_SPACE_TYPE,
        help="Vector similarity metric used when creating the index (default: cosinesimil)",
    )
    args = parser.parse_args()
    if args.dimensions <= 0:
        parser.error("--dimensions must be positive")

    session = boto3.Session(region_name=args.region)
    client = session.client("opensearchserverless")
    collection = _collection(client, args.collection_name)
    existing: dict[str, Any] | None = None
    try:
        existing = client.get_index(id=collection["id"], indexName=args.index_name)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
    if args.command == "describe":
        if existing is None:
            raise RuntimeError(f"OpenSearch Serverless index does not exist: {args.index_name}")
        print(json.dumps(existing.get("indexSchema", {}), indent=2))
        return 0
    if args.command == "delete":
        if existing is None:
            raise RuntimeError(f"OpenSearch Serverless index does not exist: {args.index_name}")
        client.delete_index(id=collection["id"], indexName=args.index_name)
        print(json.dumps({"deleted": True, "index": args.index_name}))
        return 0
    if existing is not None:
        print(
            json.dumps(
                {
                    "created": False,
                    "endpoint": collection["collectionEndpoint"],
                    "index": args.index_name,
                }
            )
        )
        return 0
    response = client.create_index(
        id=collection["id"],
        indexName=args.index_name,
        indexSchema=_mapping(args.dimensions, args.space_type),
    )
    print(
        json.dumps(
            {
                "created": True,
                "endpoint": collection["collectionEndpoint"],
                "index": args.index_name,
                "response": response.get("indexSchema", {}),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
