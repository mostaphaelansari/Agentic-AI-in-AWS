from scripts.opensearch_hybrid_index import METADATA_FIELD, TEXT_FIELD, VECTOR_FIELD, _mapping


def test_hybrid_index_has_faiss_vector_and_filterable_text_fields() -> None:
    properties = _mapping(512)["mappings"]["properties"]
    assert properties[VECTOR_FIELD]["dimension"] == 512
    assert properties[VECTOR_FIELD]["space_type"] == "cosinesimil"
    assert properties[VECTOR_FIELD]["method"]["engine"] == "faiss"
    assert properties[TEXT_FIELD] == {"type": "text", "index": True}
    assert properties[METADATA_FIELD] == {"type": "text", "index": False}


def test_hybrid_index_leaves_custom_metadata_to_bedrock() -> None:
    properties = _mapping(512)["mappings"]["properties"]
    assert "destination_key" not in properties
    assert "category_key" not in properties
    assert "entity_name" not in properties


def test_hybrid_index_can_explicitly_use_l2() -> None:
    vector = _mapping(1024, "l2")["mappings"]["properties"][VECTOR_FIELD]
    assert vector["dimension"] == 1024
    assert vector["space_type"] == "l2"
