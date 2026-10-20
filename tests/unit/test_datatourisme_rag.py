from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rag.datatourisme import (  # noqa: E402
    Poi,
    classify_poi,
    embedding_text,
    is_paris_record,
    ontology_types,
    read_paris_pois,
    write_prepared_corpus,
)
from rag.ingest import load_prepared  # noqa: E402


def _poi(**overrides: object) -> Poi:
    values: dict[str, object] = {
        "name": "Le Jules Verne",
        "category_key": "restaurants",
        "category_label": "Restaurant; dining and places to eat",
        "types": ("Restaurant", "PlaceOfInterest"),
        "address": "Tour Eiffel, 2e étage",
        "postal_code": "75007",
        "city": "Paris",
        "latitude": "48.8584",
        "longitude": "2.2945",
        "description": "Une cuisine française avec une vue sur Paris.",
        "classification": "",
        "source": "Paris je t'aime - Office de Tourisme",
        "distributor": "Paris je t'aime - Office de Tourisme",
        "updated": "2026-03-20",
        "contact_urls": ("https://example.test/jules-verne",),
        "source_uri": "https://data.datatourisme.fr/example",
    }
    values.update(overrides)
    return Poi(**values)  # type: ignore[arg-type]


def test_ontology_types_and_category_mapping_are_explicit() -> None:
    types = ontology_types(
        "https://www.datatourisme.fr/ontology/core#PlaceOfInterest|"
        "https://www.datatourisme.fr/ontology/core#Restaurant|"
        "http://schema.org/FoodEstablishment"
    )
    assert types == ("PlaceOfInterest", "Restaurant", "FoodEstablishment")
    assert classify_poi(types) == ("restaurants", "Restaurant; dining and places to eat")


def test_paris_filter_rejects_other_ile_de_france_cities() -> None:
    assert is_paris_record({"Code_postal_et_commune": "75007#Paris"})
    assert is_paris_record({"Code_postal_et_commune": "75016#Paris 16e Arrondissement"})
    assert not is_paris_record({"Code_postal_et_commune": "94380#Bonneuil-sur-Marne"})


def test_embedding_text_contains_retrieval_critical_structure() -> None:
    text = embedding_text(_poi())
    assert "Destination: Paris" in text
    assert "Country: France" in text
    assert "Category: Restaurant; dining and places to eat" in text
    assert "Entity: Le Jules Verne" in text
    assert "Document type: structured point of interest" in text
    assert "Attraction: Le Jules Verne" not in text


def test_read_paris_pois_prefers_official_richer_duplicate(tmp_path: Path) -> None:
    columns = [
        "Nom_du_POI",
        "Categories_de_POI",
        "Latitude",
        "Longitude",
        "Adresse_postale",
        "Code_postal_et_commune",
        "Periodes_regroupees",
        "Covid19_mesures_specifiques",
        "Createur_de_la_donnee",
        "SIT_diffuseur",
        "Date_de_mise_a_jour",
        "Contacts_du_POI",
        "Classements_du_POI",
        "Description",
        "URI_ID_du_POI",
    ]
    path = tmp_path / "idf.csv"
    base = {
        "Nom_du_POI": "Musée Exemple",
        "Categories_de_POI": "https://www.datatourisme.fr/ontology/core#Museum",
        "Latitude": "48.1",
        "Longitude": "2.1",
        "Adresse_postale": "1 rue Exemple",
        "Code_postal_et_commune": "75001#Paris",
        "Periodes_regroupees": "",
        "Covid19_mesures_specifiques": "",
        "Createur_de_la_donnee": "Other publisher",
        "SIT_diffuseur": "Other publisher",
        "Date_de_mise_a_jour": "2026-01-01",
        "Contacts_du_POI": "#https://example.test",
        "Classements_du_POI": "",
        "Description": "Description courte.",
        "URI_ID_du_POI": "https://data.datatourisme.fr/old",
    }
    official = {
        **base,
        "Createur_de_la_donnee": "Paris je t'aime - Office de Tourisme",
        "SIT_diffuseur": "Paris je t'aime - Office de Tourisme",
        "Date_de_mise_a_jour": "2026-03-20",
        "Description": "Description officielle plus riche et plus utile.",
        "URI_ID_du_POI": "https://data.datatourisme.fr/new",
    }
    outside = {**base, "Nom_du_POI": "Outside", "Code_postal_et_commune": "78000#Versailles"}
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows([base, official, outside])
    pois = read_paris_pois(path)
    assert len(pois) == 1
    assert pois[0].source == "Paris je t'aime - Office de Tourisme"
    assert pois[0].description == official["Description"]


def test_prepared_corpus_is_one_document_per_poi_and_ingest_compatible(
    tmp_path: Path,
) -> None:
    prepared = tmp_path / "prepared"
    manifest = write_prepared_corpus(
        [_poi()],
        prepared,
        corpus_version="datatourisme-paris-v1",
        snapshot_date="2026-08-25",
        source_url="https://example.test/idf.csv",
        source_checksum="abc",
    )
    assert manifest["article_count"] == 1
    assert manifest["chunk_count"] == 1
    assert manifest["chunker_version"] == "one-poi-one-document-v1"
    validated, records = load_prepared(
        prepared, prefix="datatourisme", namespace="datatourisme-paris-v1"
    )
    assert validated == manifest
    assert len(records) == 1
    assert records[0]["metadata"]["category_key"] == "restaurants"
    page = json.loads(next((prepared / "pages").glob("*.json")).read_text(encoding="utf-8"))
    assert len(page["chunks"]) == 1
    assert page["chunks"][0]["text"] == records[0]["embedding_text"]
