# Codebase Explanation — AWS Agentic Blueprint

A guided, **beginner-friendly** tour of every meaningful piece of code in this
repository and how the pieces fit together. If you've never touched AWS agents,
Streamlit, or Pulumi, start with
[00-concepts-for-beginners.md](00-concepts-for-beginners.md) — it defines every
term used anywhere in this folder.

> This folder is documentation only. It's safe to read in any order, but the
> numbering is a suggested path.

## What this project is

A reference implementation that compares **two ways of building the same agent** on
Amazon Bedrock AgentCore:

1. **Managed agent** — a *configuration-defined* agent. AWS hosts the agent loop
   (AgentCore **Harness**); the repo only owns its system prompt and infrastructure
   definition.
2. **Custom agent** — a *code-defined* agent. The repo owns the full agent loop
   (a **Strands** agent in a Docker container) hosted on AgentCore **Runtime**.

A **Streamlit app** sends one prompt to both in parallel and compares answers,
latency, tool calls, reasoning, token usage, prompt-cache behavior, and **measured
cost** (tokens + Gateway operations + Runtime compute).

Both agents share the same tools: an Open-Meteo weather toolset exposed through an
**AgentCore Gateway** (MCP, backed by a Lambda), plus a managed browser.

## The one idea that ties everything together

