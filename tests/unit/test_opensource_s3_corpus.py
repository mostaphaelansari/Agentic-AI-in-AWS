from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rag.ingest import bedrock_metadata  # noqa: E402
from rag.opensource.index import load_corpora  # noqa: E402
from rag.opensource.s3_corpus import (  # noqa: E402
    S3CorpusError,
    import_bedrock_s3_corpus,
)


class _Paginator:
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys

    def paginate(self, **_kwargs):
        midpoint = len(self.keys) // 2
        return (
            {"Contents": [{"Key": key} for key in self.keys[:midpoint]]},
            {"Contents": [{"Key": key} for key in self.keys[midpoint:]]},
        )


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get_object(self, *, Bucket: str, Key: str):  # noqa: N803
        del Bucket
        return {"Body": io.BytesIO(self.objects[Key])}

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        return _Paginator(sorted(self.objects))


def _chunk(chunk_id: str, page_id: int, entity: str, text: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "page_id": page_id,
        "revision_id": 10,
        "title": "Paris",
        "section_path": "See > Museums",
        "ordinal": 0,
        "text": text,
        "document_type": "listing",
        "entity_name": entity,
        "category_key": "museums",
        "source_url": f"https://example.invalid/{chunk_id}",
        "history_url": "https://example.invalid/history",
        "snapshot_date": "2026-08-25",
        "license": "CC BY-SA 4.0",
    }


def _objects(chunks: list[dict]) -> dict[str, bytes]:
    version = "wikivoyage-pilot-v1"
    root = f"wikivoyage/{version}"
    manifest = {
        "corpus_version": version,
        "s3_prefix": "wikivoyage",
        "embedding_model": "amazon.titan-embed-text-v2:0",
        "embedding_dimensions": 512,
        "article_count": len({chunk["page_id"] for chunk in chunks}),
        "selected_article_count": len({chunk["page_id"] for chunk in chunks}),
        "chunk_count": len(chunks),
        "snapshot_date": "2026-08-25",
        "source_url": "https://en.wikivoyage.org/",
        "source_checksum": "0" * 64,
        "parser_version": "test",
        "chunker_version": "test",
    }
    objects = {
        f"{root}/manifest.json": json.dumps(manifest).encode(),
    }
    for chunk in chunks:
        key = f"{root}/kb-documents/{chunk['chunk_id']}.txt"
        objects[key] = chunk["text"].encode()
        objects[f"{key}.metadata.json"] = json.dumps(bedrock_metadata(chunk, version)).encode()
    return objects


def test_import_s3_materializes_byte_identical_valid_prepared_corpus(tmp_path: Path):
    chunks = [
        _chunk("chunk-a", 1, "Musee d'Orsay", "Impressionist paintings"),
        _chunk("chunk-b", 2, "Musee Rodin", "Sculptures and gardens"),
    ]
    output = tmp_path / "prepared"

    result = import_bedrock_s3_corpus(
        bucket="travel-rag",
        prefix="wikivoyage",
        corpus_version="wikivoyage-pilot-v1",
        output=output,
        s3_client=FakeS3(_objects(chunks)),
        workers=2,
    )

    assert result["chunk_count"] == 2
    corpora = load_corpora([output])
    manifest, records = corpora[0]
    assert manifest["imported_from_s3"] is True
    assert [record["embedding_text"] for record in records] == [
        "Impressionist paintings",
        "Sculptures and gardens",
    ]
    assert records[0]["metadata"]["entity_name"] == "Musee d'Orsay"


def test_import_s3_refuses_a_sidecar_for_a_different_chunk(tmp_path: Path):
    chunk = _chunk("chunk-a", 1, "Musee d'Orsay", "Impressionist paintings")
    objects = _objects([chunk])
    key = "wikivoyage/wikivoyage-pilot-v1/kb-documents/chunk-a.txt.metadata.json"
    sidecar = json.loads(objects[key])
    sidecar["metadataAttributes"]["chunk_id"]["value"]["stringValue"] = "wrong-id"
    objects[key] = json.dumps(sidecar).encode()

    with pytest.raises(S3CorpusError, match="does not match document key"):
        import_bedrock_s3_corpus(
            bucket="travel-rag",
            prefix="wikivoyage",
            corpus_version="wikivoyage-pilot-v1",
            output=tmp_path / "prepared",
            s3_client=FakeS3(objects),
        )


def test_import_s3_derives_search_payload_fields_missing_from_legacy_sidecars(tmp_path: Path):
    chunk = _chunk("chunk-a", 1, "", "A long section about Impressionist paintings")
    objects = _objects([chunk])
    key = "wikivoyage/wikivoyage-pilot-v1/kb-documents/chunk-a.txt.metadata.json"
    sidecar = json.loads(objects[key])
    attributes = sidecar["metadataAttributes"]
    for field in ("category_key", "document_type", "entity_name"):
        attributes.pop(field, None)
    attributes["title"]["includeForEmbedding"] = True
    attributes["section"]["includeForEmbedding"] = True
    objects[key] = json.dumps(sidecar).encode()

    output = tmp_path / "prepared"
    result = import_bedrock_s3_corpus(
        bucket="travel-rag",
        prefix="wikivoyage",
        corpus_version="wikivoyage-pilot-v1",
        output=output,
        s3_client=FakeS3(objects),
    )

    assert result["derived_metadata_fields"] == [
        "category_key",
        "document_type",
        "entity_name",
    ]
    assert result["bedrock_embedding_metadata_fields"] == ["section", "title"]
    _, records = load_corpora([output])[0]
    assert records[0]["metadata"]["category_key"] == "museums"
    assert records[0]["metadata"]["document_type"] == "section_prose"


def test_import_s3_never_overwrites_an_existing_output(tmp_path: Path):
    output = tmp_path / "prepared"
    output.mkdir()
    with pytest.raises(S3CorpusError, match="already exists"):
        import_bedrock_s3_corpus(
            bucket="travel-rag",
            prefix="wikivoyage",
            corpus_version="wikivoyage-pilot-v1",
            output=output,
            s3_client=FakeS3({}),
        )
