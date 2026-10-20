# AgentCore Custom Runtime Evaluation

## Tool parity contract

Parity benchmark runs expose the same functional catalog in all three paths:

- `webSearch`, `searchMapPlaces`, and `findNearbyPlaces` come from the shared
  `opensource/mcp-tools` MCP server. `searchLocation` and `getForecast` use the
  same Open-Meteo contract in both deployments. The AWS agents reach these
  tools through the same AgentCore Gateway stack; the local open-source agent
  reaches its MCP server directly. Weather is disabled on the hosted MCP copy
  because Gateway already supplies it, preventing duplicate AWS tool entries.
- Hotel, flight, and Geoapify tools are enabled only when their matching shared
  credentials/targets are configured. Use the same credential set everywhere.
- Every path exposes a `code_interpreter` capability.
- AgentCore Browser is disabled by default. Enable it only in a separately
  labelled native-capabilities benchmark, never in a parity result.

`tests/unit/test_benchmark_tool_parity.py` guards this deployment wiring and
fails CI if an AWS path stops using the shared Gateway, browser becomes enabled
by default, or a core MCP/code-execution capability disappears.

This directory contains a real Amazon Bedrock AgentCore on-demand dataset evaluation for the deployed **Custom Runtime**. It uses the same golden cases as the MLflow dataset, represented in AgentCore's `scenarios` schema. It is a release-blocking smoke suite rather than a claim of complete agent quality. It:

1. Invokes the deployed custom AgentCore Runtime for every scenario in `agentcore_dataset.json`.
2. Waits for the Runtime's OpenTelemetry spans to reach CloudWatch.
3. Collects the session spans through `CloudWatchAgentSpanCollector`.
4. Calls AgentCore Evaluations with built-in correctness, goal-success, and trajectory evaluators.
5. Fails the workflow when a scenario, evaluator, or configured score threshold fails.

The generated report also records the source revision, dataset/configuration hashes, Runtime version, container URI, and evaluation timestamp. After deployment, the runner verifies that the Runtime container tag matches the Git commit being evaluated.

Run it from the repository root:

```bash
export AGENTCORE_EVALUATION_RUNTIME_ARN='arn:aws:bedrock-agentcore:eu-west-1:ACCOUNT:runtime/RUNTIME_ID'
export AWS_REGION=eu-west-1
uv run python evaluations/run_agentcore_evaluation.py
```

The default Runtime log group is derived as:

```text
/aws/bedrock-agentcore/runtimes/RUNTIME_ID-DEFAULT
```

Override it with `AGENTCORE_EVALUATION_LOG_GROUP` when the endpoint or log-group name differs.

To duplicate the same Runtime spans into MLflow, configure an OTel Collector fan-out
(`infrastructure/otel/README.md`). The collector must keep exporting to CloudWatch
(`aws/spans`) so this evaluation gate continues to work.

## Prerequisites

- The Runtime is deployed, ready, and instrumented for AgentCore Observability.
- CloudWatch Transaction Search is enabled.
- The caller can invoke the Runtime, query `aws/spans` and the Runtime log group, call `Evaluate`, and read evaluator metadata.
- The GitHub `dev` environment defines `CUSTOM_AGENT_RUNTIME_ARN`.
- The GitHub AWS role includes Runtime invocation, Runtime metadata read, evaluator read/evaluate, and CloudWatch Logs Insights query permissions.

Dataset evaluation is model- and service-backed. It creates AgentCore Runtime, Gateway, model, CloudWatch Logs Insights, and AgentCore Evaluations usage; it is not a free local unit test. AgentCore dataset evaluation is currently documented as public preview, so the SDK/API contract may change.

## What The Gate Proves

- All shared golden scenarios completed and returned all expected evaluator results.
- Semantic scores met the configured thresholds.
- The Paris weather path used the exact observed Open-Meteo tool order.
- Agent invocation errors, span-collection failures, evaluator errors, missing scenarios, duplicate results, invalid scores, and threshold failures block the job.
- The post-deployment workflow evaluated the expected Git-SHA-tagged container.

