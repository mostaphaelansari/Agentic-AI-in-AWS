"""MLflow GenAI scorers and scorecard artifacts for session-level review."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import mlflow
import openai
from mlflow.entities import Feedback
from mlflow.genai.scorers import scorer

COHERENCE_GUIDELINES = (
    "The response must be coherent: logically ordered, internally consistent, "
    "easy to follow, and directly connected to the user's request. It should "
    "not contradict itself, jump between unrelated topics, or provide a "
    "fragmented answer."
)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def label_coherence_score(score: float) -> str:
    if score >= 0.95:
        return "Completely yes"
    if score >= 0.75:
        return "Generally yes"
    if score >= 0.5:
        return "Partially"
    return "No"


def parse_coherence_judge_response(content: str) -> tuple[float, str]:
    """Parse a Kimi JSON judge response into a bounded score and feedback label."""
    match = re.search(r"\{[\s\S]*\}", content)
    payload = json.loads(match.group(0) if match else content)
    score = float(payload["score"])
    score = max(0.0, min(1.0, score))
    feedback = str(payload.get("feedback") or label_coherence_score(score)).strip()
    rationale = str(payload.get("rationale") or "").strip()
    if rationale and feedback not in rationale:
        feedback = f"{feedback}: {rationale}"
    return score, feedback


@scorer(
    name="coherence",
    description="Score whether the response is logically ordered and internally consistent.",
    aggregations=["mean"],
)
def coherence(inputs: Any = None, outputs: Any = None) -> Feedback:
    """Kimi-backed numeric coherence judge, shaped like AgentCore evaluator scores."""
    api_key = os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY")
    if not api_key:
        raise RuntimeError("Set KIMI_API_KEY or MOONSHOT_API_KEY to run coherence scoring")

    client = openai.OpenAI(
        api_key=api_key,
        base_url=os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1"),
    )
    response = client.chat.completions.create(
        model=os.environ.get("KIMI_MODEL_ID", "kimi-k2.6"),
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an evaluation judge. Return only valid JSON with "
                    'keys "score", "feedback", and "rationale". The score must '
                    "be a number from 0.0 to 1.0. Use feedback labels like "
                    '"Completely yes", "Generally yes", "Partially", or "No".'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Guideline:\n{COHERENCE_GUIDELINES}\n\n"
                    f"User input:\n{_stringify(inputs)[:6000]}\n\n"
                    f"Agent output:\n{_stringify(outputs)[:6000]}\n\n"
                    "Judge only coherence, not factual correctness."
                ),
            },
        ],
        temperature=float(os.environ.get("KIMI_TEMPERATURE", "1")),
    )
    content = response.choices[0].message.content or ""
    score, feedback = parse_coherence_judge_response(content)
    return Feedback(value=score, rationale=feedback)


def _score_to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trace_dict(row: dict[str, Any]) -> dict[str, Any]:
    trace = row.get("trace")
    if isinstance(trace, dict):
        return trace
    if isinstance(trace, str) and trace:
        try:
            return json.loads(trace)
        except json.JSONDecodeError:
            return {}
    return {}


def session_id_from_eval_row(row: dict[str, Any]) -> str:
    trace = _trace_dict(row)
    info = trace.get("info", {}) if isinstance(trace.get("info"), dict) else {}
    tags = row.get("tags") if isinstance(row.get("tags"), dict) else info.get("tags", {})
    if not isinstance(tags, dict):
        tags = {}
    metadata = (
        row.get("trace_metadata")
        if isinstance(row.get("trace_metadata"), dict)
        else info.get("trace_metadata", {})
    )
    if not isinstance(metadata, dict):
        metadata = {}
    for key in (
        "mlflow.trace.session",
        "mlflow.trace.session_id",
        "session_id",
        "case_id",
        "mlflow.traceName",
    ):
        if tags.get(key):
            return str(tags[key])
        if metadata.get(key):
            return str(metadata[key])
    return str(row.get("trace_id") or info.get("trace_id") or "unknown-session")


def build_session_scorecards(
    eval_results: Any,
    *,
    scorer_names: list[str],
    limit: int = 10,
) -> dict[str, Any]:
    rows = eval_results.to_dict("records") if hasattr(eval_results, "to_dict") else eval_results
    report: dict[str, Any] = {}
    for scorer_name in scorer_names:
        value_key = f"{scorer_name}/value"
        rationale_key = f"{scorer_name}/rationale"
        scored_rows = []
        for row in rows or []:
            score = _score_to_float(row.get(value_key))
            if score is None:
                continue
            scored_rows.append(
                {
                    "session_id": session_id_from_eval_row(row),
                    "score": round(score, 4),
                    "judge_feedback": row.get(rationale_key) or label_coherence_score(score),
                    "trace_id": row.get("trace_id"),
                }
            )
        ordered = sorted(scored_rows, key=lambda item: (item["score"], item["session_id"]))
        report[scorer_name] = {
            "lowest_scores": ordered[:limit],
            "highest_scores": list(reversed(ordered[-limit:])),
        }
    return report


def log_session_scorecards(
    eval_results: Any,
    *,
    scorer_names: list[str],
    limit: int = 10,
    artifact_dir: str = "session_scorecards",
) -> dict[str, Any]:
    report = build_session_scorecards(eval_results, scorer_names=scorer_names, limit=limit)
    mlflow.log_dict(report, f"{artifact_dir}/top_bottom_sessions.json")
    for scorer_name, scorer_report in report.items():
        mlflow.log_dict(
            {"rows": scorer_report["lowest_scores"]},
            f"{artifact_dir}/{scorer_name}_lowest_scores.json",
        )
        mlflow.log_dict(
            {"rows": scorer_report["highest_scores"]},
            f"{artifact_dir}/{scorer_name}_highest_scores.json",
        )
    return report
