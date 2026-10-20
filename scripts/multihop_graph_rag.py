#!/usr/bin/env python3
"""Prepare and evaluate MuSiQue/2Wiki with OpenSearch and Neptune Analytics.

Gold supporting labels are retained only in the prepared evaluation cases. The
normal graph is derived from article titles explicitly mentioned in paragraph
text. ``--graph-source gold-evidence`` is a 2Wiki oracle experiment and is
reported as such; it must not be presented as a leakage-free result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.multihop import (  # noqa: E402
    MultihopNeptuneStore,
    MultihopOpenSearchStore,
    aggregate_retrieval_metrics,
    compare_retrieval_metrics,
    index_prepared,
    load_prepared,
    prepare_dataset,
    retrieve_flat,
    retrieve_graph,
    retrieve_iterative,
    write_prepared,
)
from rag.multihop.datasets import (  # noqa: E402
    TWO_WIKI_BRIDGE_TYPES,
    TWO_WIKI_CONTROL_TYPES,
    filter_question_types,
)
from rag.multihop.evaluate import question_type_cohort_metrics  # noqa: E402
from rag.multihop.provenance import run_provenance  # noqa: E402
from rag.multihop.selection import EvidenceSelectionError  # noqa: E402
from rag.neptune_opensearch import NeptuneStoreError, OpenSearchStoreError  # noqa: E402
from rag.opensource.embeddings import (  # noqa: E402
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_HOST,
    DEFAULT_TITAN_DIMENSIONS,
    DEFAULT_TITAN_MODEL,
    BedrockTitanEmbedder,
    Embedder,
    EmbeddingError,
    OllamaEmbedder,
)


def _require(value: str, message: str) -> str:
    if not value:
        raise ValueError(message)
    return value


def _search_store(args: argparse.Namespace) -> MultihopOpenSearchStore:
    return MultihopOpenSearchStore(
        _require(args.opensearch_endpoint, "OpenSearch endpoint is required"),
        args.opensearch_index,
        region=args.region,
        service=args.opensearch_service,
    )


def _graph_store(args: argparse.Namespace) -> MultihopNeptuneStore:
    return MultihopNeptuneStore(
        _require(args.neptune_graph_id, "Neptune graph ID is required"),
        region=args.region,
        namespace=args.graph_namespace,
    )


def _embedder(args: argparse.Namespace) -> Embedder:
    if args.embedding_provider == "titan":
        return BedrockTitanEmbedder(
            region=args.region,
            model=args.titan_model,
            dimensions=args.titan_dimensions,
        )
    return OllamaEmbedder(
        host=args.ollama_host,
        model=args.embed_model,
        batch_size=args.embed_batch_size,
    )


def command_prepare(args: argparse.Namespace) -> int:
    excluded_case_ids: set[str] = set()
    if args.exclude_case_ids:
        excluded_case_ids = {
            line.strip()
            for line in args.exclude_case_ids.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    prepared = prepare_dataset(
        args.input,
        dataset=args.dataset,
        split=args.split,
        sample_size=args.sample_size,
        seed=args.seed,
        exclude_case_ids=excluded_case_ids,
        question_types=args.question_type,
    )
    write_prepared(prepared, args.output)
    print(json.dumps(prepared["manifest"], indent=2, ensure_ascii=False))
    return 0


def command_index(args: argparse.Namespace) -> int:
    with _search_store(args) as search_store:
        result = index_prepared(
            args.prepared,
            search_store=search_store,
            graph_store=_graph_store(args),
            embedder=_embedder(args),
            graph_source=args.graph_source,
            batch_size=args.batch_size,
            resume=not args.no_resume,
            progress=print,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _case(prepared: dict[str, Any], case_id: str) -> dict[str, Any]:
    for case in prepared["cases"]:
        if str(case["case_id"]) == case_id:
            return case
    raise ValueError(f"Unknown case ID {case_id!r}")


def _retrieve(
    strategy: str,
    question: str,
    *,
    args: argparse.Namespace,
    search_store: MultihopOpenSearchStore,
    graph_store: MultihopNeptuneStore | None,
    embedder: Embedder,
    dataset: str,
    case_id: str,
) -> list[dict[str, Any]]:
    common = {
        "search_store": search_store,
        "embedder": embedder,
        "dataset": dataset,
        "case_id": "" if args.pooled else case_id,
        "top_k": args.top_k,
        "candidate_limit": args.candidate_limit,
        "reranker_url": "" if args.no_rerank else args.reranker_url,
        "bedrock_reranker_model": (
            "" if args.no_rerank else args.bedrock_reranker_model
        ),
        "region": args.region,
        "reranker_timeout": args.reranker_timeout,
        "evidence_selector_model": args.evidence_selector_model,
    }
    if strategy == "flat":
        return retrieve_flat(question, **common)
    if strategy == "iterative":
        return retrieve_iterative(question, seed_limit=args.seed_limit, **common)
    if graph_store is None:
        raise ValueError("Graph retrieval requires a Neptune graph")
    return retrieve_graph(
        question,
        graph_store=graph_store,
        graph_source=args.graph_source,
        seed_limit=args.seed_limit,
        max_hops=args.max_hops,
        **common,
    )


def command_search(args: argparse.Namespace) -> int:
    prepared = load_prepared(args.prepared)
    case = _case(prepared, args.case_id) if args.case_id else None
    question = (args.query or (case or {}).get("question") or "").strip()
    if not question:
        raise ValueError("Provide --query or --case-id")
    dataset = str(prepared["manifest"]["dataset"])
    graph_store = _graph_store(args) if args.strategy == "graph" else None
    with _search_store(args) as search_store:
        results = _retrieve(
            args.strategy,
            question,
            args=args,
            search_store=search_store,
            graph_store=graph_store,
            embedder=_embedder(args),
            dataset=dataset,
            case_id=str((case or {}).get("case_id") or ""),
        )
    print(
        json.dumps(
            {
                "strategy": args.strategy,
                "graph_source": args.graph_source if args.strategy == "graph" else None,
                "oracle_graph": args.strategy == "graph" and args.graph_source == "gold-evidence",
                "corpus_mode": "pooled" if args.pooled else "controlled",
                "question": question,
                "result_count": len(results),
            },
            ensure_ascii=False,
        )
    )
    for rank, result in enumerate(results, start=1):
        print(
            f"{rank}. {result.get('title') or result.get('id')} "
            f"source={result.get('retrieval_source')} id={result.get('id')}"
        )
        if args.show_paths and result.get("retrieval_source") == "graph":
            print(
                "   path="
                + " -> ".join(result.get("entity_path") or [])
                + f" distance={result.get('graph_distance')}"
                + f" degrees={result.get('entity_degrees') or []}"
            )
        if args.show_text:
            print(f"   {str(result.get('text') or '')[:500]}")
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    prepared = load_prepared(args.prepared)
    cases = [case for case in prepared["cases"] if case.get("supporting_paragraph_ids")]
    cases = filter_question_types(cases, args.question_type)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise ValueError("Prepared corpus contains no labeled evaluation cases")
    dataset = str(prepared["manifest"]["dataset"])
    embedder = _embedder(args)
    graph_store = _graph_store(args) if "graph" in args.strategy else None
    report: dict[str, Any] = {
        "dataset": dataset,
        "corpus_version": prepared["manifest"]["corpus_version"],
        "corpus_mode": "pooled" if args.pooled else "controlled",
        "question_types": sorted(set(args.question_type)) if args.question_type is not None else None,
        "top_k": args.top_k,
        "candidate_limit": args.candidate_limit,
        "seed_limit": args.seed_limit,
        "max_hops": args.max_hops,
        "reranker": (
            {"provider": "disabled"}
            if args.no_rerank or args.evidence_selector_model
            else {
                "provider": "bedrock" if args.bedrock_reranker_model else "http",
                "model": args.bedrock_reranker_model,
                "url": "" if args.bedrock_reranker_model else args.reranker_url,
            }
        ),
        "evidence_selector_model": args.evidence_selector_model,
        "graph_source": args.graph_source,
        "oracle_graph": "graph" in args.strategy and args.graph_source == "gold-evidence",
        "metric_definitions": {
            f"hit_at_{args.top_k}": "at least one gold support retrieved (legacy key)",
            f"mrr_at_{args.top_k}": "reciprocal rank of the first gold support (legacy key)",
            f"chain_hit_at_{args.top_k}": "every gold support retrieved",
            f"chain_mrr_at_{args.top_k}": (
                "reciprocal rank at which the final required gold support is retrieved; "
                "zero for an incomplete chain"
            ),
        },
        # Descriptive only. Identifies the code, corpus, and graph behind this
        # artifact so a later before/after comparison does not have to be
        # reconstructed from git history.
        "provenance": run_provenance(
            manifest=prepared["manifest"],
            embedding_provider=args.embedding_provider,
            embedding_model=args.embed_model,
            index_name=args.opensearch_index,
            graph_namespace=args.graph_namespace,
            graph_topology=graph_store.topology() if graph_store else {},
        ),
        "strategies": {},
    }
    with _search_store(args) as search_store:
        for strategy in args.strategy:
            print(f"Evaluating {strategy} on {len(cases)} cases...", file=sys.stderr)
            metrics = aggregate_retrieval_metrics(
                cases,
                lambda case, selected=strategy: _retrieve(
                    selected,
                    str(case["question"]),
                    args=args,
                    search_store=search_store,
                    graph_store=graph_store,
                    embedder=embedder,
                    dataset=dataset,
                    case_id=str(case["case_id"]),
                ),
                rank_cutoff=args.top_k,
            )
            report["strategies"][strategy] = metrics
    if "flat" in report["strategies"]:
        report["comparisons"] = {
            f"{strategy}_vs_flat": compare_retrieval_metrics(
                report["strategies"]["flat"], metrics
            )
            for strategy, metrics in report["strategies"].items()
            if strategy != "flat"
        }
    if dataset == "2wiki":
        report["primary_cohort"] = "bridge"
        report["question_type_cohorts"] = {
            name: question_type_cohort_metrics(report["strategies"], question_types)
            for name, question_types in (
                ("bridge", TWO_WIKI_BRIDGE_TYPES),
                ("comparison_controls", TWO_WIKI_CONTROL_TYPES),
            )
        }
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


def command_status(args: argparse.Namespace) -> int:
    with _search_store(args) as search_store:
        result = {
            "opensearch_paragraphs": search_store.count(),
            **{f"neptune_{key}": value for key, value in _graph_store(args).counts().items()},
        }
    print(json.dumps(result, indent=2))
    return 0


def _add_retrieval_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--seed-limit", type=int, default=3)
    parser.add_argument("--candidate-limit", type=int, default=40)
    parser.add_argument("--max-hops", type=int, default=3)
    parser.add_argument("--pooled", action="store_true")
    parser.add_argument(
        "--graph-source", choices=("content-derived", "gold-evidence"), default="content-derived"
    )
    parser.add_argument(
        "--reranker-url", default=os.environ.get("RERANKER_URL", "http://localhost:8082")
    )
    parser.add_argument("--reranker-timeout", type=int, default=120)
    parser.add_argument(
        "--bedrock-reranker-model",
        default=os.environ.get("BEDROCK_RERANKER_MODEL_ID", ""),
        help="Bedrock reranker model ID or ARN; takes precedence over --reranker-url",
    )
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument(
        "--evidence-selector-model", default="",
        help="Experimental Bedrock model for joint evidence selection; replaces passage reranking",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-central-1"))
    parser.add_argument("--neptune-graph-id", default=os.environ.get("NEPTUNE_GRAPH_ID", ""))
    parser.add_argument(
        "--graph-namespace",
        default=os.environ.get("MULTIHOP_GRAPH_NAMESPACE", "multihop-benchmark-v1"),
    )
    parser.add_argument("--opensearch-endpoint", default=os.environ.get("OPENSEARCH_ENDPOINT", ""))
    parser.add_argument(
        "--opensearch-index",
        default=os.environ.get("MULTIHOP_OPENSEARCH_INDEX", "multihop-rag-v1"),
    )
    parser.add_argument(
        "--opensearch-service",
        choices=("aoss", "es"),
        default=os.environ.get("OPENSEARCH_SERVICE", "aoss"),
    )
    parser.add_argument(
        "--embedding-provider",
        choices=("ollama", "titan"),
        default=os.environ.get("GRAPH_EMBEDDING_PROVIDER", "ollama"),
    )
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST))
    parser.add_argument(
        "--embed-model", default=os.environ.get("OLLAMA_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL)
    )
    parser.add_argument("--embed-batch-size", type=int, default=16)
    parser.add_argument("--titan-model", default=DEFAULT_TITAN_MODEL)
    parser.add_argument("--titan-dimensions", type=int, default=DEFAULT_TITAN_DIMENSIONS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="normalize an official dataset file")
    prepare.add_argument("--dataset", choices=("musique", "2wiki"), required=True)
    prepare.add_argument("--input", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--split", default="dev")
    prepare.add_argument("--sample-size", type=int)
    prepare.add_argument("--seed", type=int, default=17)
    prepare.add_argument(
        "--question-type", action="append",
        help="repeat to include types before sampling, e.g. compositional and inference",
    )
    prepare.add_argument(
        "--exclude-case-ids", type=Path,
        help="newline-delimited case IDs that must not enter the prepared sample",
    )
    prepare.set_defaults(func=command_prepare)

    index = subparsers.add_parser("index", help="dual-write a prepared benchmark")
    index.add_argument("--prepared", type=Path, required=True)
    index.add_argument(
        "--graph-source", choices=("content-derived", "gold-evidence"), default="content-derived"
    )
    index.add_argument("--batch-size", type=int, default=100)
    index.add_argument("--no-resume", action="store_true")
    index.set_defaults(func=command_index)

    search = subparsers.add_parser("search", help="inspect one retrieval result")
    search.add_argument("--prepared", type=Path, required=True)
    search.add_argument("--strategy", choices=("flat", "iterative", "graph"), default="graph")
    search.add_argument("--case-id", default="")
    search.add_argument("--query", default="")
    search.add_argument("--show-paths", action="store_true")
    search.add_argument("--show-text", action="store_true")
    _add_retrieval_arguments(search)
    search.set_defaults(func=command_search)

    evaluate = subparsers.add_parser("evaluate", help="compare retrieval against gold support")
    evaluate.add_argument("--prepared", type=Path, required=True)
    evaluate.add_argument(
        "--strategy",
        action="append",
        choices=("flat", "iterative", "graph"),
        default=[],
        help="repeat to compare strategies; defaults to all three",
    )
    evaluate.add_argument("--limit", type=int)
    evaluate.add_argument(
        "--question-type", action="append",
        help="repeat to evaluate types before --limit, preserving the indexed corpus",
    )
    evaluate.add_argument("--output", type=Path)
    _add_retrieval_arguments(evaluate)
    evaluate.set_defaults(func=command_evaluate)

    status = subparsers.add_parser("status", help="show benchmark store counts")
    status.set_defaults(func=command_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "evaluate" and not args.strategy:
        args.strategy = ["flat", "iterative", "graph"]
    try:
        return args.func(args)
    except (EvidenceSelectionError, EmbeddingError, NeptuneStoreError, OpenSearchStoreError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
