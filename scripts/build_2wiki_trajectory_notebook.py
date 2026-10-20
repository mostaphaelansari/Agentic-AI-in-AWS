#!/usr/bin/env python3
"""Build a self-contained notebook for presenting the frozen 2Wiki run."""

from __future__ import annotations

import base64
import gzip
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "var/multihop/2wiki-20260911"
OUTPUT = ROOT / "notebooks/2Wiki-Retrieval-Trajectory-Explorer.ipynb"
RIDE_CASE_ID = "022a3dd40bdd11eba7f7acde48001122"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def by_case(report: dict[str, Any], strategy: str) -> dict[str, dict[str, Any]]:
    return {
        str(row["case_id"]): row
        for row in report["strategies"][strategy]["cases"]
    }


def graph_adjacency(
    paragraphs: list[dict[str, Any]], mentions: list[dict[str, Any]]
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str]]:
    adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
    labels: dict[str, str] = {}

    def connect(left: str, right: str, predicate: str) -> None:
        adjacency[left].append((right, predicate))
        adjacency[right].append((left, predicate))

    for paragraph in paragraphs:
        paragraph_node = f"p|{paragraph['paragraph_id']}"
        entity_node = f"e|{paragraph['about_entity_id']}"
        labels[paragraph_node] = str(paragraph["title"])
        labels[entity_node] = str(paragraph["about_entity_name"])
        connect(paragraph_node, entity_node, "ABOUT")
    for mention in mentions:
        paragraph_node = f"p|{mention['paragraph_id']}"
        entity_node = f"e|{mention['entity_id']}"
        labels[entity_node] = str(mention["entity_name"])
        connect(paragraph_node, entity_node, "MENTIONS")
    for node in adjacency:
        adjacency[node].sort(key=lambda item: item[0])
    return adjacency, labels


def shortest_paths(
    adjacency: dict[str, list[tuple[str, str]]],
    start: str,
    *,
    max_edges: int,
) -> dict[str, tuple[list[str], list[str]]]:
    queue: deque[tuple[str, list[str], list[str]]] = deque([(start, [start], [])])
    found = {start: ([start], [])}
    while queue:
        node, nodes, predicates = queue.popleft()
        depth = len(predicates)
        if depth >= max_edges:
            continue
        for neighbor, predicate in adjacency.get(node, []):
            if neighbor in found:
                continue
            next_nodes = [*nodes, neighbor]
            next_predicates = [*predicates, predicate]
            found[neighbor] = (next_nodes, next_predicates)
            queue.append((neighbor, next_nodes, next_predicates))
    return found


def best_paths_for_seeds(
    seed_ids: list[str],
    paragraph_ids: list[str],
    adjacency: dict[str, list[tuple[str, str]]],
    labels: dict[str, str],
    *,
    max_hops: int,
    per_seed: int,
) -> dict[str, dict[str, Any]]:
    per_seed_paths: dict[str, list[tuple[int, str, list[str], list[str]]]] = {}
    for seed_id in seed_ids:
        paths = shortest_paths(
            adjacency, f"p|{seed_id}", max_edges=max_hops * 2
        )
        found = []
        for paragraph_id in paragraph_ids:
            if paragraph_id == seed_id:
                continue
            path = paths.get(f"p|{paragraph_id}")
            if path is None:
                continue
            nodes, predicates = path
            found.append((len(predicates), paragraph_id, nodes, predicates))
        per_seed_paths[seed_id] = sorted(found, key=lambda row: (row[0], row[1]))[
            :per_seed
        ]

    best: dict[str, dict[str, Any]] = {}
    for seed_id in seed_ids:
        for distance, paragraph_id, nodes, predicates in per_seed_paths[seed_id]:
            previous = best.get(paragraph_id)
            if previous is not None and int(previous["distance"]) <= distance:
                continue
            best[paragraph_id] = {
                "seed_id": seed_id,
                "distance": distance,
                "nodes": [
                    {
                        "kind": "paragraph" if node.startswith("p|") else "entity",
                        "id": node[2:],
                        "label": labels.get(node, node[2:]),
                    }
                    for node in nodes
                ],
                "predicates": predicates,
            }
    return best


