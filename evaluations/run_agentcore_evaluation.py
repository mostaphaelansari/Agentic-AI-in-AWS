"""Run a release-gating AgentCore on-demand dataset evaluation."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

import boto3
from bedrock_agentcore.evaluation import (
    AgentInvokerInput,
    AgentInvokerOutput,
    CloudWatchAgentSpanCollector,
    Dataset,
    EvaluationRunConfig,
    EvaluatorConfig,
    FileDatasetProvider,
    OnDemandEvaluationDatasetRunner,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evaluations" / "agentcore_dataset.json"
DEFAULT_CONFIG = ROOT / "evaluations" / "agentcore_evaluation_config.json"
DEFAULT_RESULTS = ROOT / "evaluation-results" / "agentcore-evaluation.json"

LOGGER = logging.getLogger("agentcore-evaluation")
logging.basicConfig(level=logging.INFO)


def _runtime_id(runtime_arn: str) -> str:
    match = re.fullmatch(r"arn:[^:]+:bedrock-agentcore:[^:]+:\d+:runtime/([^/]+)", runtime_arn)
    if not match:
        raise ValueError(f"Invalid AgentCore Runtime ARN: {runtime_arn}")
    return match.group(1)


def _region_from_arn(runtime_arn: str) -> str:
    return runtime_arn.split(":", 5)[3]


def _load_config(path: Path) -> dict[str, Any]:
    """Load and validate the AgentCore evaluation config."""
    config = json.loads(path.read_text(encoding="utf-8"))
    for group in config.get("evaluator_groups", []):
        for evaluator_id, threshold in group.get("evaluator_thresholds", {}).items():
            if not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
                raise ValueError(f"{evaluator_id} threshold must be a finite number")
            if threshold < 0 or threshold > 1:
                raise ValueError(f"{evaluator_id} threshold must be between 0 and 1")
    config.setdefault("invocation_payload", {})
    return config


def _wait_for_runtime(
    runtime_arn: str,
    region: str,
    timeout_seconds: int,
    poll_seconds: int,
    expected_image_tag: str | None = None,
) -> dict[str, Any]:
    """Wait for the Runtime to be READY and optionally verify its image tag."""
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    runtime_id = _runtime_id(runtime_arn)
    deadline = time.time() + timeout_seconds

    while True:
        response = client.get_agent_runtime(agentRuntimeId=runtime_id)
        status = response.get("status")
        artifact = response.get("agentRuntimeArtifact", {})
        container_uri = artifact.get("containerConfiguration", {}).get(
            "containerUri"
        ) or artifact.get("container_configuration", {}).get("container_uri")
        metadata = {
            "runtime_id": response.get("agentRuntimeId", runtime_id),
            "runtime_name": response.get("agentRuntimeName"),
            "runtime_version": response.get("agentRuntimeVersion"),
            "status": status,
            "container_uri": container_uri,
        }

        if status == "READY":
            if expected_image_tag and expected_image_tag not in str(container_uri):
                raise RuntimeError(
                    "Runtime READY but expected deployment image "
                    f"{expected_image_tag!r} was not found in {container_uri!r}"
                )
            return metadata

        if time.time() >= deadline:
            raise TimeoutError(f"Runtime {runtime_id} did not become READY; last status={status}")
        time.sleep(poll_seconds)


class RequiredCloudWatchSpanCollector(CloudWatchAgentSpanCollector):
    """CloudWatch span collector that fails the evaluation when no spans arrive."""

    def __init__(
        self,
        log_group_name: str,
        region: str = "us-west-2",
        max_wait_seconds: int = 300,
        poll_interval_seconds: int = 30,
    ) -> None:
        super().__init__(
            log_group_name=log_group_name,
            region=region,
            max_wait_seconds=max_wait_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        self._log_group_name = log_group_name
        self._collections: list[dict[str, Any]] = []

    def collect(self, session_id, start_time, end_time):
        spans = CloudWatchAgentSpanCollector.collect(self, session_id, start_time, end_time)
        if not spans:
            raise RuntimeError(f"No CloudWatch spans were collected for session_id={session_id}")
        trace_ids = sorted(
            {
                str(span.get("traceId"))
                for span in spans
                if isinstance(span, dict) and span.get("traceId")
            }
        )
        self._collections.append(
            {
                "session_id": session_id,
                "span_count": len(spans),
                "trace_ids": trace_ids,
            }
        )
        return spans

    def collection_report(self) -> dict[str, Any]:
        return {
            "source": "cloudwatch_logs_insights",
            "log_groups": ["aws/spans", self._log_group_name],
            "collections": self._collections,
        }


def _parse_response_body(body) -> tuple[str, list[dict[str, Any]]]:
    """Read the custom Runtime's SSE response and return answer text plus events."""
    events: list[dict[str, Any]] = []

    text_parts: list[str] = []

    for raw_line in body.iter_lines(chunk_size=1):
        line = (
            raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        )
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        event = json.loads(payload)
        if not isinstance(event, dict):
            continue
        events.append(event)
        if event.get("type") == "text":
            text_parts.append(str(event.get("content", "")))
        elif event.get("type") == "error":
            raise RuntimeError(f"Agent invocation failed: {event.get('content', 'unknown error')}")

    if not events:
        raise RuntimeError("Agent Runtime returned no SSE events")
    if not text_parts:
        raise RuntimeError("Agent Runtime returned no answer text")
    return "".join(text_parts), events


