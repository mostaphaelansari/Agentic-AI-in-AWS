#!/usr/bin/env python3
"""Compare live Bedrock Knowledge Bases on a shared retrieval evaluation set."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import unicodedata
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.opensource import query as planner  # noqa: E402
from rag.wikivoyage import category_filter_value  # noqa: E402

# Wikivoyage prose uses typographic punctuation; the evaluation datasets were
# hand-typed with ASCII. NFKD does not unify the two, so without this table
# "Cité des sciences et de l’industrie" (U+2019) never matches the dataset's
# "Cité des sciences et de l'industrie" (U+0027) and a rank-1 hit is scored as
# a miss. Kept byte-identical to scripts/opensource_rag.py so neither stack is
# advantaged by its acceptance logic.
_PUNCTUATION_FOLD = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "ʼ": "'",
        "´": "'",
        "`": "'",
        "“": '"',
        "”": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
    }
)


def _normalize(value: object) -> str:
    folded = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join(
        "".join(character for character in folded if not unicodedata.combining(character))
        .translate(_PUNCTUATION_FOLD)
        .casefold()
        .split()
    )


def _knowledge_base(value: str) -> tuple[str, str, str]:
    label, separator, specification = value.partition("=")
    knowledge_base_id, search_separator, search_type = specification.partition(":")
    search_type = (search_type if search_separator else "SEMANTIC").strip().upper()
    if (
        not separator
        or not label.strip()
        or not knowledge_base_id.strip()
        or search_type not in {"HYBRID", "SEMANTIC"}
    ):
        raise argparse.ArgumentTypeError(
            "knowledge bases must use LABEL=ID or LABEL=ID:SEMANTIC|HYBRID"
        )
    return label.strip(), knowledge_base_id.strip(), search_type


def _matches(case: dict, result: dict) -> bool:
    metadata = result.get("metadata") or {}
    accepted_category = case.get("accept_category")
    result_category = metadata.get("category_key") or category_filter_value(
        str(metadata.get("section") or "")
    )
    if accepted_category and result_category == accepted_category:
        return True
    entity = _normalize(metadata.get("entity_name"))
    text = _normalize((result.get("content") or {}).get("text"))
    return any(
        (bool(entity) and (accepted in entity or entity in accepted)) or accepted in text
        for accepted in (_normalize(value) for value in case.get("accept") or [])
        if accepted and (entity or text)
    )


def _retrieval_filter(
    destination_key: str,
    category_keys: tuple[str, ...] = (),
    sections: tuple[str, ...] = (),
) -> dict:
    filters = [
        {
            "equals": {
                "key": "destination_key",
                "value": destination_key,
            }
        }
    ]
    if category_keys:
        categories = [
            {"equals": {"key": "category_key", "value": category}} for category in category_keys
        ]
        filters.append(categories[0] if len(categories) == 1 else {"orAll": categories})
    elif sections:
        section_filters = [{"equals": {"key": "section", "value": section}} for section in sections]
        filters.append(
            section_filters[0] if len(section_filters) == 1 else {"orAll": section_filters}
        )
    return filters[0] if len(filters) == 1 else {"andAll": filters}


def _deduplicate_entities(results: list[dict]) -> list[dict]:
    """Mirror the open-source pipeline's one-result-per-entity rule."""
    seen: set[str] = set()
    unique = []
    for result in results:
        entity = _normalize((result.get("metadata") or {}).get("entity_name"))
        if entity:
            if entity in seen:
                continue
            seen.add(entity)
        unique.append(result)
    return unique


