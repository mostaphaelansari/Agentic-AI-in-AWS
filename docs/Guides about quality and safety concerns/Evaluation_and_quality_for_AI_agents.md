---
title: "Evaluation And Quality For AI Agents"
date: 2026-06-22
as_of: 2026-06-22
last_verified: 2026-06-23
author: Mostapha EL ANSARI
tags:
  - aws
  - agentcore
  - evaluations
  - quality
  - observability
  - regression
status: internal
---

# Evaluation And Quality For AI Agents

> [!summary]
> Agent quality is not proven by a successful demo or a single answer score. It is managed through **offline datasets for controlled comparison, golden traces for known-good behavior, regression suites for release safety, and online evaluation for production drift**. Amazon Bedrock AgentCore Evaluations supplies online, on-demand, batch, dataset-runner, built-in, custom LLM, and custom code-based capabilities. The team still owns the benchmark, ground truth, deterministic assertions, thresholds, incident feedback loop, and final release decision.

> [!note]
> **Research cutoff: 22 June 2026.** AgentCore Dataset Evaluation, Batch Evaluation, and Simulation are documented as public-preview capabilities. Batch Evaluation does not support AWS CloudTrail at this cutoff.

## Definition

Agent evaluation is the systematic measurement of whether an agent completes realistic tasks correctly, safely, consistently, and efficiently. Unlike a simple chatbot test, an agent evaluation must inspect both the answer and the execution path:

- Did the agent understand the task?
- Did it retrieve appropriate evidence?
- Did it select the correct tools?
- Were tool arguments valid and authorized?
- Did it avoid unnecessary or forbidden calls?
- Did it complete the business goal?
- Was the final response correct, grounded, safe, and useful?
- Were latency, token use, cost, and failures within acceptable limits?

The goal is not to prove that an agent can never fail. The goal is to make important failures repeatable, measurable, diagnosable, and release-blocking when necessary.

## The Terms That Matter

| Term | Meaning | Is it a named AgentCore resource? |
|---|---|---:|
| **Offline evaluation** | Controlled evaluation against a fixed or versioned dataset before release | No; it is a practitioner category implemented with on-demand, dataset, batch, or custom runners |
| **Golden trace** | A reviewed execution showing an accepted answer and agent trajectory for an important task | No; store it as a dataset scenario, trace fixture, or versioned test case |
| **Regression suite** | A versioned collection of cases and quality gates rerun after changes | No; the team owns the suite and release policy |
| **Online evaluation** | Continuous scoring of sampled live traffic | Yes; AgentCore online evaluation configuration |
| **On-demand evaluation** | Targeted synchronous scoring of selected spans, traces, or sessions | Yes; AgentCore `Evaluate` workflow |
| **Batch evaluation** | Asynchronous service-side evaluation of multiple CloudWatch sessions | Yes; public preview |
| **Dataset evaluation** | SDK runner that invokes scenarios, collects telemetry, and evaluates results | Yes; the batch runner depends on public-preview Batch Evaluation |

Do not use these terms interchangeably. Online evaluation observes uncontrolled production traffic. Offline regression evaluates controlled cases with expected behavior before a change reaches users.

## What Quality Means For An Agent

| Quality dimension | Core question | Strong measurement |
|---|---|---|
| Final-answer correctness | Is the conclusion correct? | Expected response, domain ground truth, deterministic fact check |
| Goal success | Did the agent complete the user's task? | Session assertions, business outcome, human label |
| Grounding | Are factual claims supported by approved evidence? | Faithfulness evaluator, citation/source validator, Guardrails contextual grounding |
| Tool selection | Did the agent choose the right capability? | Expected trajectory, forbidden-tool assertion |
| Tool arguments | Were parameters valid and safe? | Schema validation, exact constraints, code-based evaluator |
| Authorization | Was the action allowed for this identity and context? | AgentCore Policy/Cedar result and application authorization checks |
| Safety | Did the agent avoid harmful content, prompt injection, and leakage? | Adversarial cases, Guardrails results, deterministic secret/PII checks |
| Efficiency | Did it avoid loops, redundant calls, and excess tokens? | Tool-call count, iteration count, latency, tokens, cost envelope |
| Reliability | Does it behave acceptably across repeated runs? | Pass rate and score distribution across repeated trials |
| Regression stability | Did a change degrade an accepted baseline? | Candidate-versus-baseline comparison with release gates |