def _build_agent_invoker(
    runtime_arn: str,
    region: str,
    invocation_payload: dict[str, Any],
    current_date: str | None = None,
):
    client = boto3.client("bedrock-agentcore", region_name=region)
    invocation_date = current_date or date.today().isoformat()

    def invoke(invoker_input: AgentInvokerInput) -> AgentInvokerOutput:
        if not invoker_input.session_id:
            raise ValueError("AgentCore evaluation runner did not supply a session ID")

        user_input = invoker_input.payload
        if isinstance(user_input, dict):
            user_input = user_input.get("prompt") or user_input.get("message") or user_input
        if not isinstance(user_input, str):
            user_input = json.dumps(user_input, ensure_ascii=False)

        payload = {
            **invocation_payload,
            "prompt": f"Current date: {invocation_date}\n{user_input}",
        }
        response = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=invoker_input.session_id,
            payload=json.dumps(payload).encode("utf-8"),
        )
        answer, events = _parse_response_body(response["response"])
        metrics = next((event for event in reversed(events) if event.get("type") == "metrics"), {})
        LOGGER.info(
            "session=%s answer_chars=%d tool_names=%s",
            invoker_input.session_id,
            len(answer),
            metrics.get("tool_names", []),
        )
        return AgentInvokerOutput(agent_output=answer)

    return invoke


def _gate_report(
    result_payload: dict,
    evaluator_thresholds: dict[str, float],
    expected_scenario_ids: set[str] | None = None,
) -> dict:
    failures: list[str] = []
    scores: list[dict[str, Any]] = []
    seen_scenario_ids: set[str] = set()

    for scenario in result_payload.get("scenario_results", []):
        scenario_id = scenario.get("scenario_id", "unknown")
        seen_scenario_ids.add(scenario_id)
        if scenario.get("status") != "COMPLETED":
            failures.append(f"{scenario_id}: scenario failed: {scenario.get('error')}")
            continue

        evaluator_results = {
            item.get("evaluator_id"): item for item in scenario.get("evaluator_results", [])
        }
        for evaluator_id, threshold in evaluator_thresholds.items():
            evaluator = evaluator_results.get(evaluator_id)
            if not evaluator:
                failures.append(f"{scenario_id}: missing evaluator result for {evaluator_id}")
                continue

            raw_results = evaluator.get("results", [])
            if not raw_results:
                failures.append(f"{scenario_id}: {evaluator_id} returned no scores")
                continue

            for index, raw in enumerate(raw_results):
                if raw.get("errorCode") or raw.get("errorMessage"):
                    failures.append(
                        f"{scenario_id}: {evaluator_id}[{index}] error: "
                        f"{raw.get('errorCode')} {raw.get('errorMessage')}"
                    )
                    continue
                value = raw.get("value")
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    failures.append(f"{scenario_id}: {evaluator_id}[{index}] has no numeric score")
                    continue
                scores.append(
                    {
                        "scenario_id": scenario_id,
                        "evaluator_id": evaluator_id,
                        "value": value,
                        "threshold": threshold,
                        "label": raw.get("label"),
                    }
                )
                if value < threshold:
                    failures.append(
                        f"{scenario_id}: {evaluator_id} score {value:.3f} is below {threshold:.3f}"
                    )

    if not result_payload.get("scenario_results"):
        failures.append("AgentCore returned no scenario results")
    if expected_scenario_ids:
        missing = sorted(expected_scenario_ids - seen_scenario_ids)
        if missing:
            failures.append(f"Missing scenario results: {', '.join(missing)}")

    return {
        "passed": not failures,
        "scores": scores,
        "failures": failures,
    }


