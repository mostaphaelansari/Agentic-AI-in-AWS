# 07 — `evaluations/` — The Quality Release Gate

The cost model proves an agent is *cheap*. This framework proves it is *correct*.
It runs a real Amazon Bedrock AgentCore **on-demand dataset evaluation** against the
**deployed Custom Runtime** and **fails the build** if the agent regresses. It runs
in two places: on every push to `main` (as a recurring smoke check) and right after
a custom-agent deploy (as the actual release gate). See
[05-ci-tests-config.md](05-ci-tests-config.md) for the CI wiring.

New to spans/tracing/evaluators? Skim
[00-concepts-for-beginners.md §6](00-concepts-for-beginners.md) first.

```text
evaluations/
  agentcore_dataset.json            the test cases (scenarios)
  agentcore_evaluation_config.json  which evaluators + pass thresholds
  run_agentcore_evaluation.py       the runner (invoke → collect spans → score → gate)
  README.md                         what the gate proves and its limits
```

---

## The mental model: what an "agent evaluation" even is

Unit tests check pure functions. But an *agent* is non-deterministic — it calls a
model, browses the web, and phrases answers differently every time. You can't assert
`answer == "17°C"`. So instead you assert **semantic invariants** ("the answer
reports Paris weather from the tools without inventing values") and **trajectory**
("it called `searchLocation` *then* `getForecast`"), and you let **AI evaluators**
(built into AgentCore) score how well each scenario was handled. The evaluation
*gate* then compares those scores to thresholds.

---

## `agentcore_dataset.json` — the test cases

[evaluations/agentcore_dataset.json](../evaluations/agentcore_dataset.json) holds
**scenarios**. Each has an `input` (the user turn), an `expected_response` written
as *semantic invariants* (not a fixed string), an `expected_trajectory` (the exact
ordered tool calls), and human-readable `assertions`/`metadata`. The two current
scenarios:

1. **`weather_paris_current`** — "What is the current weather in Paris?" Expected
   trajectory: `searchLocation` → `getForecast`. Invariant: report real tool values,
   invent nothing.
2. **`weather_location_missing`** — "What is the weather right now?" Expected
   trajectory: *empty*. Invariant: **ask for the location** instead of inventing
   one.

Note the tool names are prefixed with the Gateway target
(`open-meteo-tools___searchLocation`) — matching what the Lambda and Gateway use.

## `agentcore_evaluation_config.json` — evaluators and thresholds

[evaluations/agentcore_evaluation_config.json](../evaluations/agentcore_evaluation_config.json)
defines **evaluator groups**, each with a `scenario_filter` and per-evaluator pass
thresholds (0–1):

- **`semantic`** group (all scenarios): `Builtin.Correctness ≥ 0.7`,
  `Builtin.GoalSuccessRate ≥ 0.8`.
- **`trajectory`** group (only scenarios *with* a non-empty expected trajectory):
  `Builtin.TrajectoryExactOrderMatch = 1.0`.

Plus operational knobs: delays, concurrency, span-wait timeouts, runtime-ready
polling, and an `invocation_payload` (`enable_caching:false`,
`enable_thinking:false` for a deterministic baseline).

> Why two separate groups (and two separate AgentCore runs)? Because the service
> rejects an *empty* expected trajectory, so the missing-location case can only be
> in the semantic group. Their scores therefore come from different sessions — the
> README calls this out honestly.

---

## `run_agentcore_evaluation.py` — the runner, step by step

[evaluations/run_agentcore_evaluation.py](../evaluations/run_agentcore_evaluation.py)
orchestrates the whole gate. Reading it top to bottom:

1. **Identify the target.** `_runtime_id` / `_region_from_arn` parse the Runtime
   ARN. `_load_config` reads the config and **validates every threshold** is a finite
   number in [0, 1] (a bad config fails loudly, not silently).
2. **Wait for the Runtime to be READY** (`_wait_for_runtime`) and — when
   `AGENTCORE_EVALUATION_EXPECTED_IMAGE_TAG` is set — **verify the live container's
   image tag matches the Git SHA being evaluated**. This is what makes the
   post-deploy job a true gate: it refuses to "pass" a stale deployment.
3. **Invoke the agent per scenario** (`_build_agent_invoker`). For each scenario it
   calls `invoke_agent_runtime` with the same `Current date: …\n<prompt>` shape the
   real app uses, then reads the **SSE** response with `_parse_response_body`,
   concatenating `text` events into the answer and raising on any `error` event.
   This is a mini reimplementation of the runtime backend, specialized for
   evaluation.
4. **Collect the spans** (`RequiredCloudWatchSpanCollector`, a subclass of the SDK's
   `CloudWatchAgentSpanCollector`). It waits for the agent's OpenTelemetry spans to
   land in CloudWatch and **fails if none arrive** — no observability, no pass. It
   records how many spans/traces each session produced for the report.
5. **Score** — hands the dataset, the agent invoker, and the span collector to the
   SDK's `OnDemandEvaluationDatasetRunner`, once per evaluator group, with only that
   group's evaluators enabled.
6. **Gate** (`_gate_report`). For every scenario × evaluator it checks the score is
   present, numeric, and **≥ threshold**, and accumulates human-readable failures.
   It also fails on: a scenario that didn't COMPLETE, an evaluator error, missing
   scores, **or a missing expected scenario** (so a silently dropped test can't slip
   through).
7. **Report + exit code.** `_write_report` writes a rich JSON report (runtime
   metadata, container URI, region, log group, span-collection summary, all scores,
   all failures) to `evaluation-results/agentcore-evaluation.json` — uploaded as a
   CI artifact. `main()` prints each score line and returns **exit code 1 if any
   threshold failed**, which turns the workflow red.

---

## How the pieces fit CI

```text
deploy-custom-agent.yml:
  build+push SHA image → pulumi up → run_agentcore_evaluation.py
                                        │
        EXPECTED_IMAGE_TAG=github.sha ──┤ verify live container == this commit
                                        │ invoke each scenario (real Runtime, real Gateway, live Open-Meteo)
                                        │ wait for CloudWatch spans (required)
                                        │ score with Builtin.Correctness / GoalSuccessRate / TrajectoryExactOrderMatch
                                        ▼
                            any score < threshold  ⇒  exit 1  ⇒  red deploy, report artifact uploaded
```

The pure, offline parts (ARN parsing, SSE parsing, the gate logic, the readiness
check) are unit-tested in
[test_agentcore_evaluation.py](../tests/unit/test_agentcore_evaluation.py) so the
release gate's *own* logic can't silently break.

---

## Honest boundaries (straight from `evaluations/README.md`)

The [README](../evaluations/README.md) is refreshingly candid about what this does
**not** prove, and it's worth internalizing as a beginner:

- The Paris case uses **live** Open-Meteo data, so the expected answer is a set of
  invariants, not fixed temperatures.
- Thresholds are **initial smoke thresholds**, not calibrated against a
  human-labeled benchmark.
- **One run per scenario** measures behavior once, not a reliability distribution.
- The recurring `main` job evaluates whatever is deployed in `dev`; only the
  post-deploy job binds the evaluated container to `${{ github.sha }}`.
- It evaluates **only the Custom Runtime** — the Managed Harness would need its own
  invocation adapter and log-group discovery.
- Dataset evaluation is **model- and service-backed** (it costs real AgentCore /
  model / CloudWatch usage) and the AgentCore dataset-evaluation API is in public
  preview, so the contract may change.

The stated next step: a larger frozen-fixture dataset, repeated runs,
baseline-vs-candidate comparison, calibrated thresholds, and a dedicated
least-privilege evaluation role.

---

### Where this connects to the rest of the repo

- **Producer contract:** the runner parses the *same six SSE events*
  ([00-concepts §9](00-concepts-for-beginners.md)) that
  [agents/custom/agent.py](../agents/custom/agent.py) emits and
  [backends/runtime.py](../apps/comparison/backends/runtime.py) forwards.
- **Observability contract:** it relies on the CloudWatch spans that the Custom
  agent produces via ADOT (see [03](03-custom-agent.md)) — the exact pipeline
  [infrastructure/otel/README.md](../infrastructure/otel/) warns you not to disable.
- **Release contract:** it's the teeth behind the deploy pipeline in
  [05-ci-tests-config.md](05-ci-tests-config.md) and the prompt-pinning in
  [06-services-and-finops.md](06-services-and-finops.md).