def reconstructed_origins(
    graph_row: dict[str, Any], flat_row: dict[str, Any]
) -> list[str]:
    ids = [str(value) for value in graph_row["retrieved_ids"]]
    seed_count = int(graph_row.get("source_counts", {}).get("seed", 0))
    graph_count = sum(
        int(graph_row.get("source_counts", {}).get(name, 0))
        for name in ("graph", "both")
    )
    fallback_count = int(graph_row.get("source_counts", {}).get("fallback", 0))
    flat_ids = set(str(value) for value in flat_row["retrieved_ids"])
    origins = ["seed"] * seed_count
    cursor = seed_count
    for index in range(max(graph_count, fallback_count)):
        if index < graph_count:
            origins.append("both" if ids[cursor] in flat_ids else "graph")
            cursor += 1
        if index < fallback_count:
            origins.append("fallback")
            cursor += 1
    if len(origins) != len(ids):
        raise AssertionError("Could not reconstruct the Graph RAG mixing order")
    expected = {
        name: int(graph_row.get("source_counts", {}).get(name, 0))
        for name in ("seed", "graph", "both", "fallback")
    }
    actual = {name: origins.count(name) for name in expected}
    if actual != expected:
        raise AssertionError(f"Origin reconstruction mismatch: {actual} != {expected}")
    return origins


def compact_metric(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "retrieved_ids": [str(value) for value in row["retrieved_ids"]],
        "gold_count": int(row["gold_count"]),
        "matched_count": int(row["matched_count"]),
        "support_recall": float(row["support_recall"]),
        "all_support_recalled": bool(row["all_support_recalled"]),
        "chain_completion_rank": int(row["chain_completion_rank"]),
        "source_counts": row.get("source_counts", {}),
        "selected_evidence": row.get("selected_evidence", []),
    }


def payload() -> dict[str, Any]:
    cases = read_jsonl(RUN_DIR / "prepared/cases.jsonl")
    paragraphs = read_jsonl(RUN_DIR / "prepared/paragraphs.jsonl")
    mentions = read_jsonl(RUN_DIR / "prepared/mentions.jsonl")
    pool_report = json.loads((RUN_DIR / "pool-pooled-k40.json").read_text())
    final_report = json.loads((RUN_DIR / "final-pooled-k5.json").read_text())
    pool_flat = by_case(pool_report, "flat")
    pool_graph = by_case(pool_report, "graph")
    final_flat = by_case(final_report, "flat")
    final_graph = by_case(final_report, "graph")
    paragraph_by_id = {str(row["paragraph_id"]): row for row in paragraphs}
    adjacency, labels = graph_adjacency(paragraphs, mentions)
    paragraph_ids = sorted(paragraph_by_id)

    rendered_cases = []
    for case in cases:
        case_id = str(case["case_id"])
        flat40 = pool_flat[case_id]
        graph40 = pool_graph[case_id]
        flat5 = final_flat[case_id]
        graph5 = final_graph[case_id]
        origins = reconstructed_origins(graph40, flat40)
        seed_ids = [
            paragraph_id
            for paragraph_id, origin in zip(graph40["retrieved_ids"], origins)
            if origin == "seed"
        ]
        best_paths = best_paths_for_seeds(
            seed_ids,
            paragraph_ids,
            adjacency,
            labels,
            max_hops=int(pool_report["max_hops"]),
            per_seed=int(pool_report["candidate_limit"]),
        )
        graph_candidates = []
        for rank, (paragraph_id, origin) in enumerate(
            zip(graph40["retrieved_ids"], origins), start=1
        ):
            candidate = {"rank": rank, "id": paragraph_id, "origin": origin}
            if origin in {"graph", "both"}:
                candidate["path"] = best_paths.get(paragraph_id)
            graph_candidates.append(candidate)
        rendered_cases.append(
            {
                "case_id": case_id,
                "question": case["question"],
                "answer": case["answer"],
                "question_type": case["question_type"],
                "hop_count": int(case["hop_count"]),
                "gold_ids": [str(value) for value in case["supporting_paragraph_ids"]],
                "gold_evidences": case.get("gold_evidences", []),
                "flat40": compact_metric(flat40),
                "graph40": {
                    **compact_metric(graph40),
                    "candidates": graph_candidates,
                },
                "flat5": compact_metric(flat5),
                "graph5": compact_metric(graph5),
            }
        )

    return {
        "meta": {
            "case_count": len(cases),
            "paragraph_count": len(paragraphs),
            "candidate_limit": int(pool_report["candidate_limit"]),
            "seed_limit": int(pool_report["seed_limit"]),
            "max_hops": int(pool_report["max_hops"]),
            "final_k": int(final_report["top_k"]),
            "graph_source": pool_report["graph_source"],
            "corpus_version": pool_report["corpus_version"],
        },
        "ride_case_id": RIDE_CASE_ID,
        "paragraphs": {
            paragraph_id: {"title": row["title"]}
            for paragraph_id, row in paragraph_by_id.items()
        },
        "cases": rendered_cases,
    }


