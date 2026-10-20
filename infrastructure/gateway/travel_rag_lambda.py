"""AgentCore Gateway Lambda adapter for a Bedrock Knowledge Base."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from typing import Any

import boto3

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

TOOL_NAME = "search_travel_guides"
MAX_QUERY_CHARS = 500
MAX_LOCATION_CHARS = 200
MAX_FOCUSED_QUERY_CHARS = 900
MAX_TOP_K = 10
MAX_RERANK_CANDIDATES = 100
SUPPORTED_SEARCH_TYPES = {"HYBRID", "SEMANTIC"}
_WHITESPACE_RE = re.compile(r"\s+")
_INTENT_ROUTES: tuple[tuple[str, re.Pattern[str], str, tuple[str, ...]], ...] = (
    (
        "museums",
        re.compile(r"\b(?:museums?|galler(?:y|ies)|art galler(?:y|ies))\b"),
        "Museum or art gallery; cultural attraction",
        (
            "See > Museums and Galleries",
            "See > Museums",
            "See > Downtown > Museums",
        ),
    ),
    (
        "landmarks",
        re.compile(
            r"\b(?:landmarks?|monuments?|historic(?:al)? sites?|sights?|"
            r"cathedrals?|church(?:es)?|palaces?|castles?)\b"
        ),
        "Landmark, monument or historic site",
        (
            "See > Landmarks",
            "See > Historical sites",
            "See > Other attractions",
        ),
    ),
    (
        "attractions",
        re.compile(
            r"\b(?:attractions?|attracions?|places? to visit|what to (?:see|visit)|sightseeing)\b"
        ),
        "Tourist attraction or place to visit",
        (
            "See",
            "See > Museums and Galleries",
            "See > Landmarks",
            "See > Historical sites",
            "See > Other attractions",
        ),
    ),
    (
        "activities",
        re.compile(
            r"\b(?:activity|activities|things? to do|events?|festivals?|tours?|"
            r"cruises?|excursions?)\b"
        ),
        "Activity, tour, event or thing to do",
        (
            "Do",
            "Do > Events",
            "Do > Festivals",
            "Do > Festivals and events",
            "Do > Beaches",
        ),
    ),
    (
        "restaurants",
        re.compile(
            r"\b(?:restaurants?|places? to eat|somewhere to eat|something to eat|dining|"
            r"local food|cuisines?|brunch|lunch|dinner)\b|\bwhere\b.{0,40}\beat\b"
        ),
        "Restaurant; dining and places to eat",
        (
            "Eat",
            "Eat > Budget",
            "Eat > Mid-range",
            "Eat > Splurge",
            "Eat > Restaurants",
        ),
    ),
    (
        "drinks",
        re.compile(
            r"\b(?:bars?|pubs?|nightlife|night ?clubs?|places? to drink|cocktails?|"
            r"wine bars?|go dancing)\b"
        ),
        "Bar, pub or nightlife venue; places to drink",
        (
            "Drink",
            "Drink > Bars",
            "Drink > Coffee",
            "Drink > Beer and go-go bars",
            "Drink > Alcohol",
        ),
    ),
    (
        "accommodation",
        re.compile(
            r"\b(?:hotels?|hostels?|accommodation|lodging|places? to stay)\b|"
            r"\bwhere\b.{0,40}\bstay\b"
        ),
        "Accommodation; hotel, hostel or place to stay",
        (
            "Sleep",
            "Sleep > Budget",
            "Sleep > Mid-range",
            "Sleep > Splurge",
            "Sleep > Lodging",
        ),
    ),
    (
        "shopping",
        re.compile(r"\b(?:shopping|shops?|markets?|malls?|bookshops?|boutiques?|stores?)\b"),
        "Shopping; shop, boutique or market",
        (
            "Buy",
            "Buy > Markets",
            "Buy > Malls",
            "Buy > Shopping",
        ),
    ),
)
FRESHNESS_WARNING = (
    "This travel-guide corpus is a dated snapshot and may not reflect current prices, "
    "schedules, opening hours, entry rules, health guidance, or safety conditions."
)

_bedrock_agent_runtime = None


def _client():
    global _bedrock_agent_runtime
    _bedrock_agent_runtime = _bedrock_agent_runtime or boto3.client("bedrock-agent-runtime")
    return _bedrock_agent_runtime


def _normalize_tool_name(raw_name: str) -> str:
    return raw_name.split("___", 1)[1] if "___" in raw_name else raw_name


def _tool_name(context: Any) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    return _normalize_tool_name(str(custom.get("bedrockAgentCoreToolName", "")))


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing retrieval configuration: {name}")
    return value


def _normalize_filter_value(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return _WHITESPACE_RE.sub(" ", normalized.strip().casefold())[:240]


def _focus_query(query: str, destination: str, country: str) -> str:
    """Add location context in a natural form when the query omits it."""
    normalized_query = _normalize_filter_value(query)
    if _normalize_filter_value(destination) in normalized_query:
        return query

    location = ", ".join(part for part in (destination, country) if part)
    punctuation = query[-1] if query[-1] in ".?!" else ""
    stem = query[:-1].rstrip() if punctuation else query
    return f"{stem} in {location}{punctuation}"


def _enrich_query(query: str, destination: str, country: str, category: str) -> str:
    """Preserve the user's request and add fields mirrored in embedded documents."""
    natural_query = _focus_query(query, destination, country)
    context = [f"Destination: {destination}"]
    if country:
        context.append(f"Country: {country}")
    context.append(f"Category: {category}")
    return f"{natural_query}\n\n{'\n'.join(context)}"