No single evaluator covers all of these dimensions.

## Where Evaluation Fits

```text
Production or test invocation
  -> OpenTelemetry/OpenInference records spans, traces, tools, and sessions
  -> CloudWatch stores and indexes observable behavior
  -> Evaluators score a tool call, trace, or complete session
  -> Results are compared with ground truth and a previous baseline
  -> Release gates pass, block, or require review
  -> Failures become new regression cases
```

Evaluation quality cannot exceed trace quality. If tool names, arguments, outputs, session boundaries, or final responses are absent or incorrectly correlated, the evaluator is judging an incomplete story.

## Offline Evaluation

Offline evaluation runs a controlled, versioned set of scenarios against a candidate agent configuration. It should happen before changing a model, prompt, tool schema, retriever, memory strategy, policy, Guardrail, or orchestration loop.

An offline case should contain more than a prompt:

```text
Stable case ID
User input or multi-turn conversation
Fixture and environment version
Expected response or acceptable facts
Expected tool trajectory
Forbidden tools or actions
Assertions about observable behavior
Safety and authorization expectations
Criticality and owner
```

Use fixed fixtures for external systems. A weather regression test that queries live weather cannot reliably compare exact answers across days. Freeze the tool result, record the observation time, or assert only invariant behavior such as correct tool selection and faithful use of returned fields.

### Why Offline Evaluation Is Valuable

- The same candidate and baseline see the same cases.
- Critical failures are tested even if production traffic rarely triggers them.
- Ground truth can be supplied.
- Tests can run before deployment.
- Failures are reproducible.
- Release decisions can be automated.

Offline results still have limits: the dataset can become stale, synthetic cases can differ from real users, and an LLM judge can introduce its own variance.

### Offline Evaluation Versus Golden Traces

These concepts describe different parts of an evaluation system:

| Concept | What it describes | Meaning |
|---|---|---|
| **Offline evaluation** | When and how evaluation runs | Controlled scenarios are executed outside production traffic, usually before release or on a schedule |
| **Golden trace** | What candidate behavior is compared against | A reviewed, versioned, known-good execution path containing the accepted answer, tool behavior, and relevant context |

Offline evaluation is the execution mode; a golden trace is a reference artifact that an evaluation may use. An offline evaluation does not require a golden trace: it can score outputs with assertions, expected responses, deterministic checks, or an LLM judge. Conversely, storing a golden trace does not itself run an evaluation; a runner must replay or compare candidate behavior against it.

The repository's current AgentCore smoke gate is an offline dataset evaluation with expected responses, assertions, and a lightweight golden tool trajectory. It collects a new candidate trace from CloudWatch for each run, but it does not compare that trace with a stored, reviewed full-span golden trace.

## Golden Traces

A golden trace is a reviewed, known-good execution for an important task. It represents the accepted path, not necessarily the only valid wording.

A strong golden trace records:

```text
Input and conversation state
Tool catalog and schema version
Retrieved or tool-provided evidence
Expected tool names and order
Important argument constraints
Forbidden calls
Expected final-answer facts
Safety, policy, and escalation decisions
Maximum iteration, latency, or cost envelope
```

### Golden Response Versus Golden Trajectory

These solve different problems:

- A **golden response** describes what the answer must communicate.
- A **golden trajectory** describes how the agent should reach the answer.

An exact response string is often too brittle because multiple phrasings can be correct. Tool trajectories can also be over-specified: if two safe paths are valid, forcing one exact order creates false regressions. Choose the weakest trajectory constraint that protects the requirement:

| Constraint | Use when |
|---|---|
| Exact order | Every tool and order is mandatory; extras are unsafe or wasteful |
| In order | Required tools must occur in sequence, but helper calls are acceptable |
| Any order | Required tools must be used, but order does not matter |
| Forbidden set | Certain tools must never be used for the case |
| Argument predicate | Tool choice is flexible, but parameters must satisfy exact constraints |

### Golden Trace Lifecycle