SELECTOR_CODE = r'''
import json

import ipywidgets as widgets
import pandas as pd
from IPython.display import clear_output, display

ORIGIN_LABELS = {
    "seed": "OpenSearch seed",
    "graph": "Graph only",
    "both": "Graph + search",
    "fallback": "OpenSearch fallback",
}
PARAGRAPHS = DATA["paragraphs"]
CASES = {case["case_id"]: case for case in DATA["cases"]}
pd.set_option("display.max_rows", 120)
pd.set_option("display.max_colwidth", 140)

questions = pd.DataFrame(
    [
        {
            "number": number,
            "case_id": case["case_id"],
            "type": case["question_type"],
            "hops": case["hop_count"],
            "question": case["question"],
        }
        for number, case in enumerate(DATA["cases"], start=1)
    ]
)
display(questions)

question_picker = widgets.Dropdown(
    options=[(f"{row.number:03d} · {row.question}", row.case_id) for row in questions.itertuples()],
    value=DATA["ride_case_id"],
    description="Question:",
    layout=widgets.Layout(width="100%"),
    style={"description_width": "80px"},
)
selection_output = widgets.Output()


def select_case(case_id):
    global selected_case, seed_params, path_params, full_graph_params, final_params
    selected_case = CASES[case_id]
    graph_candidates = selected_case["graph40"]["candidates"]
    gold_ids = set(selected_case["gold_ids"])
    seeds = [row for row in graph_candidates if row["origin"] == "seed"]
    path_rows = [
        row
        for row in graph_candidates
        if row["origin"] in {"graph", "both"} and row.get("path")
    ]
    trajectory = next(
        (row for row in path_rows if row["id"] in gold_ids),
        path_rows[0] if path_rows else None,
    )
    seed_params = json.dumps(
        {
            "schema": "2wiki-content-v2-20260911",
            "seed_ids": [row["id"] for row in seeds],
        }
    )
    path_params = json.dumps(
        {
            "schema": "2wiki-content-v2-20260911",
            "seed_id": trajectory["path"]["seed_id"] if trajectory else "",
            "candidate_id": trajectory["id"] if trajectory else "",
        }
    )
    full_graph_params = json.dumps(
        {
            "schema": "2wiki-content-v2-20260911",
            "path_pairs": [
                {
                    "seed_id": row["path"]["seed_id"],
                    "candidate_id": row["id"],
                }
                for row in graph_candidates
                if row["origin"] in {"graph", "both"} and row.get("path")
            ],
        }
    )
    final_params = json.dumps(
        {
            "schema": "2wiki-content-v2-20260911",
            "final_ids": selected_case["graph5"]["retrieved_ids"],
        }
    )
    with selection_output:
        clear_output(wait=True)
        print(selected_case["question"])
        print(f"Type: {selected_case['question_type']} · hops: {selected_case['hop_count']}")
        print("Recorded OpenSearch seeds:")
        for row in seeds:
            print(f"  rank {row['rank']}: {PARAGRAPHS[row['id']]['title']}")
        if trajectory:
            labels = " → ".join(node["label"] for node in trajectory["path"]["nodes"])
            print(
                f"Neptune trajectory to display: rank {trajectory['rank']} "
                f"({ORIGIN_LABELS[trajectory['origin']]})"
            )
            print(f"  {labels}")
        print("\nRun the openCypher cells below again after changing the question.")


def on_question_change(change):
    if change["name"] == "value":
        select_case(change["new"])


question_picker.observe(on_question_change)
display(question_picker, selection_output)
select_case(question_picker.value)
'''