def _intent_route(query: str) -> tuple[str, str, tuple[str, ...]] | None:
    """Map broad travel questions to exact section values present in the corpus."""
    normalized_query = _normalize_filter_value(query)
    for name, pattern, rewrite, sections in _INTENT_ROUTES:
        if pattern.search(normalized_query):
            return name, rewrite, sections
    return None


def _retrieval_filter(
    destination_key: str,
    sections: tuple[str, ...],
    category_keys: tuple[str, ...] = (),
) -> dict:
    destination_filter = {
        "equals": {
            "key": "destination_key",
            "value": destination_key,
        }
    }
    if category_keys:
        category_filters = [
            {"equals": {"key": "category_key", "value": category_key}}
            for category_key in category_keys
        ]
        category_filter = (
            category_filters[0] if len(category_filters) == 1 else {"orAll": category_filters}
        )
        return {
            "andAll": [
                destination_filter,
                category_filter,
            ]
        }
    if not sections:
        return destination_filter

    section_filters = [{"equals": {"key": "section", "value": section}} for section in sections]
    section_filter = section_filters[0] if len(section_filters) == 1 else {"orAll": section_filters}
    return {"andAll": [destination_filter, section_filter]}


def _reranking_model_arn() -> str:
    return (os.environ.get("BEDROCK_RERANKING_MODEL_ARN") or "").strip()


def _search_type() -> str:
    search_type = (os.environ.get("RAG_SEARCH_TYPE") or "SEMANTIC").strip().upper()
    if search_type not in SUPPORTED_SEARCH_TYPES:
        raise RuntimeError(
            f"RAG_SEARCH_TYPE must be one of {', '.join(sorted(SUPPORTED_SEARCH_TYPES))}"
        )
    return search_type


def _candidate_count(top_k: int) -> int:
    if not _reranking_model_arn():
        # Over-fetch even without a reranker: _deduplicate_entities drops repeat
        # listings, so asking for exactly top_k would return fewer than top_k.
        return min(top_k * 2, MAX_RERANK_CANDIDATES)
    raw = os.environ.get("RAG_RERANK_CANDIDATES", "20")
    try:
        configured = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("RAG_RERANK_CANDIDATES must be an integer") from exc
    if not 1 <= configured <= MAX_RERANK_CANDIDATES:
        raise RuntimeError(f"RAG_RERANK_CANDIDATES must be between 1 and {MAX_RERANK_CANDIDATES}")
    return max(top_k, configured)


def _retrieve(
    query: str,
    top_k: int,
    destination_key: str,
    sections: tuple[str, ...] = (),
    category_keys: tuple[str, ...] = (),
) -> dict:
    """Run semantic retrieval through Bedrock Knowledge Bases."""
    return _client().retrieve(
        knowledgeBaseId=_required_env("BEDROCK_KNOWLEDGE_BASE_ID"),
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": top_k,
                "overrideSearchType": _search_type(),
                "filter": _retrieval_filter(destination_key, sections, category_keys),
            }
        },
    )