1. Start with business-critical happy paths and failure paths.
2. Review the accepted answer and trajectory with the domain owner.
3. Store the case beside the agent version or in a versioned evaluation dataset.
4. Rerun it for every relevant change.
5. Convert serious production incidents into new golden regression cases.
6. Review cases when tools, policies, products, or source data change.

Never promote a trace to “golden” merely because the current agent produced it.

## Regression Suites

A regression suite is the executable contract for agent quality. It combines cases, evaluators, baselines, and release gates.

### Recommended Dataset Mix

| Case class | Examples |
|---|---|
| Core tasks | Common user goals the agent must complete |
| Boundary cases | Missing location, ambiguous date, partial account data |
| Negative cases | Unsupported request, unavailable tool, no matching source |
| Adversarial cases | Prompt injection, malicious tool output, data-exfiltration request |
| Authorization cases | Allowed and denied identities, amounts, tenants, and operations |
| Reliability cases | Tool timeout, malformed tool response, retryable failure |
| Incident cases | Every material production failure that must not recur |
| Efficiency cases | Loop prevention, redundant tools, excessive retrieval or tokens |

Split the suite into:

- **Smoke:** small, fast, release-blocking checks on every change.
- **Core regression:** representative tasks on pull requests or deployment candidates.
- **Extended:** larger, repeated, adversarial, and expensive cases on a schedule.
- **Production replay:** sanitized real failures and representative traffic snapshots.

### Candidate Versus Baseline

Run the current production configuration and the candidate against the same dataset and fixtures. Compare:

```text
Absolute pass/fail on critical cases
Candidate score minus baseline score
Failure categories, not only the mean
Latency and token distributions
New tool paths and argument changes
Evaluator errors and missing telemetry
```

An improved average must not hide a catastrophic failure in one high-risk case.

## AgentCore Evaluation Surfaces

| Surface | Trigger and data | Best use | Important boundary |
|---|---|---|---|
| Online | Continuous; watches an agent endpoint or CloudWatch log groups with sampling and filters | Production trends, drift, dashboards | Live traffic has no per-case ground truth |
| On-demand | Caller supplies selected spans/traces to `Evaluate` | Debugging, CI spot checks, incident analysis | Caller owns selection and span collection |
| Batch | Asynchronous; service discovers multiple sessions from CloudWatch Logs | Baselines, large comparisons, periodic audits | Public preview; no CloudTrail support at cutoff |
| On-demand dataset runner | Invokes scenarios, collects spans SDK-side, calls `Evaluate` | Small offline suites and immediate per-case detail | Requires observability and telemetry ingestion |
| Batch dataset runner | Invokes scenarios, then delegates collection/scoring to Batch Evaluation | Large suites and aggregate comparison | Inherits Batch Evaluation preview boundaries |

AgentCore dataset evaluation requires an observable agent, supported instrumentation, CloudWatch Transaction Search, the AgentCore SDK, and appropriate AgentCore/CloudWatch permissions.

### Evaluation Levels

| Level | Unit being judged | Typical checks |
|---|---|---|
| `TOOL_CALL` | One tool invocation | Tool selection, argument safety, schema validity |
| `TRACE` | One conversational turn/trace | Correctness, helpfulness, faithfulness, response format |
| `SESSION` | Complete multi-turn interaction | Goal success, trajectory, escalation, cumulative policy behavior |

Choose the narrowest level that contains enough evidence. A session-level judge is unnecessary for a JSON-schema check; a tool-call judge cannot decide whether a multi-turn user goal was completed.

## Ground Truth And AgentCore Dataset Mapping

AgentCore predefined datasets use the following fields:

| Dataset field | Evaluator | Scoring behavior |
|---|---|---|
| `turns[].expected_response` | `Builtin.Correctness` | Trace-level LLM-as-a-judge comparison with expected answer |
| `assertions` | `Builtin.GoalSuccessRate` | Session-level LLM-as-a-judge check of natural-language expectations |
| `expected_trajectory` | `Builtin.TrajectoryExactOrderMatch` | Programmatic exact tool sequence |
| `expected_trajectory` | `Builtin.TrajectoryInOrderMatch` | Programmatic ordered subsequence; extras allowed |
| `expected_trajectory` | `Builtin.TrajectoryAnyOrderMatch` | Programmatic required-tool membership; order irrelevant |
| `metadata` | Dataset organization | Owner-defined labels such as category, risk, and criticality |