CANDIDATES_CODE = r'''
gold_ids = set(selected_case["gold_ids"])
final_ids = set(selected_case["graph5"]["retrieved_ids"])
candidate_table = pd.DataFrame(
    [
        {
            "rank": row["rank"],
            "origin": ORIGIN_LABELS[row["origin"]],
            "title": PARAGRAPHS[row["id"]]["title"],
            "paragraph_id": row["id"],
            "gold evaluation only": "required" if row["id"] in gold_ids else "",
            "kept in final K=5": "yes" if row["id"] in final_ids else "",
            "graph path": " → ".join(
                node["label"] for node in (row.get("path") or {}).get("nodes", [])
            ),
        }
        for row in selected_case["graph40"]["candidates"]
    ]
)
display(candidate_table)

comparison = pd.DataFrame(
    [
        {
            "stage": "Candidate K=40",
            "Flat matched": f"{selected_case['flat40']['matched_count']}/{selected_case['flat40']['gold_count']}",
            "Graph matched": f"{selected_case['graph40']['matched_count']}/{selected_case['graph40']['gold_count']}",
            "Flat complete": selected_case["flat40"]["all_support_recalled"],
            "Graph complete": selected_case["graph40"]["all_support_recalled"],
        },
        {
            "stage": "Final K=5",
            "Flat matched": f"{selected_case['flat5']['matched_count']}/{selected_case['flat5']['gold_count']}",
            "Graph matched": f"{selected_case['graph5']['matched_count']}/{selected_case['graph5']['gold_count']}",
            "Flat complete": selected_case["flat5"]["all_support_recalled"],
            "Graph complete": selected_case["graph5"]["all_support_recalled"],
        },
    ]
)
display(comparison)
'''


def cell(cell_type: str, source: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "cell_type": cell_type,
        "metadata": {},
        "source": source.splitlines(keepends=True),
    }
    if cell_type == "code":
        result.update({"execution_count": None, "outputs": []})
    return result


