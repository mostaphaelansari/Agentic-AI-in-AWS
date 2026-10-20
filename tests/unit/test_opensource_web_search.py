from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "opensource" / "mcp-tools"))
import server  # noqa: E402


def test_web_search_bounds_and_cleans_results(monkeypatch):
    captured = {}

    def fake_search(**kwargs):
        captured.update(kwargs)
        return [
            {"title": "Official tourism", "href": "https://example.org/visit", "body": "Details"},
            {"title": "Unsafe scheme", "href": "javascript:alert(1)", "body": "Ignore"},
        ]

    monkeypatch.setattr(server, "_DDGS_MAX_RESULTS", 7)
    monkeypatch.setattr(server, "_ddgs_text", fake_search)

    payload = json.loads(server.webSearch("  Lisbon attractions  ", max_results=100, timelimit="m"))

    assert captured == {
        "query": "Lisbon attractions",
        "region": "us-en",
        "safesearch": "moderate",
        "timelimit": "m",
        "max_results": 7,
        "backend": "auto",
    }
    assert payload == {
        "query": "Lisbon attractions",
        "results": [
            {"title": "Official tourism", "url": "https://example.org/visit", "snippet": "Details"}
        ],
    }


@pytest.mark.parametrize("query", ["", "   ", "x" * 501])
def test_web_search_rejects_invalid_queries(query):
    with pytest.raises(ValueError):
        server.webSearch(query)


def test_web_search_rejects_invalid_timelimit():
    with pytest.raises(ValueError, match="timelimit"):
        server.webSearch("Paris museums", timelimit="hour")
