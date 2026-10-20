from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "opensource" / "mcp-tools"))
import server  # noqa: E402


def test_search_map_places_shapes_results(monkeypatch):
    captured = {}

    def fake_search(params):
        captured.update(params)
        return [
            {
                "display_name": "Lisbon, Portugal",
                "lat": "38.7077507",
                "lon": "-9.1365919",
                "category": "boundary",
                "type": "administrative",
                "osm_type": "relation",
                "osm_id": 295480,
            }
        ]

    monkeypatch.setattr(server, "_nominatim_search", fake_search)
    payload = json.loads(server.searchMapPlaces("  Lisbon  ", limit=99, language="pt"))

    assert captured["q"] == "Lisbon"
    assert captured["limit"] == 10
    assert captured["accept-language"] == "pt"
    assert payload["results"][0]["latitude"] == pytest.approx(38.7077507)
    assert payload["results"][0]["osm_id"] == 295480
    assert payload["attribution"] == "© OpenStreetMap contributors"


def test_find_nearby_places_builds_bounded_query_and_skips_unnamed(monkeypatch):
    captured = {}

    def fake_search(query):
        captured["query"] = query
        return {
            "elements": [
                {"type": "node", "id": 10, "lat": 38.7, "lon": -9.1, "tags": {}},
                {
                    "type": "way",
                    "id": 20,
                    "center": {"lat": 38.71, "lon": -9.14},
                    "tags": {
                        "name": "Example Museum",
                        "tourism": "museum",
                        "website": "https://museum.example",
                        "opening_hours": "Tu-Su 10:00-18:00",
                    },
                },
                {
                    "type": "way",
                    "id": 20,
                    "center": {"lat": 38.71, "lon": -9.14},
                    "tags": {"name": "Example Museum", "historic": "building"},
                },
            ]
        }

    monkeypatch.setattr(server, "_overpass_search", fake_search)
    payload = json.loads(
        server.findNearbyPlaces(38.7, -9.1, category="museums", radius=50_000, limit=50)
    )

    assert "around:10000,38.7,-9.1" in captured["query"]
    assert '["tourism"="museum"]' in captured["query"]
    assert payload["radius_metres"] == 10_000
    assert payload["results"] == [
        {
            "name": "Example Museum",
            "latitude": 38.71,
            "longitude": -9.14,
            "distance_metres": 3645,
            "place_type": "tourism:museum",
            "osm_type": "way",
            "osm_id": 20,
            "osm_url": "https://www.openstreetmap.org/way/20",
            "website": "https://museum.example",
            "opening_hours": "Tu-Su 10:00-18:00",
        }
    ]


def test_places_category_combines_visitor_places_and_green_spaces():
    query = server._overpass_category_query(48.8566, 2.3522, "places", 2000)

    assert '["tourism"="attraction"]' in query
    assert '["tourism"="museum"]' in query
    assert '["historic"]' in query
    assert '["leisure"="park"]' in query
    assert '["leisure"="garden"]' in query


def test_overpass_falls_back_after_retryable_http_error(monkeypatch):
    calls = []

    def fake_request(url, data=None, timeout=20):
        calls.append(url)
        if url == "https://first.example/api":
            raise urllib.error.HTTPError(url, 504, "timeout", None, None)
        return {"elements": []}

    monkeypatch.setattr(
        server,
        "_OVERPASS_URLS",
        ("https://first.example/api", "https://second.example/api"),
    )
    monkeypatch.setattr(server, "_request_json", fake_request)

    assert server._overpass_search("unique fallback query") == {"elements": []}
    assert calls == ["https://first.example/api", "https://second.example/api"]


def test_overpass_error_includes_endpoint_failures(monkeypatch):
    def fake_request(url, data=None, timeout=20):
        raise urllib.error.HTTPError(url, 429, "too many requests", None, None)

    monkeypatch.setattr(
        server,
        "_OVERPASS_URLS",
        ("https://first.example/api", "https://second.example/api"),
    )
    monkeypatch.setattr(server, "_request_json", fake_request)

    with pytest.raises(RuntimeError) as exc:
        server._overpass_search("unique failure query")

    message = str(exc.value)
    assert "https://first.example/api -> HTTP 429" in message
    assert "https://second.example/api -> HTTP 429" in message


@pytest.mark.parametrize(
    "kwargs",
    [
        {"latitude": 91, "longitude": 0},
        {"latitude": 0, "longitude": 181},
        {"latitude": 0, "longitude": 0, "category": "everything"},
    ],
)
def test_find_nearby_places_rejects_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        server.findNearbyPlaces(**kwargs)