def _deduplicate_entities(results: list[dict]) -> list[dict]:
    """Keep one chunk per attraction, preserving the incoming ranking.

    A destination overview page and its district subpage usually both carry a
    listing for the same attraction, so "Musée d'Orsay" arrives twice: once from
    ``Paris`` and once from ``Paris/7th arrondissement``. Without this, one
    attraction can occupy several of the result slots. Chunks with no entity
    name are section prose and are always kept.
    """
    seen: set[str] = set()
    unique: list[dict] = []
    for result in results:
        metadata = result.get("metadata") or {}
        entity = _normalize_filter_value(str(metadata.get("entity_name") or ""))
        if entity:
            if entity in seen:
                continue
            seen.add(entity)
        unique.append(result)
    return unique


def _rerank(query: str, results: list[dict], top_k: int) -> tuple[list[dict], bool]:
    """Rerank filtered vector candidates while retaining their vector scores."""
    model_arn = _reranking_model_arn()
    if not model_arn or not results:
        return results[:top_k], False

    sources = []
    for result in results:
        metadata = result.get("metadata") or {}
        content = result.get("content") or {}
        sources.append(
            {
                "type": "INLINE",
                "inlineDocumentSource": {
                    "type": "JSON",
                    "jsonDocument": {
                        "destination": metadata.get("destination_key"),
                        "title": metadata.get("title"),
                        "section": metadata.get("section"),
                        "text": content.get("text"),
                    },
                },
            }
        )

    response = _client().rerank(
        queries=[{"type": "TEXT", "textQuery": {"text": query}}],
        sources=sources,
        rerankingConfiguration={
            "type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {
                "modelConfiguration": {"modelArn": model_arn},
                "numberOfResults": min(top_k, len(results)),
            },
        },
    )
    ranked: list[dict] = []
    for reranked in response.get("results") or []:
        index = reranked.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(results):
            raise RuntimeError("Bedrock reranker returned an invalid result index")
        item = dict(results[index])
        item["_rerank_score"] = float(reranked.get("relevanceScore", 0.0) or 0.0)
        ranked.append(item)
    if not ranked:
        raise RuntimeError("Bedrock reranker returned no results")
    return ranked, True


def _location_uri(location: dict) -> str | None:
    for location_type in (
        "s3Location",
        "webLocation",
        "confluenceLocation",
        "salesforceLocation",
        "sharePointLocation",
        "customDocumentLocation",
    ):
        value = location.get(location_type)
        if not isinstance(value, dict):
            continue
        for key in ("uri", "url", "id"):
            candidate = value.get(key)
            if candidate:
                return str(candidate)
    return None


def _result(result: dict) -> dict:
    score = float(result.get("score", 0.0) or 0.0)
    metadata = result.get("metadata") or {}
    content = result.get("content") or {}
    source_url = metadata.get("source_url") or _location_uri(result.get("location") or {})
    return {
        "chunk_id": str(metadata.get("chunk_id") or ""),
        "title": str(metadata.get("title") or ""),
        "destination_key": str(metadata.get("destination_key") or ""),
        "category_key": str(metadata.get("category_key") or ""),
        "document_type": str(metadata.get("document_type") or ""),
        "entity_name": str(metadata.get("entity_name") or ""),
        "section": str(metadata.get("section") or ""),
        "text": str(content.get("text") or ""),
        "source_url": str(source_url or ""),
        "history_url": str(metadata.get("history_url") or ""),
        "revision_id": metadata.get("revision_id"),
        "score": score,
        "rerank_score": result.get("_rerank_score"),
        "snapshot_date": str(metadata.get("snapshot_date") or ""),
        "license": str(metadata.get("license") or ""),
    }


def _response(status_code: int, body: dict) -> dict:
    return {"statusCode": status_code, "body": json.dumps(body, ensure_ascii=False)}