The equivalent `Evaluate` API fields use camel case: `expectedResponse`, `assertions`, and `expectedTrajectory`.

Ground truth is optional, but its absence changes what the evaluator can prove. `Builtin.Correctness` without `expectedResponse` judges from context rather than comparing with a known answer. Online evaluations cannot use custom evaluator placeholders that require per-case ground truth because live traffic does not carry those expected values.

### What The Core Metrics Mean

These metrics answer different quality questions and should not be treated as interchangeable:

| Metric | Question it answers | What it examines | What it does not prove |
|---|---|---|---|
| **Correctness** | Is the information in this response factually right? | The response, available context, and optionally a reference answer or ground truth | That the response completed the user's broader task, followed the required process, was safe, or was efficient |
| **Goal success** | Did the interaction accomplish what the user was trying to achieve? | The complete session, user goal, outcome, and any natural-language success assertions | That every statement was factually correct or that the agent used a specific tool path |
| **Trajectory exact-order match** | Did the agent follow exactly the required sequence of tool calls or actions? | The observed tool trajectory compared with the expected ordered trajectory | That the final answer was correct, useful, or grounded; a perfectly matched process can still produce a bad answer |

**Correctness** is normally a response-quality metric. It is appropriate when the main concern is factual accuracy: whether claims agree with known facts, supplied evidence, or an expected answer. A partially correct response may contain some accurate information while omitting or misstating an important fact.

**Goal success** is a task-completion metric. It judges the outcome of the whole interaction rather than one sentence or one tool call. For example, an agent may provide factually correct information but still fail the goal because it never completes the requested action, asks unnecessary questions, or stops before producing the required result. A per-scenario result is success or failure; aggregating those results across many scenarios produces a success rate.

**Trajectory exact-order match** is a process-conformance metric. It requires the observed tools to match the expected tools in the same order, without missing or additional calls. Use it only when one exact path is genuinely required. If several safe approaches are valid, an in-order, any-order, forbidden-tool, or argument-based check is less brittle.

Together, these metrics distinguish three independent concerns:

```text
Correctness       -> Was the answer right?
Goal success      -> Was the user's objective achieved?
Trajectory match  -> Was the required process followed?
```

### Predefined Versus Simulated Scenarios

| Scenario | Behavior | Ground truth support |
|---|---|---|
| Predefined | Replays fixed authored turns | Expected response, trajectory, assertions, metadata |
| Simulated | LLM-backed actor generates turns from a persona and goal | Assertions and metadata; no per-turn expected response or expected trajectory |

Simulation is useful for discovering conversation variants. It is not a replacement for a stable predefined regression set.

## Evaluator Strategy

### Built-In LLM Evaluators

Built-in evaluators provide AWS-managed model and prompt configurations for common dimensions such as correctness, helpfulness, faithfulness, goal success, and tool behavior. Their internal configuration cannot be modified.

Use them for broad semantic judgments, then calibrate against human labels. Do not assume an evaluator score is ground truth simply because the evaluator is managed.

### Custom LLM-As-A-Judge Evaluators

Custom evaluators let the team define the Bedrock judge model, instructions, placeholders, and rating scale at `TOOL_CALL`, `TRACE`, or `SESSION` level.

Use them for requirements such as:

- Correct escalation language.
- Domain-specific completeness.
- Whether a response distinguishes observations from inference.
- Whether a multi-turn workflow satisfied business assertions.

Version evaluator prompts and models. A changed evaluator can move the score even when the agent did not change.

### Programmatic And Code-Based Evaluators

Use deterministic evaluation for non-negotiable invariants:

- JSON or schema validity.
- Forbidden tool calls.
- Exact argument constraints.
- PII or secret patterns.
- Required citations.
- Maximum iteration count.
- Authorization and policy outcomes.
- Numerical correctness where inputs are known.

