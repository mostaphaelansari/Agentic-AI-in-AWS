"""Coverage for run provenance: identifying the graph behind a result.

These fields are descriptive only. The tests pin that they record what they claim
and degrade honestly when the information is unavailable, because a provenance
field that silently reports a wrong or empty value is worse than none.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rag.multihop.neptune import MultihopNeptuneStore  # noqa: E402
from rag.multihop.provenance import (  # noqa: E402
    commit_sha,
    degree_distribution,
    fingerprint,
    run_provenance,
)

MANIFEST = {
    "schema_version": "multihop-v1",
    "corpus_version": "musique-dev-multihop-v1",
    "content_graph_policy": "article-title ABOUT plus candidate-title MENTIONS",
    "sample_seed": 9102026,
    "paragraph_count": 1057,
}


def test_commit_sha_records_a_dirty_tree_rather_than_hiding_it():
    clean = commit_sha(runner=lambda cmd: "abc123" if "rev-parse" in cmd else "")
    assert clean == {"commit_sha": "abc123", "dirty": False}
    dirty = commit_sha(
        runner=lambda cmd: "abc123" if "rev-parse" in cmd else " M rag/multihop/retrieve.py"
    )
    assert dirty == {"commit_sha": "abc123", "dirty": True}


def test_commit_sha_degrades_to_unknown_outside_a_repository():
    def missing(cmd):
        raise OSError("git not found")

    assert commit_sha(runner=missing) == {"commit_sha": "", "dirty": None}
    assert commit_sha(runner=lambda cmd: "") == {"commit_sha": "", "dirty": None}

    def failing(cmd):
        raise subprocess.SubprocessError("timed out")

    assert commit_sha(runner=failing)["dirty"] is None


def test_manifest_fingerprint_ignores_key_order_but_tracks_content():
    reordered = dict(reversed(list(MANIFEST.items())))
    assert fingerprint(MANIFEST) == fingerprint(reordered)
    changed = {**MANIFEST, "content_graph_policy": "corpus-wide titles and phrases"}
    assert fingerprint(changed) != fingerprint(MANIFEST)


def test_degree_distribution_summarizes_the_shape_a_hub_cut_is_judged_on():
    degrees = [1] * 10 + [2, 3, 7, 9, 94]
    summary = degree_distribution(degrees)
    assert summary["entities"] == 15
    assert summary["max"] == 94
    assert summary["median"] == 1
    assert summary["above_6"] == 3
    # One extreme hub must not make the histogram unbounded.
    assert summary["histogram"]["11+"] == 1
    assert summary["histogram"]["1"] == 10
    assert sum(summary["histogram"].values()) == 15


def test_degree_distribution_handles_an_empty_graph():
    assert degree_distribution([])["entities"] == 0
    assert degree_distribution([])["histogram"] == {}


def test_run_provenance_carries_corpus_graph_and_index_identity():
    record = run_provenance(
        manifest=MANIFEST,
        embedding_provider="titan",
        embedding_model="amazon.titan-embed-text-v2:0",
        index_name="multihop-musique",
        graph_namespace="multihop",
        graph_topology={"paragraphs": 1057, "entities": 1372, "paragraph_entity_edges": 2789},
        runner=lambda cmd: "deadbeef" if "rev-parse" in cmd else "",
    )
    assert record["commit_sha"] == "deadbeef"
    assert record["content_graph_policy"] == MANIFEST["content_graph_policy"]
    assert record["sample_seed"] == 9102026
    assert record["prepared_manifest_hash"] == fingerprint(MANIFEST)
    assert record["graph_build"]["entities"] == 1372
    assert record["embedding_model"] == "amazon.titan-embed-text-v2:0"
    assert record["index_name"] == "multihop-musique"


def test_topology_reports_edges_and_degrees_alongside_node_counts():
    store = object.__new__(MultihopNeptuneStore)
    store.namespace = "test"
    queries = []

    def execute(query, **params):
        queries.append(query)
        if "size((entity)--()) AS degree" in query:
            return [{"degree": d} for d in (1, 1, 2, 8, 94)]
        return [{"paragraphs": 1057, "entities": 5, "cases": 60}]

    store._execute = execute
    topology = store.topology()
    assert topology["paragraphs"] == 1057
    assert topology["paragraph_entity_edges"] == 106  # the degree sum, counted once
    assert topology["degrees"]["max"] == 94
    assert topology["degrees"]["above_6"] == 2
    # Structure only: no benchmark label may enter a provenance query.
    assert not any("supporting" in query or "gold" in query for query in queries)


def test_run_provenance_records_an_absent_graph_without_inventing_counts():
    record = run_provenance(
        manifest=MANIFEST,
        embedding_provider="ollama",
        embedding_model="nomic-embed-text",
        index_name="multihop-musique",
        graph_namespace="multihop",
        runner=lambda cmd: "",
    )
    # A flat-only run has no graph; that must read as absent, never as zero.
    assert record["graph_build"] == {}