def _filter_dataset(dataset: Dataset, scenario_filter: str) -> Dataset:
    if scenario_filter == "all":
        return dataset
    if scenario_filter == "with_expected_trajectory":
        scenarios = [
            scenario
            for scenario in dataset.scenarios
            if getattr(scenario, "expected_trajectory", None)
        ]
        if not scenarios:
            raise ValueError("No scenarios provide a non-empty expected trajectory")
        return Dataset(scenarios=scenarios)
    raise ValueError(f"Unsupported scenario_filter: {scenario_filter}")


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-arn",
        default=os.environ.get("AGENTCORE_EVALUATION_RUNTIME_ARN"),
        help="Deployed Runtime ARN; defaults to AGENTCORE_EVALUATION_RUNTIME_ARN",
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"))
    parser.add_argument(
        "--log-group",
        default=os.environ.get("AGENTCORE_EVALUATION_LOG_GROUP"),
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULTS)
    args = parser.parse_args()
    if not args.runtime_arn:
        parser.error("--runtime-arn or AGENTCORE_EVALUATION_RUNTIME_ARN is required")
    return args


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _arguments()
    config_values = _load_config(args.config)
    region = args.region or _region_from_arn(args.runtime_arn)
    runtime_id = _runtime_id(args.runtime_arn)
    log_group = args.log_group or f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    evaluator_groups = config_values["evaluator_groups"]
    expected_image_tag = os.environ.get("AGENTCORE_EVALUATION_EXPECTED_IMAGE_TAG")

    LOGGER.info("runtime=%s region=%s log_group=%s", runtime_id, region, log_group)
    try:
        runtime_metadata = _wait_for_runtime(
            args.runtime_arn,
            region,
            config_values["runtime_ready_timeout_seconds"],
            config_values["runtime_ready_poll_seconds"],
            expected_image_tag,
        )
        dataset = FileDatasetProvider(str(args.dataset)).get_dataset()
        span_collector = RequiredCloudWatchSpanCollector(
            log_group_name=log_group,
            region=region,
            max_wait_seconds=config_values["span_max_wait_seconds"],
            poll_interval_seconds=config_values["span_poll_interval_seconds"],
        )
        runner = OnDemandEvaluationDatasetRunner(region=region)
        agent_invoker = _build_agent_invoker(
            args.runtime_arn,
            region,
            config_values.get("invocation_payload", {}),
        )
        runs: list[dict[str, Any]] = []
        all_scores: list[dict[str, Any]] = []
        all_failures: list[str] = []

        for group in evaluator_groups:
            group_name = group["name"]
            thresholds = group["evaluator_thresholds"]
            group_dataset = _filter_dataset(dataset, group["scenario_filter"])
            run_config = EvaluationRunConfig(
                evaluator_config=EvaluatorConfig(evaluator_ids=list(thresholds)),
                evaluation_delay_seconds=config_values["evaluation_delay_seconds"],
                max_concurrent_scenarios=config_values["max_concurrent_scenarios"],
            )
            result = runner.run(
                config=run_config,
                dataset=group_dataset,
                agent_invoker=agent_invoker,
                span_collector=span_collector,
            )
            result_payload = result.model_dump(mode="json")
            expected_scenario_ids = {
                scenario.scenario_id
                for scenario in group_dataset.scenarios
                if getattr(scenario, "scenario_id", None)
            }
            group_gate = _gate_report(result_payload, thresholds, expected_scenario_ids)
            for score in group_gate["scores"]:
                score["group"] = group_name
            all_scores.extend(group_gate["scores"])
            all_failures.extend(f"{group_name}: {failure}" for failure in group_gate["failures"])
            runs.append(
                {
                    "name": group_name,
                    "scenario_filter": group["scenario_filter"],
                    "evaluators": thresholds,
                    "gate": group_gate,
                    "result": result_payload,
                }
            )

        gate = {
            "passed": not all_failures,
            "scores": all_scores,
            "failures": all_failures,
        }
        report = {
            "runtime_arn": args.runtime_arn,
            "runtime": runtime_metadata,
            "region": region,
            "log_group": log_group,
            "dataset": str(args.dataset),
            "evaluator_groups": evaluator_groups,
            "span_collection": span_collector.collection_report(),
            "gate": gate,
            "runs": runs,
        }
    except Exception as exc:
        report = {
            "runtime_arn": args.runtime_arn,
            "region": region,
            "log_group": log_group,
            "dataset": str(args.dataset),
            "evaluator_groups": evaluator_groups,
            "gate": {"passed": False, "scores": [], "failures": [str(exc)]},
        }
        _write_report(args.output, report)
        raise

    _write_report(args.output, report)
    for score in gate["scores"]:
        print(
            f"{score['group']} {score['scenario_id']} {score['evaluator_id']}: "
            f"{score['value']:.3f} >= {score['threshold']:.3f} ({score['label']})"
        )
    if gate["failures"]:
        for failure in gate["failures"]:
            print(f"FAIL: {failure}")
        return 1

    print(f"AgentCore evaluation passed; report={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