def build_notebook() -> dict[str, Any]:
    packed = base64.b64encode(
        gzip.compress(
            json.dumps(payload(), ensure_ascii=False, separators=(",", ":")).encode()
        )
    ).decode()
    data_cell = f'''import base64, gzip, json

# The frozen 120-case benchmark data is embedded so this notebook works after
# uploading this single file to SageMaker. Gold data is evaluation-only.
DATA = json.loads(gzip.decompress(base64.b64decode("""{packed}""")))
print(
    f"Loaded {{DATA['meta']['case_count']}} questions and "
    f"{{DATA['meta']['paragraph_count']}} unique passages."
)
'''
    return {
        "cells": [
            cell(
                "markdown",
                """# 2Wiki retrieval trajectory explorer

This notebook presents the **frozen 120-question benchmark run**. It lets you
search or select any question and defaults to:

> Who is the father of the director of film *The Ride (1994 Film)*?

The view separates the retrieval process from evaluation. OpenSearch and
Neptune never receive the gold passage IDs. The gold overlay is applied only
after retrieval so the audience can see which required passages were found.

The graph cells use Neptune's **openCypher** `%%oc` magic and render in the
notebook's native **Graph** tab. GraphQL is an API query language; Neptune
Analytics uses openCypher for this property graph.

Definitions used in the explorer:

- **Seed:** one of the first three OpenSearch passages used as a starting node.
- **Graph only:** a passage reached through Neptune traversal that is absent
  from Flat RAG's recorded top 40.
- **Graph + search:** a passage reached by the graph and also present in Flat
  RAG's top 40.
- **Fallback:** a remaining OpenSearch result interleaved with graph results.
- **K=40 candidate pool:** the forty passages available to the evidence
  selector.
- **K=5 final evidence:** the five passages retained by the shared selector.
- **Gold passage:** a dataset-labeled required passage used only to score the
  result.
""",
            ),
            cell("code", "%load_ext graph_notebook.magics\n"),
            cell("code", data_cell),
            cell("code", SELECTOR_CODE),
            cell(
                "markdown",
                """## 1. Display the three OpenSearch seeds in Neptune

The selected case above supplies the three recorded seed paragraph IDs as
query parameters. Run this cell, then open its **Graph** tab. These are the
OpenSearch starting points; gold labels were not used to choose them.
""",
            ),
            cell(
                "code",
                """%%oc --query-parameters seed_params --display-property title --label-max-length 50
UNWIND $seed_ids AS seed_id
MATCH (seed:BenchmarkParagraph {schema: $schema, id: seed_id})
RETURN seed
ORDER BY seed.id
""",
            ),
            cell(
                "markdown",
                """## 2. Display the actual Neptune traversal path

This query starts from the recorded seed and ends at the recorded graph-arm
candidate. It permits only `ABOUT` and `MENTIONS`, the content-derived edge
types used by this benchmark. For *The Ride*, the native Graph tab shows:

`The Ride passage → Jan Svěrák entity → Jan Svěrák passage`

The passage is reached in two graph edges, which equals one paragraph hop.
""",
            ),
            cell(
                "code",
                """%%oc --query-parameters path_params --display-property title --edge-display-property ~labels --label-max-length 50
MATCH path=(seed:BenchmarkParagraph {schema: $schema, id: $seed_id})
           -[:ABOUT|MENTIONS*1..6]-
           (candidate:BenchmarkParagraph {schema: $schema, id: $candidate_id})
RETURN path
ORDER BY length(path), [node IN nodes(path) | node.key]
LIMIT 1
""",
            ),
            cell(
                "markdown",
                """The Graph tab uses one display property for every node type.
Paragraphs have `title`, while entities have `canonical_name`. Run the next
query to print the readable node and edge sequence beneath the graph.
""",
            ),
            cell(
                "code",
                """%%oc --query-parameters path_params
MATCH path=(seed:BenchmarkParagraph {schema: $schema, id: $seed_id})
           -[:ABOUT|MENTIONS*1..6]-
           (candidate:BenchmarkParagraph {schema: $schema, id: $candidate_id})
RETURN [node IN nodes(path) | coalesce(node.title, node.canonical_name)] AS node_sequence,
       [relation IN relationships(path) | type(relation)] AS edge_sequence,
       length(path) AS graph_edges
ORDER BY graph_edges, [node IN nodes(path) | node.key]
LIMIT 1
""",
            ),
            cell(
                "markdown",
                """## 3. Display the full K=40 retrieval graph

This is the complete **Neptune traversal graph** for the selected question. It
returns every recorded graph-arm candidate and the shortest content-derived
path from the exact seed recorded by retrieval. The separate K=40 table below
contains all forty candidates, including OpenSearch fallbacks. Fallbacks are
not added to this graph because Neptune did not retrieve them.

Open the **Graph** tab and use its full-screen button. The result is larger
than the focused path above, so dragging the three seed nodes apart usually
makes the branches easier to explain.
""",
            ),
            cell(
                "code",
                """%%oc --query-parameters full_graph_params --display-property title --edge-display-property ~labels --label-max-length 50
UNWIND $path_pairs AS pair
MATCH path=(seed:BenchmarkParagraph {schema: $schema})
           -[:ABOUT|MENTIONS*1..6]-
           (candidate:BenchmarkParagraph {schema: $schema})
WHERE seed.id = pair.seed_id
  AND candidate.id = pair.candidate_id
WITH pair, path
ORDER BY pair.candidate_id,
         length(path),
         [node IN nodes(path) | node.key]
WITH pair, head(collect(path)) AS recorded_path
RETURN pair.candidate_id AS candidate_id, recorded_path
ORDER BY candidate_id
""",
            ),
            cell(
                "markdown",
                """## 4. Inspect the recorded K=40 trajectory

This table contains every Graph RAG candidate in rank order. The first three
are seeds. After them, graph-arm candidates and OpenSearch fallbacks are
interleaved until the forty-passage budget is full. The gold column is an
evaluation overlay added after retrieval.
""",
            ),
            cell("code", CANDIDATES_CODE),
            cell(
                "markdown",
                """## 5. Display the final five passages in Neptune

This query returns the five paragraph nodes preserved by the shared selector,
plus any direct content-derived paragraph-to-entity-to-paragraph connections
among them. For *The Ride*, the connected pair is the film passage and the
Jan Svěrák biography.
""",
            ),
            cell(
                "code",
                """%%oc --query-parameters final_params --display-property title --edge-display-property ~labels --label-max-length 50
MATCH (selected:BenchmarkParagraph {schema: $schema})
WHERE selected.id IN $final_ids
OPTIONAL MATCH path=(selected)-[:ABOUT|MENTIONS*1..2]-(other:BenchmarkParagraph {schema: $schema})
WHERE other.id IN $final_ids
RETURN selected, path, other
LIMIT 50
""",
            ),
            cell(
                "markdown",
                """## Presentation order

1. Show the table of all 120 questions and choose *The Ride*.
2. Run the first openCypher cell to show the three seeds in the Graph tab.
3. Run the path query to show how Neptune reaches *Jan Svěrák*.
4. Run the full K=40 graph and expand its Graph tab to full screen.
5. Open the forty-row table and point out the required biography at rank 8.
6. Compare Flat and Graph at K=40 and K=5 in the summary table.
7. Run the final openCypher query to show the evidence retained by the selector.

The origin beside each candidate is reconstructed from the frozen
implementation's pinned-seed/interleaving order and checked against its saved
origin totals. Paths are reconstructed from the same frozen prepared
content graph. The saved aggregate artifact did not retain an origin and path
object beside every individual result.
""",
            ),
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def main() -> None:
    notebook = build_notebook()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"Wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
