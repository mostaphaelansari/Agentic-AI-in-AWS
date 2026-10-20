from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rag.opensource.embeddings import BedrockTitanEmbedder, EmbeddingError  # noqa: E402


class FakeBedrockRuntime:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def invoke_model(self, **kwargs):
        self.requests.append(kwargs)
        payload = json.loads(kwargs["body"])
        value = float(len(payload["inputText"]))
        return {
            "body": io.BytesIO(
                json.dumps({"embedding": [value] * payload["dimensions"]}).encode()
            )
        }


def test_titan_embedder_requests_normalized_1024_dimension_vectors() -> None:
    client = FakeBedrockRuntime()
    embedder = BedrockTitanEmbedder(
        client=client,
        dimensions=1024,
        workers=1,
    )

    vector = embedder.embed_one("Paris museums")

    assert len(vector) == 1024
    assert embedder.provider == "titan"
    assert embedder.dimension == 1024
    request = client.requests[0]
    assert request["modelId"] == "amazon.titan-embed-text-v2:0"
    assert json.loads(request["body"]) == {
        "inputText": "Paris museums",
        "dimensions": 1024,
        "normalize": True,
    }


def test_titan_embedder_preserves_batch_order() -> None:
    embedder = BedrockTitanEmbedder(
        client=FakeBedrockRuntime(),
        dimensions=256,
        workers=2,
    )

    vectors = embedder.embed(["a", "three"])

    assert [vector[0] for vector in vectors] == [1.0, 5.0]


def test_titan_embedder_rejects_unsupported_dimensions() -> None:
    with pytest.raises(EmbeddingError, match="256, 512, 1024"):
        BedrockTitanEmbedder(client=FakeBedrockRuntime(), dimensions=768)


def test_titan_embedder_rejects_an_empty_input_without_calling_bedrock() -> None:
    client = FakeBedrockRuntime()
    embedder = BedrockTitanEmbedder(client=client, max_attempts=1)

    with pytest.raises(EmbeddingError, match="must not be empty"):
        embedder.embed_one("  ")

    assert not client.requests