def lambda_handler(event, context):
    if _tool_name(context) != TOOL_NAME:
        return _response(400, {"error": "unknown_tool", "message": "Unknown RAG tool"})

    if not isinstance(event, dict):
        return _response(400, {"error": "invalid_input", "message": "tool input must be an object"})
    for field in ("query", "destination", "country"):
        if event.get(field) is not None and not isinstance(event[field], str):
            return _response(
                400,
                {"error": f"invalid_{field}", "message": f"{field} must be a string"},
            )

    query = (event.get("query") or "").strip()
    if not query:
        return _response(400, {"error": "invalid_query", "message": "query is required"})
    if len(query) > MAX_QUERY_CHARS:
        return _response(
            400,
            {"error": "invalid_query", "message": f"query exceeds {MAX_QUERY_CHARS} characters"},
        )
    destination = (event.get("destination") or "").strip()
    country = (event.get("country") or "").strip()
    for field, value in (("destination", destination), ("country", country)):
        if len(value) > MAX_LOCATION_CHARS:
            return _response(
                400,
                {
                    "error": f"invalid_{field}",
                    "message": f"{field} exceeds {MAX_LOCATION_CHARS} characters",
                },
            )
    if not destination:
        return _response(
            400,
            {"error": "invalid_destination", "message": "destination is required"},
        )
    intent_route = _intent_route(query)
    if intent_route:
        intent, category_context, section_filters = intent_route
        focused_query = _enrich_query(query, destination, country, category_context)
    else:
        intent = None
        section_filters = ()
        focused_query = _focus_query(query, destination, country)
    if len(focused_query) > MAX_FOCUSED_QUERY_CHARS:
        return _response(
            400,
            {
                "error": "invalid_query",
                "message": f"combined query exceeds {MAX_FOCUSED_QUERY_CHARS} characters",
            },
        )
    try:
        raw_top_k = event.get("top_k", 5)
        if isinstance(raw_top_k, bool) or not isinstance(raw_top_k, int):
            raise ValueError
        if not 1 <= raw_top_k <= MAX_TOP_K:
            raise ValueError
        top_k = raw_top_k
    except (TypeError, ValueError):
        return _response(
            400,
            {
                "error": "invalid_top_k",
                "message": f"top_k must be an integer from 1 to {MAX_TOP_K}",
            },
        )

    started = time.monotonic()
    try:
        destination_key = _normalize_filter_value(destination)
        candidate_count = _candidate_count(top_k)
        category_keys: tuple[str, ...] = ()
        if intent and os.environ.get("RAG_CATEGORY_FILTER_KEY"):
            category_keys = (
                ("attractions", "museums", "landmarks") if intent == "attractions" else (intent,)
            )
        response = _retrieve(
            focused_query,
            candidate_count,
            destination_key,
            section_filters,
            category_keys,
        )
        candidates = response.get("retrievalResults") or []
        route_fallback = False
        if intent and not candidates:
            route_fallback = True
            focused_query = _focus_query(query, destination, country)
            response = _retrieve(focused_query, candidate_count, destination_key)
            candidates = response.get("retrievalResults") or []
        # Collapse repeat listings before ranking so neither the reranker's
        # result budget nor top_k is spent on the same attraction twice.
        candidates = _deduplicate_entities(candidates)
        try:
            ranked, reranked = _rerank(query, candidates, top_k)
            ranking = "BEDROCK_RERANK" if reranked else "SEMANTIC"
        except Exception:
            LOGGER.exception("Travel RAG reranking failed; using filtered semantic order")
            ranked = candidates[:top_k]
            ranking = "SEMANTIC_FALLBACK"
        results = [_result(item) for item in ranked]
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return _response(
            200,
            {
                "corpus": _required_env("RAG_CORPUS_VERSION"),
                "results": results,
                "warnings": [FRESHNESS_WARNING],
                "retrieval": {
                    "result_count": len(results),
                    "candidate_count": len(candidates),
                    "search_type": _search_type(),
                    "ranking": ranking,
                    "intent": intent,
                    "query_rewritten": bool(intent and not route_fallback),
                    "query_enriched": bool(intent and not route_fallback),
                    "retrieval_query": focused_query,
                    "category_filter": (
                        category_keys[0] if not route_fallback and len(category_keys) == 1 else None
                    ),
                    "category_filters": list(category_keys) if not route_fallback else [],
                    "section_filters": (
                        list(section_filters) if not route_fallback and not category_keys else []
                    ),
                    "route_fallback": route_fallback,
                    "latency_ms": elapsed_ms,
                },
            },
        )
    except Exception:
        LOGGER.exception("Travel RAG retrieval failed")
        return _response(
            503,
            {
                "error": "retrieval_unavailable",
                "message": "The travel-guide corpus is temporarily unavailable.",
                "results": [],
            },
        )
