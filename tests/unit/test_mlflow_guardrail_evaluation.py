from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evaluations.run_mlflow_guardrail_evaluation import (  # noqa: E402
    EVAL_DATASET,
    apply_guardrails,
    guardrail_decision_accuracy,
    guardrail_output_safety,
    guardrail_redaction_coverage,
)


def test_guardrail_evaluation_fixtures_pass_all_scorers():
    scorers = [
        guardrail_decision_accuracy,
        guardrail_redaction_coverage,
        guardrail_output_safety,
    ]
    for row in EVAL_DATASET:
        outputs = apply_guardrails(**row["inputs"])
        for evaluation_scorer in scorers:
            feedback = evaluation_scorer(
                outputs=outputs,
                expectations=row["expectations"],
            )
            assert feedback.value is True, (
                row["inputs"],
                evaluation_scorer.name,
                feedback.rationale,
            )


def test_guardrail_evaluation_contains_allow_block_redaction_and_tool_cases():
    outputs = [apply_guardrails(**row["inputs"]) for row in EVAL_DATASET]
    assert any(output["allowed"] for output in outputs)
    assert any(not output["allowed"] for output in outputs)
    assert any(output["redactions"] for output in outputs)
    assert any(row["inputs"]["case_type"] == "tool" for row in EVAL_DATASET)