def evaluate(
    client,
    knowledge_base_id: str,
    cases: list[dict],
    top_k: int,
    search_type: str,
    expected_corpus_version: str = "",
    category_filter_enabled: bool = True,
) -> dict:
    rows = []
    for case in cases:
        plan = planner.plan(
            case["query"],
            case["destination"],
            case.get("country", ""),
            category_filter_enabled=category_filter_enabled,
        )
        candidate_count = planner.candidate_count(top_k, rerank_enabled=False)
        retrieval_query = plan["retrieval_query"]
        response = client.retrieve(
            knowledgeBaseId=knowledge_base_id,
            retrievalQuery={"text": retrieval_query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": candidate_count,
                    "overrideSearchType": search_type,
                    "filter": _retrieval_filter(
                        plan["destination_key"],
                        plan["category_filters"],
                        plan["section_filters"],
                    ),
                }
            },
        )
        results = response.get("retrievalResults") or []
        route_fallback = False
        if not results and plan["intent"]:
            route_fallback = True
            retrieval_query = planner.focus_query(
                case["query"], case["destination"], case.get("country", "")
            )
            response = client.retrieve(
                knowledgeBaseId=knowledge_base_id,
                retrievalQuery={"text": retrieval_query},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {
                        "numberOfResults": candidate_count,
                        "overrideSearchType": search_type,
                        "filter": _retrieval_filter(plan["destination_key"], ()),
                    }
                },
            )
            results = response.get("retrievalResults") or []
        if expected_corpus_version:
            versions = {
                str((result.get("metadata") or {}).get("corpus_version") or "")
                for result in results
            }
            if versions - {expected_corpus_version}:
                raise RuntimeError(
                    f"Knowledge Base returned corpus versions {sorted(versions)!r}; "
                    f"expected only {expected_corpus_version!r}"
                )
        results = _deduplicate_entities(results)[:top_k]
        rank = next(
            (
                position
                for position, result in enumerate(results, start=1)
                if _matches(case, result)
            ),
            None,
        )
        top = results[0] if results else {}
        top_metadata = top.get("metadata") or {}
        entity_scores = [
            float(result.get("score") or 0.0)
            for result in results
            if (result.get("metadata") or {}).get("entity_name")
        ]
        rows.append(
            {
                "query": case["query"],
                "category": case.get("category_key") or plan["intent"],
                "retrieval_query": retrieval_query,
                "route_fallback": route_fallback,
                "rank": rank,
                "top_entity": top_metadata.get("entity_name"),
                "top_score": float(top.get("score") or 0.0),
                "best_entity_score": max(entity_scores) if entity_scores else None,
            }
        )
    top_scores = [row["top_score"] for row in rows]
    entity_scores = [
        row["best_entity_score"] for row in rows if row["best_entity_score"] is not None
    ]
    return {
        "search_type": search_type,
        "corpus_version": expected_corpus_version or None,
        "comparison_contract": {
            "query_planner": "rag.opensource.query",
            "category_filter": category_filter_enabled,
            "rerank": False,
            "deduplicate_entities": True,
            "candidate_count": planner.candidate_count(top_k, rerank_enabled=False),
            "top_k": top_k,
        },
        "queries": len(rows),
        f"hit_at_{top_k}": sum(row["rank"] is not None for row in rows) / len(rows),
        "mrr": statistics.fmean((1 / row["rank"] if row["rank"] else 0) for row in rows),
        "top1_named_entity_rate": sum(bool(row["top_entity"]) for row in rows) / len(rows),
        "top1_score_mean": statistics.fmean(top_scores),
        "best_named_entity_score_mean": statistics.fmean(entity_scores) if entity_scores else None,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-base", action="append", type=_knowledge_base, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--corpus-version",
        default="",
        help="fail if retrieved results are not from this exact corpus version",
    )
    parser.add_argument(
        "--no-category-filter",
        action="store_true",
        help="use legacy exact section filters, matching corpora without category_key metadata",
    )
    args = parser.parse_args()
    if not 1 <= args.top_k <= 100:
        parser.error("--top-k must be between 1 and 100")
    cases = json.loads(args.queries.read_text(encoding="utf-8"))["queries"]
    client = boto3.client("bedrock-agent-runtime", region_name=args.region)
    report = {
        label: evaluate(
            client,
            knowledge_base_id,
            cases,
            args.top_k,
            search_type,
            args.corpus_version,
            not args.no_category_filter,
        )
        for label, knowledge_base_id, search_type in args.knowledge_base
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