## Important Boundaries

- The weather cases use live Open-Meteo data. Their expected answers therefore define semantic invariants, not fixed temperatures or conditions.
- The semantic and trajectory groups are separate AgentCore runs because the service rejects an empty expected trajectory for the missing-location case. Their scores come from different sessions.
- The thresholds are initial smoke thresholds, not yet calibrated against a human-labeled benchmark.
- One stochastic run per scenario does not measure reliability distributions.
- The `main` CI job evaluates the Runtime currently deployed in `dev`; only the post-deployment workflow binds the evaluated container tag to `${{ github.sha }}`.
- This runner does not evaluate the Managed Harness. Managed needs its own invocation adapter and backing Runtime log-group discovery.

The next maturity step is a larger frozen-fixture dataset, repeated runs, baseline-versus-candidate comparison, calibrated thresholds, and a dedicated least-privilege evaluation role.

## MLflow Golden Dataset

`datasets/opensource_golden_v1.json` is the curated golden dataset for the
open-source agent. `agentcore_dataset.json` mirrors the same cases in
AgentCore's required scenario shape. The MLflow file follows MLflow's GenAI
dataset shape: each record has
`inputs`, `expectations`, and review-friendly `tags`. The expectations are
semantic instead of brittle exact strings, because several scenarios rely on
live weather, maps, booking, flight, or web-search data.

Sync it into the local MLflow experiment:

```bash
export MLFLOW_TRACKING_URI='http://localhost:5000'
export MLFLOW_EXPERIMENT_ID='1'
uv run --group mlflow-evaluation python evaluations/run_mlflow_golden_dataset.py
```

This creates or updates the managed dataset named `opensource-golden-v1`, so it
appears in MLflow's dataset UI and can be reused across evaluation runs. The
runner also creates or selects the LoggedModel named
`opensource-custom-agent-v3` by default and registers the semantic judges
`correctness`, `faithfulness`, `relevance_to_query`, and `safety` so they appear
in the MLflow **Judges** page. The registered LLM judges use
`MLFLOW_JUDGE_MODEL` or `openai:/${KIMI_MODEL_ID:-kimi-k2.6}` by default, with
Moonshot/Kimi exposed through OpenAI-compatible environment variables.

To evaluate the running open-source agent against the golden dataset:

```bash
export MLFLOW_GOLDEN_EVALUATE=true
export MLFLOW_GOLDEN_AGENT_URL='http://localhost:8080/invocations'
uv run --group mlflow-evaluation python evaluations/run_mlflow_golden_dataset.py
```

By default the evaluation uses deterministic code scorers for required terms,
forbidden claims, refusal boundaries, and `latency_ms`. Add Kimi-backed MLflow
judges when you want the semantic quality metrics: `correctness`,
`faithfulness`, `relevance_to_query`, and `safety`.
Evaluation runs are linked to the active LoggedModel, so aggregate metrics show
on the model version page as well as in the evaluation run.

```bash
export KIMI_API_KEY='...'
export MLFLOW_GOLDEN_USE_LLM_JUDGE=true
uv run --group mlflow-evaluation python evaluations/run_mlflow_golden_dataset.py
```

To turn on automatic future-trace monitoring in the MLflow Judges UI for
`faithfulness`, `relevance_to_query`, and `safety`, set:

```bash
export MLFLOW_ENABLE_ONLINE_JUDGES=true
export MLFLOW_ONLINE_JUDGE_SAMPLE_RATE='1.0'
uv run --group mlflow-evaluation python evaluations/run_mlflow_golden_dataset.py
```

`correctness` is intentionally not enabled for arbitrary future traces because
it requires `expected_response` or `expected_facts`; run it against the golden
dataset instead. In Compose, put `KIMI_API_KEY` in `opensource/compose/.env` so
the MLflow server can execute UI-triggered LLM judges.