AgentCore custom code-based evaluators invoke a Lambda function. The Lambda receives session spans, reference inputs, and the evaluation target, and returns a label plus optional numeric value and explanation. AWS documents a maximum Lambda timeout of 300 seconds and a 6 MB input payload limit.

The safest pattern is hybrid:

```text
Deterministic checks for hard invariants
LLM judges for semantic quality
Human review for ambiguous or high-impact cases
```

## Concrete Dataset Example For This Repository

The current project compares a managed Harness and a custom Runtime agent using weather and browsing tools. A first offline dataset could be:

```json
{
  "scenarios": [
    {
      "scenario_id": "weather-paris-current",
      "turns": [
        {
          "input": "What is the current weather in Paris?",
          "expected_response": "The answer reports only weather values present in the frozen Open-Meteo fixture."
        }
      ],
      "expected_trajectory": ["searchLocation", "getForecast"],
      "assertions": [
        "The agent uses the weather tool before answering.",
        "The agent identifies Paris as the requested location.",
        "The agent does not invent weather values absent from the tool result."
      ],
      "metadata": {
        "category": "core_weather",
        "critical": true,
        "fixture": "open_meteo_paris_v1"
      }
    },
    {
      "scenario_id": "weather-location-missing",
      "turns": [
        {
          "input": "What is the weather right now?"
        }
      ],
      "assertions": [
        "The agent asks for a location instead of inventing one.",
        "The agent does not call the weather tool with a fabricated location."
      ],
      "metadata": {
        "category": "ambiguity",
        "critical": true
      }
    }
  ]
}
```

The exact tool name must match the instrumented span name used by the deployed agent. If the managed and custom architectures emit different tool names for equivalent behavior, either normalize those names before evaluation or maintain architecture-specific expected trajectories.

## Quality Gates

Evaluation results become governance only when they trigger decisions.

Example release policy:

```text
Block release when:
  any critical case fails
  any unauthorized or forbidden tool call occurs
  any secret or PII leakage occurs
  required deterministic assertions fail
  telemetry is missing for a critical case

Require review when:
  semantic correctness or goal success regresses beyond tolerance
  the tool trajectory changes but the final answer remains correct
  evaluator explanations disagree
  latency, tokens, or tool calls exceed the agreed envelope

Allow release when:
  all hard gates pass
  aggregate metrics meet their thresholds
  candidate-versus-baseline changes are understood
  reviewed exceptions have owners and expiry dates
```

Thresholds such as `0.90` are not universal defaults. Derive them by comparing evaluator results with human labels and business risk. Track false passes and false failures for the evaluator itself.

## When It Bites

1. **Only evaluating the final answer.**
   A correct answer can hide a forbidden call, wrong customer lookup, leaked argument, or unnecessary action.

2. **Treating an LLM judge as an oracle.**
   Judges can be inconsistent, biased toward verbosity, sensitive to ordering, or wrong about domain rules.

3. **Exact-string golden answers.**
   Correct paraphrases fail while semantically wrong answers containing expected keywords can pass.

4. **Over-constrained trajectories.**
   Safe improvements fail because the suite permits only one historical tool path.

5. **Under-constrained trajectories.**
   The answer passes even though the agent skipped a mandatory authorization or evidence step.

6. **Live external data in offline tests.**
   Changing weather, search, prices, or APIs make candidate and baseline incomparable.

7. **No negative or adversarial cases.**
   Happy-path quality hides prompt injection, unauthorized actions, refusal failures, and recovery defects.

8. **Data leakage between train, tuning, and evaluation sets.**
   Optimizing prompts against every test case creates a flattering benchmark that no longer measures generalization.

9. **No repeated runs.**
   One stochastic success hides a poor pass distribution.

10. **Ignoring evaluator failures.**
    Missing spans, throttling, judge errors, or telemetry delay must not silently count as agent passes.

11. **Averages hiding critical failures.**
    A high mean score cannot compensate for one unauthorized payment or PII leak.

12. **Stale golden traces.**
    Product policy or tool behavior changed, but the expected trace did not.

13. **Using online evaluation as the release suite.**
    Production sampling cannot guarantee coverage of critical pre-release cases and has no per-request ground truth.

14. **Preview features used as an audit control.**
    Batch Evaluation has no CloudTrail coverage at this cutoff.

