import json
from types import SimpleNamespace

import pytest

from rag.multihop.retrieve import _finalize
from rag.multihop.selection import EvidenceSelectionError, select_evidence


def response(indices, quotes=None):
    quotes = quotes or ['a', 'b']
    return {"stopReason": "tool_use", "output": {"message": {"content": [
        {"toolUse": {"name": "submit_evidence", "toolUseId": "t0",
                     "input": {"indices": indices, "evidence": [
                         {"index": i, "quote": quotes[i], "role": "bridge relationship"}
                         for i in indices if type(i) is int and 0 <= i < len(quotes)
                     ]}}}
    ]}}}


def test_joint_selection_uses_only_question_and_passage_content():
    captured = {}
    def converse(**kwargs):
        captured.update(kwargs)
        return response([1, 0], ['bridge fact', 'second fact'])
    candidates = [
        {"id": "a", "title": "A", "text": "bridge fact", "answer": "GOLD_SECRET", "is_supporting": True},
        {"id": "b", "title": "B", "text": "second fact", "question_decomposition": ["GOLD_SECRET"]},
    ]
    selected = select_evidence("Where?", candidates, 2, model_id="test", region="test",
                               client=SimpleNamespace(converse=converse))
    assert [r['id'] for r in selected] == ['b', 'a']
    assert all(r['evidence_selector_applied'] for r in selected)
    assert 'GOLD_SECRET' not in json.dumps(captured)
    assert candidates[0].get('evidence_selector_applied') is None


def test_joint_selection_passes_specific_content_graph_paths_but_suppresses_hubs():
    captured = {}
    def converse(**kwargs):
        captured.update(kwargs)
        return response([0, 1], ['one', 'two'])
    candidates = [
        {"id": "a", "title": "A", "text": "one", "entity_path": ["A", "Bridge", "B"],
         "entity_degrees": [2]},
        {"id": "b", "title": "B", "text": "two", "entity_path": ["B", "Common hub", "C"],
         "entity_degrees": [20], "gold_path": "GOLD_SECRET"},
    ]

    select_evidence("Where?", candidates, 2, model_id="test", region="test",
                    client=SimpleNamespace(converse=converse))

    payload = json.loads(captured["messages"][0]["content"][0]["text"])
    assert payload["passages"][0]["graph_connection"] == "A -> Bridge -> B"
    assert "graph_connection" not in payload["passages"][1]
    assert "GOLD_SECRET" not in json.dumps(captured)


@pytest.mark.parametrize('indices', [[0, 0], [0, 3], [True, 1], [0], '0,1'])
def test_joint_selection_rejects_invalid_evidence_sets(indices):
    with pytest.raises(EvidenceSelectionError):
        select_evidence('q', [{'id': 'a'}, {'id': 'b'}], 2, model_id='test', region='test',
                        client=SimpleNamespace(converse=lambda **kwargs: response(indices)))


def test_joint_selection_retries_with_the_violation_fed_back():
    calls = []
    def converse(**kwargs):
        calls.append(kwargs['messages'])
        return response([1, 1] if len(calls) == 1 else [1, 0])
    selected = select_evidence('q', [{'id': 'a', 'text': 'a'}, {'id': 'b', 'text': 'b'}], 2, model_id='test',
                               region='test', client=SimpleNamespace(converse=converse))
    assert [r['id'] for r in selected] == ['b', 'a']
    assert [r['evidence_selector_attempts'] for r in selected] == [2, 2]
    # The retry must carry the rejected submission and say what was wrong with it.
    assert len(calls[0]) == 1 and len(calls[1]) == 3
    error = calls[1][2]['content'][0]['toolResult']
    assert error['status'] == 'error' and error['toolUseId'] == 't0'
    assert 'distinct' in error['content'][0]['text']


def test_joint_selection_gives_up_after_max_attempts():
    calls = []
    def converse(**kwargs):
        calls.append(kwargs)
        return response([0, 0])
    with pytest.raises(EvidenceSelectionError, match='after 3 attempts'):
        select_evidence('q', [{'id': 'a'}, {'id': 'b'}], 2, model_id='test', region='test',
                        client=SimpleNamespace(converse=converse))
    assert len(calls) == 3


def test_joint_selection_does_not_fall_back_silently():
    def fail(**kwargs):
        raise RuntimeError('service unavailable')
    with pytest.raises(EvidenceSelectionError, match='service unavailable'):
        select_evidence('q', [{'id': 'a'}], 1, model_id='test', region='test',
                        client=SimpleNamespace(converse=fail))


def test_joint_selection_replaces_pairwise_reranking(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('pairwise reranker must not run')
    monkeypatch.setattr('rag.multihop.retrieve.bedrock_rerank', unexpected)
    monkeypatch.setattr('rag.multihop.retrieve.select_evidence', lambda q, c, k, **kw: [c[1]])
    assert _finalize('q', [{'id': 'a'}, {'id': 'b'}], top_k=1,
                     evidence_selector_model='selector', bedrock_reranker_model='reranker') == [{'id': 'b'}]
