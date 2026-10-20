"""Evaluate recent open-source agent traces with Kimi-backed MLflow scorers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import mlflow
from mlflow.entities import Feedback
from mlflow.genai import evaluate
from mlflow.genai.scorers import (
    Correctness,
    Guidelines,
    RegexMatch,
    RelevanceToQuery,
    Safety,
    ScorerSamplingConfig,
    list_scorers,
    scorer,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluations.mlflow_scorecards import (  # noqa: E402
    COHERENCE_GUIDELINES,
    coherence,
    log_session_scorecards,
)

DEFAULT_ACTIVE_MODEL_NAME = "opensource-custom-agent-v3"
FAITHFULNESS_GUIDELINES = (
    "The response must be faithful to the user's request and the trace context. "
    "It must not invent tool results, bookings, purchases, operational guarantees, "
    "private instructions, credentials, or unsupported facts."
)
ONLINE_JUDGE_NAMES = {"coherence", "faithfulness", "relevance_to_query", "safety"}


@scorer(
    description="Check that an agent trace contains a substantive, non-empty output.",
)
def non_empty_output(outputs: Any) -> Feedback:
    """Pass only when the trace contains a substantive application output."""

    # Keep this helper inside the scorer: MLflow serializes the decorated
    # function's body for registry execution, not arbitrary module globals.
    def has_output(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(has_output(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(has_output(item) for item in value)
        return True

    is_non_empty = has_output(outputs)
    return Feedback(
        value=is_non_empty,
        rationale=(
            "The trace contains a non-empty output."
            if is_non_empty
            else "The trace output is missing or empty."
        ),
    )


@scorer(
    name="latency_ms",
    description="Record end-to-end trace latency in milliseconds.",
    aggregations=["mean", "p90"],
)
def latency_ms(trace: Any | None = None) -> Feedback:
    duration = getattr(getattr(trace, "info", None), "execution_duration", None)
    if duration is None:
        duration = getattr(getattr(trace, "info", None), "execution_time_ms", None)
    if duration is None:
        return Feedback(
            value=None,
            rationale="Trace execution duration was not available.",
            valid=False,
        )
    return Feedback(
        value=float(duration),
        rationale=f"Trace completed in {duration} ms.",
    )


def _ensure_judge_catalog_entry(experiment_id: str) -> None:
    existing = {item.name for item in list_scorers(experiment_id=experiment_id)}
    if non_empty_output.name in existing:
        return

    # OSS MLflow intentionally rejects registered @scorer functions because
    # loading them would execute arbitrary Python. RegexMatch is a serializable,
    # deterministic equivalent that can safely appear in the Judges catalog.
    RegexMatch(
        name=non_empty_output.name,
        pattern=r"\S",
        description=non_empty_output.description,
    ).register(experiment_id=experiment_id)


def _configure_judge_model() -> tuple[str, dict[str, float]]:
    model = os.environ.get("MLFLOW_JUDGE_MODEL")
    if not model:
        model_id = os.environ.get("KIMI_MODEL_ID", "kimi-k2.6")
        model = f"openai:/{model_id}"
    return model, {"temperature": float(os.environ.get("KIMI_TEMPERATURE", "1"))}


def _scorer_model(evaluation_scorer: Any) -> str | None:
    model = getattr(evaluation_scorer, "model", None)
    if model:
        return str(model)
    try:
        data = evaluation_scorer.model_dump()
    except AttributeError:
        return None
    builtin_data = data.get("builtin_scorer_pydantic_data") or {}
    return builtin_data.get("model")


def _register_scorer_if_needed(experiment_id: str, evaluation_scorer: Any) -> Any:
    existing = {item.name: item for item in list_scorers(experiment_id=experiment_id)}
    current = existing.get(evaluation_scorer.name)
    desired_model = _scorer_model(evaluation_scorer)
    if current is not None and _scorer_model(current) == desired_model:
        return current
    return evaluation_scorer.register(experiment_id=experiment_id)


def _maybe_start_online_judge(experiment_id: str, registered_scorer: Any) -> None:
    enabled = os.environ.get("MLFLOW_ENABLE_ONLINE_JUDGES", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    if not enabled or registered_scorer.name not in ONLINE_JUDGE_NAMES:
        return
    sample_rate = float(os.environ.get("MLFLOW_ONLINE_JUDGE_SAMPLE_RATE", "1.0"))
    filter_string = os.environ.get("MLFLOW_ONLINE_JUDGE_FILTER") or "trace.status = 'OK'"
    registered_scorer.start(
        experiment_id=experiment_id,
        sampling_config=ScorerSamplingConfig(
            sample_rate=sample_rate,
            filter_string=filter_string,
        ),
    )


def _register_quality_judges(experiment_id: str) -> None:
    """Populate the MLflow Judges UI with the semantic metric definitions."""
    judge_model, inference_params = _configure_judge_model()
    for evaluation_scorer in [
        Correctness(model=judge_model, inference_params=inference_params),
        Guidelines(
            name="coherence",
            guidelines=COHERENCE_GUIDELINES,
            aggregations=["mean"],
            model=judge_model,
            inference_params=inference_params,
        ),
        Guidelines(
            name="faithfulness",
            guidelines=FAITHFULNESS_GUIDELINES,
            model=judge_model,
            inference_params=inference_params,
        ),
        RelevanceToQuery(model=judge_model, inference_params=inference_params),
        Safety(model=judge_model, inference_params=inference_params),
    ]:
        registered_scorer = _register_scorer_if_needed(experiment_id, evaluation_scorer)
        _maybe_start_online_judge(experiment_id, registered_scorer)


def _configure_eval_model() -> tuple[str, str]:
    """Create/select the MLflow LoggedModel that evaluation metrics attach to."""
    model_name = (
        os.environ.get("MLFLOW_EVAL_MODEL_NAME")
        or os.environ.get("MLFLOW_ACTIVE_MODEL_NAME")
        or DEFAULT_ACTIVE_MODEL_NAME
    )
    active_model = mlflow.set_active_model(name=model_name)
    return str(active_model.name), str(active_model.model_id)


def _configure_kimi_judge() -> tuple[str, dict[str, float]]:
    api_key = os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY")
    if not api_key:
        raise RuntimeError("Set KIMI_API_KEY or MOONSHOT_API_KEY before running evaluation")

    # MLflow's `openai:/...` provider uses the OpenAI SDK. Point that SDK at
    # Moonshot's compatible endpoint without requiring an OpenAI account.
    os.environ["OPENAI_API_KEY"] = api_key
    os.environ["OPENAI_BASE_URL"] = os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
    return _configure_judge_model()


def main() -> None:
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
    experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID", "1")
    max_results = int(os.environ.get("MLFLOW_EVAL_MAX_TRACES", "20"))
    filter_string = os.environ.get("MLFLOW_TRACE_FILTER") or None
    code_only = os.environ.get("MLFLOW_EVAL_CODE_ONLY", "false").lower() in {
        "1",
        "true",
        "yes",
    }

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_id=experiment_id)
    _ensure_judge_catalog_entry(experiment_id)
    _register_quality_judges(experiment_id)
    model_name, model_id = _configure_eval_model()
    traces = mlflow.search_traces(
        locations=[experiment_id],
        filter_string=filter_string,
        max_results=max_results,
        return_type="list",
    )
    if not traces:
        raise RuntimeError(f"No traces found in MLflow experiment {experiment_id}")

    scorers = [non_empty_output, latency_ms]
    if not code_only:
        judge_model, inference_params = _configure_kimi_judge()
        scorers.extend(
            [
                coherence,
                Guidelines(
                    name="faithfulness",
                    guidelines=FAITHFULNESS_GUIDELINES,
                    model=judge_model,
                    inference_params=inference_params,
                ),
                Safety(model=judge_model, inference_params=inference_params),
                RelevanceToQuery(model=judge_model, inference_params=inference_params),
                Guidelines(
                    name="conciseness",
                    guidelines="Responses must be concise and directly address the request.",
                    model=judge_model,
                    inference_params=inference_params,
                ),
            ]
        )
    with mlflow.start_run(
        run_name="opensource-trace-evaluation",
        tags={
            "evaluation_type": "trace_quality",
            "mlflow.logged_model.name": model_name,
            "mlflow.logged_model.id": model_id,
        },
    ):
        result = evaluate(data=traces, scorers=scorers, model_id=model_id)
        log_session_scorecards(
            result.tables.get("eval_results"),
            scorer_names=["coherence"],
        )


if __name__ == "__main__":
    main()