Every layer converges on a **single normalized event vocabulary** (the "six
events"):

```python
{"type": "reasoning",  "content": "..."}
{"type": "text",       "content": "..."}
{"type": "tool_start", "name": "...", "input": {...}}
{"type": "tool_end",   "name": "...", "result": "..."}
{"type": "metrics",    "usage": {...}}
{"type": "error",      "content": "..."}
```

- The **Custom agent** ([agents/custom/agent.py](../agents/custom/agent.py)) *emits*
  these directly as SSE.
- The **Managed adapter** ([apps/comparison/backends/harness.py](../apps/comparison/backends/harness.py))
  *translates* the Harness Converse-style stream into the same events.
- The **event reducer** ([apps/comparison/services/event_reducer.py](../apps/comparison/services/event_reducer.py))
  consumes them and updates UI state, never knowing which backend produced them.

This contract is written in three places that must stay in sync: this README, the
docstring atop `agents/custom/agent.py`, and the typed models in
[apps/comparison/models/events.py](../apps/comparison/models/events.py).

## High-level data flow

```text
                 ┌──────────────────────────────────────────────────────────┐
                 │ apps/comparison (Streamlit, runs on your laptop)         │
 user prompt ──> │ app.py → services/security (block leaks) → components/comparison.run_comparison()
                 │   2 threads → backends/dispatcher.dispatch()             │
                 │     ├─ backends/harness.invoke_harness()  ──────────────────► AgentCore Harness (eu-central-1)
                 │     └─ backends/runtime.invoke_runtime()  ──────────────────► AgentCore Runtime (eu-west-1)
                 │   events → queue → services/event_reducer.apply_event()  │        │ runs agents/custom/agent.py
                 │   state → components/* render + services/cost + runtime_usage │    ▼
                 └──────────────────────────────────────────────────────────┘   AgentCore Gateway (MCP, AWS_IAM)
                                            both agents call tools via ─────────►  └─ Lambda: open_meteo_lambda.py
                                                                                       └─ Open-Meteo APIs
```

## The guide, in reading order

| Doc | Covers |
|---|---|
| [00-concepts-for-beginners.md](00-concepts-for-beginners.md) | **Start here.** Every concept: LLM/token/tool/agent-loop, AgentCore Harness/Runtime/Gateway, MCP, SSE, SigV4, IAM, FinOps, OTEL/MLflow, Pulumi, CI/CD, and the Python tooling. |
| [01-streamlit-app.md](01-streamlit-app.md) | `apps/comparison/` — the UI, its layered design, the live poll loop, state machine, event reducer, formatting, and the measured cost table. |
| [02-backends.md](02-backends.md) | `apps/comparison/backends/` — the Managed & Custom adapters, the thinking-injection trick, error safety, dispatcher. |
| [03-custom-agent.md](03-custom-agent.md) | `agents/` — the two system prompts, the Strands agent, prompt-management loading + hash check, the SigV4 MCP client, MLflow tracing, Dockerfile. |
| [04-infrastructure.md](04-infrastructure.md) | `infrastructure/` — the three Pulumi stacks, the Gateway Lambda, prompt/IAM wiring, the Agent Registry script, local OTEL, the legacy script. |
| [05-ci-tests-config.md](05-ci-tests-config.md) | `.github/workflows/`, `tests/`, `pyproject.toml` — CI, the manual OIDC deploys, the guardrail tests, and project config. |
| [06-services-and-finops.md](06-services-and-finops.md) | `services/` — the measured cost model, deferred Runtime-usage lookup, the log CLI, the security guard, and the prompt-versioning pipeline. |
| [07-evaluations.md](07-evaluations.md) | `evaluations/` — the real AgentCore dataset evaluation that gates releases. |

## Repository layout at a glance

```text
apps/comparison/     Streamlit comparison UI (the only supported UI; runs locally)
agents/managed/      system-prompt.md for the Harness agent (config-defined)
agents/custom/       Strands agent code + Docker image for AgentCore Runtime
infrastructure/
  pulumi/            gateway / managed_agent / custom_agent stacks (source of truth)
  gateway/           Open-Meteo Lambda source + legacy boto3 deploy script
  scripts/           CI Streamlit health check + Agent Registry registration
  otel/              optional local MLflow/OpenTelemetry setup
evaluations/         dataset + config + runner for the release-gating evaluation
tests/unit/          offline logic + CI/CD-guardrail tests
tests/manual/        real-AWS diagnostic for thinking events
docs/                architecture/FinOps/caching write-ups (rendered in-app)
aws/                 historical deployed-state.json from the legacy script
```

## Key cross-correlations (quick reference)

- **The six-event contract:** `agents/custom/agent.py` (producer) ↔
  `backends/harness.py` (translator) ↔ `models/events.py` (types) ↔
  `services/event_reducer.py` (consumer) ↔ `evaluations/run_agentcore_evaluation.py`
  (parses the same SSE).
- **System prompts kept identical:** `agents/managed/system-prompt.md` and
  `agents/custom/prompts/system.md` are near-identical so the comparison is fair.
  The managed one is baked into the Harness by the managed_agent Pulumi stack at
  deploy time; the custom one is versioned into Bedrock Prompt Management and pinned
  by SHA-256 into the Runtime container.
- **Current date:** the UI prepends `Current date: YYYY-MM-DD` to every prompt
  (`backends/common.with_current_date`), and both system prompts treat that line as
  metadata; the custom agent also injects `{today}` when reading a local prompt.
- **Cost is measured, never guessed:** token quantities (model stream) and tool
  counts (event stream) × list rates in `config.py`, via `services/cost.py`; Runtime
  vCPU/GB-hours come from AWS's vended USAGE_LOGS via `services/runtime_usage.py`.
  There is no assumed-utilization knob.
- **Gateway URL/ARN:** exported by the gateway Pulumi stack, consumed via
  `StackReference` by both agent stacks; reaches the custom agent as `GATEWAY_URL`
  and the Harness as an `agentcore_gateway` tool config.
- **Tool names must match in five places:** `getForecast` / `searchLocation` appear
  in the Pulumi Gateway tool schema, the Lambda's dispatch on
  `bedrockAgentCoreToolName`, both system prompts, the Prompt Management tool config
  in `services/prompt_version.py`, and the evaluation dataset's expected trajectory.
- **Feature flags flow end to end:** the sidebar's `enable_thinking` /
  `enable_caching` → `options` → both backends → the model config in
  `backends/harness.py` and `agents/custom/agent.py::_bedrock_model_kwargs` (which
  enforce the *same* thinking-budget constraints).
- **CI guardrails as code:** `tests/unit/test_workflows.py` asserts properties of
  the workflow YAML (deploys are manual, OIDC-based, `environment: dev`, and never
  host Streamlit; CI smoke-tests Streamlit without AWS credentials; the evaluation
  runs on `main` and after custom deploys, bound to the Git SHA).