## Recommended Operating Model

```text
Development
  -> run a small on-demand smoke dataset
  -> debug failures at tool-call and trace level

Pull request / release candidate
  -> run the versioned predefined regression suite
  -> compare candidate with production baseline
  -> enforce deterministic critical gates

Scheduled extended testing
  -> run repeated, adversarial, simulated, and large batch suites
  -> review drift in evaluator and agent behavior

Production
  -> sample live sessions with online evaluation
  -> alert on trends and inspect low-scoring sessions
  -> turn material incidents into offline regression cases
```

## Repository Status And Practical Next Step

The repository now runs a small file-backed AgentCore on-demand dataset evaluation against the deployed custom Runtime. The runner invokes the real agent, collects its CloudWatch spans, and calls `Builtin.Correctness`, `Builtin.GoalSuccessRate`, and `Builtin.TrajectoryExactOrderMatch`. Numeric thresholds make failed scenarios, evaluator errors, missing results, invalid scores, and low scores release-blocking. The report records source, dataset/config hashes, Runtime version, and container URI. After custom deployment it also verifies the expected Git-SHA image tag before scoring.

The push-to-`main` job evaluates the service currently deployed in `dev`; it is a monitoring smoke check, not proof that the pushed commit is deployed. The post-deployment workflow is the candidate gate. The two-case suite uses live Open-Meteo data and initial, uncalibrated thresholds, so it should not be represented as a complete regression benchmark.

The repository still does not create custom evaluators, online evaluation configurations, service-side datasets, or batch jobs. The application also provides useful prerequisites for extending the evaluation:

- Per-agent `runtimeSessionId` values passed to Harness and Runtime calls.
- CloudWatch session queries and links.
- Visible tool activity and reasoning/text/tool events.
- Latency and normalized token usage for each run.

These are observability and comparison signals, not proof of answer quality.

A narrow next iteration would be:

1. Freeze representative Open-Meteo and browser fixtures.
2. Expand the initial two scenarios to 10–20 predefined scenarios covering normal, ambiguous, failing, and adversarial prompts.
3. Add deterministic checks for invented weather fields, forbidden tools, missing location clarification, and iteration limits.
4. Run both architectures against identical fixtures and dataset versions.
5. Compare candidate scores against a stored production baseline instead of using absolute thresholds alone.
6. Calibrate evaluator scores and thresholds against reviewed human labels.
7. Add online evaluation only after the offline suite and evaluator calibration are stable.

## Validation Questions

1. What are the critical user goals and unacceptable failures?
2. Does each critical goal have happy, boundary, negative, and adversarial cases?
3. Is every important production incident converted into a regression case?
4. Which checks require an expected response, trajectory, or session assertion?
5. Which invariants are deterministic rather than LLM-judged?
6. Are fixtures frozen and versioned?
7. Are managed and custom agents evaluated against identical inputs and tool data?
8. Does trace instrumentation capture the evidence every evaluator needs?
9. Are evaluators versioned and calibrated against human labels?
10. Are critical cases gated individually rather than hidden in an average?
11. Are repeated runs used where model variance matters?
12. Are latency, tokens, retries, and tool calls part of the quality envelope?
13. Does every threshold have an owner and operational action?
14. Are preview and audit limitations acceptable for the intended workload?
15. Can a reviewer reproduce a failure from the stored case, fixture, agent version, and evaluator version?

## Sources

- [AWS documentation: AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html)
- [AWS documentation: Online evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/online-evaluations.html)
- [AWS documentation: On-demand evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/on-demand-evaluations.html)
- [AWS documentation: Batch evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/batch-evaluations.html)
- [AWS documentation: Dataset evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/dataset-evaluations.html)
- [AWS documentation: Dataset schema](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/dataset-evaluations-schema.html)
- [AWS documentation: Ground truth evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/ground-truth-evaluations.html)
- [AWS documentation: Built-in evaluators](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/built-in-evaluators-overview.html)
- [AWS documentation: Custom evaluators](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/custom-evaluators.html)
- [AWS documentation: Custom code-based evaluators](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html)
- [AWS documentation: AgentCore Simulation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/simulation.html)
