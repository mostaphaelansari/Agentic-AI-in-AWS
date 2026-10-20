"""Replace bootstrap utilities with repeated measured Haiku/Sonnet results.

Input is JSONL with repeated records containing ``task_id``, ``model_name``,
``quality_score``, and optional response/cost/token/latency fields. The output
keeps LLMRouter's paired routing schema but makes ``performance`` a value
decision utility so KNN learns the least costly candidate that clears quality.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

MODELS = ("cheap", "strong")
QUALITY_THRESHOLDS = {"low": 0.85, "medium": 0.90, "high": 0.94, "critical": 0.97}


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _lower_confidence_bound(values: list[float]) -> float:
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean
    return max(0.0, mean - 1.96 * statistics.stdev(values) / math.sqrt(len(values)))


def promote(
    gold_records: list[dict], measurements: list[dict], min_runs: int = 3
) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in measurements:
        model_name = str(record.get("model_name", ""))
        if model_name not in MODELS:
            raise ValueError(f"Unknown model_name: {model_name!r}")
        score = float(record["quality_score"])
        if not 0.0 <= score <= 1.0:
            raise ValueError("quality_score must be between zero and one")
        grouped[(str(record["task_id"]), model_name)].append(record)

    output: list[dict] = []
    for gold in gold_records:
        task_id = str(gold["task_id"])
        per_model = {model: grouped[(task_id, model)] for model in MODELS}
        for model, records in per_model.items():
            if len(records) < min_runs:
                raise ValueError(f"{task_id}/{model} has {len(records)} runs; need {min_runs}")

        summaries = {}
        for model, records in per_model.items():
            quality = [float(item["quality_score"]) for item in records]
            costs = [float(item.get("cost_usd", 0.0)) for item in records]
            summaries[model] = {
                "quality_mean": statistics.fmean(quality),
                "quality_lcb95": _lower_confidence_bound(quality),
                "cost_mean_usd": statistics.fmean(costs),
                "runs": len(records),
            }

        threshold = QUALITY_THRESHOLDS[str(gold["risk"])]
        policy_requires_strong = str(gold["risk"]) in {"high", "critical"}
        cheap_is_acceptable = summaries["cheap"]["quality_lcb95"] >= threshold
        winner = "cheap" if cheap_is_acceptable and not policy_requires_strong else "strong"
        utilities = {winner: 1.0, "strong" if winner == "cheap" else "cheap": 0.985}

        for model in MODELS:
            last_run = per_model[model][-1]
            record = dict(gold)
            record.update(
                {
                    "model_name": model,
                    "response": str(last_run.get("response", "")),
                    "token_num": int(last_run.get("input_tokens", 0))
                    + int(last_run.get("output_tokens", 0)),
                    "input_tokens": int(last_run.get("input_tokens", 0)),
                    "output_tokens": int(last_run.get("output_tokens", 0)),
                    "response_time": float(last_run.get("response_time", 0.0)),
                    "api_key_used": "",
                    "performance": utilities[model],
                    "quality_score": summaries[model]["quality_mean"],
                    "quality_lcb95": summaries[model]["quality_lcb95"],
                    "cost_mean_usd": summaries[model]["cost_mean_usd"],
                    "evaluation_runs": summaries[model]["runs"],
                    "quality_threshold": threshold,
                    "value_winner": winner,
                    "score_type": "measured_value_utility",
                    "label_source": "repeated_model_evaluation",
                    "measured": True,
                    "user_id": None,
                    "fig_id": None,
                }
            )
            output.append(record)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--measurements", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-runs", type=int, default=3)
    args = parser.parse_args()
    promoted = promote(
        _read_jsonl(args.gold),
        _read_jsonl(args.measurements),
        min_runs=args.min_runs,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for record in promoted:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"Wrote {len(promoted)} paired routing records to {args.output}")


if __name__ == "__main__":
    main()
