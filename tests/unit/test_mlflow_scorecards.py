from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evaluations.mlflow_scorecards import (  # noqa: E402
    build_session_scorecards,
    label_coherence_score,
    parse_coherence_judge_response,
    session_id_from_eval_row,
)


def test_parse_coherence_judge_response_bounds_score_and_keeps_feedback():
    score, feedback = parse_coherence_judge_response(
        json.dumps(
            {
                "score": 1.2,
                "feedback": "Completely yes",
                "rationale": "The response is coherent.",
            }
        )
    )

    assert score == 1.0
    assert feedback == "Completely yes: The response is coherent."


def test_label_coherence_score_matches_aws_style_buckets():
    assert label_coherence_score(1.0) == "Completely yes"
    assert label_coherence_score(0.75) == "Generally yes"
    assert label_coherence_score(0.5) == "Partially"
    assert label_coherence_score(0.25) == "No"


def test_session_id_prefers_trace_session_then_case_id_then_trace_id():
    assert (
        session_id_from_eval_row(
            {
                "trace_id": "tr-1",
                "tags": {"mlflow.trace.session": "session-1", "case_id": "case-1"},
            }
        )
        == "session-1"
    )
    assert session_id_from_eval_row({"trace_id": "tr-2", "tags": {"case_id": "case-2"}}) == "case-2"
    assert session_id_from_eval_row({"trace_id": "tr-3", "tags": {}}) == "tr-3"


def test_build_session_scorecards_returns_lowest_and_highest_scores():
    rows = [
        {
            "trace_id": "tr-high",
            "tags": {"case_id": "high"},
            "coherence/value": 1.0,
            "coherence/rationale": "Completely yes",
        },
        {
            "trace_id": "tr-low",
            "tags": {"case_id": "low"},
            "coherence/value": 0.75,
            "coherence/rationale": "Generally yes",
        },
    ]

    report = build_session_scorecards(rows, scorer_names=["coherence"], limit=1)

    assert report["coherence"]["lowest_scores"] == [
        {
            "session_id": "low",
            "score": 0.75,
            "judge_feedback": "Generally yes",
            "trace_id": "tr-low",
        }
    ]
    assert report["coherence"]["highest_scores"] == [
        {
            "session_id": "high",
            "score": 1.0,
            "judge_feedback": "Completely yes",
            "trace_id": "tr-high",
        }
    ]