See MLflow's official GenAI datasets documentation:
https://mlflow.org/docs/latest/genai/datasets

### Evaluate Existing Agent Traces

`run_mlflow_trace_evaluation.py` evaluates recent traces already stored in
experiment `1`. It always runs the custom code judges `non_empty_output` and
`latency_ms` and, by default, also uses Kimi for MLflow's `Safety`,
`RelevanceToQuery`, a numeric `coherence` judge, a `faithfulness` guidelines
judge, and conciseness scorers.
It does not invoke the agent again:

```bash
export KIMI_API_KEY='...'
export MLFLOW_TRACKING_URI='http://localhost:5000'
export MLFLOW_EXPERIMENT_ID='1'
export MLFLOW_EVAL_MAX_TRACES='20'
uv run --group mlflow-evaluation python evaluations/run_mlflow_trace_evaluation.py
```

Set `MLFLOW_TRACE_FILTER` to an MLflow trace filter when only a subset should
be evaluated. `KIMI_BASE_URL`, `KIMI_MODEL_ID`, and `KIMI_TEMPERATURE` use the
same defaults as the open-source agent. The runner links each evaluation run to
`MLFLOW_EVAL_MODEL_NAME` or `MLFLOW_ACTIVE_MODEL_NAME`, defaulting to
`opensource-custom-agent-v3`, so the model page displays the evaluation metrics
and linked runs.

For an AWS-style bottom/top scoring session view, run against real agent
traces, for example:

```bash
export MLFLOW_TRACE_FILTER="trace.name = 'chat_completion' AND trace.status = 'OK'"
uv run --group mlflow-evaluation python evaluations/run_mlflow_trace_evaluation.py
```

The run logs `session_scorecards/top_bottom_sessions.json` plus separate
`coherence_lowest_scores.json` and `coherence_highest_scores.json` artifacts
with `session_id`, `score`, `judge_feedback`, and `trace_id` columns.

The code judges are deterministic, local, and do not call Kimi or AWS. To run
only those judges without any model-backed evaluation cost:

```bash
export MLFLOW_EVAL_CODE_ONLY=true
uv run --group mlflow-evaluation python evaluations/run_mlflow_trace_evaluation.py
```

Its `@scorer` function returns an MLflow `Feedback` value and rationale for
each trace, so its results appear alongside model-backed scorer results in the
evaluation UI. The runner also creates a safe deterministic `RegexMatch`
equivalent named `non_empty_output` in the experiment's **Judges** catalog.
MLflow OSS intentionally does not register arbitrary Python `@scorer` code
because loading a registered function would require arbitrary code execution.

The open-source Streamlit UI also writes the human `user_satisfaction`
assessment (`true` for thumbs up, `false` for thumbs down) directly onto each
MLflow trace. Use that assessment in the Traces view to compare human ratings
with automated judge feedback and to select poorly rated traces for the next
golden-dataset revision.

### Evaluate Guardrails

`run_mlflow_guardrail_evaluation.py` runs a frozen, deterministic dataset
against the same `guard_prompt` and `guard_tool_call` functions used by the
open-source service. It covers normal traffic, instruction override,
system-prompt exfiltration, malware requests, prompt-size limits, PII and
credential redaction, safe Python, unsafe subprocess use, and redaction before
an external tool call.

```bash
export MLFLOW_TRACKING_URI='http://localhost:5000'
export MLFLOW_EXPERIMENT_ID='1'
uv run --group mlflow-evaluation python evaluations/run_mlflow_guardrail_evaluation.py
```

The evaluation uses three custom code scorers—decision accuracy, redaction
coverage, and output safety—and requires neither Kimi nor AWS. Results are
logged under the run name `opensource-guardrail-evaluation` in MLflow's
**Evaluation Runs** view.
