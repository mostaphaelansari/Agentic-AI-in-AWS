# 00 — Concepts For Beginners (read this first)

This file explains **every idea** the codebase uses, in plain language, before you
look at any code. If a word in another doc confuses you, it is defined here. Think
of it as the vocabulary list for the rest of the guide.

You do **not** need to know AWS, agents, or Streamlit to read this. We build up
from zero.

---

## 1. What problem does this whole repository solve?

There are two very different ways to build an "AI agent" on Amazon's platform
(Amazon Bedrock AgentCore). This repo builds **the same agent twice, once each
way**, and puts a screen in front of them that runs your question through *both at
the same time* so you can compare them: which is faster, which uses more tokens,
which costs more, how each one "thinks."

The two ways:

| | **Managed agent** | **Custom agent** |
|---|---|---|
| Who writes the "agent loop"? | AWS does (a service called **Harness**) | We do (Python code) |
| Where does it live? | Configuration in AWS | A **Docker** container we build |
| What do we own? | Just a text file (the instructions) | All the code |
| AWS hosting service | AgentCore **Harness** | AgentCore **Runtime** |
| Analogy | Buying a pre-built appliance and setting the dials | Building the appliance yourself |

Both agents use the **same tools** (a weather API and a web browser) and the
**same instructions**, so the comparison is fair.

---

## 2. Core AI / agent vocabulary

**LLM (Large Language Model).** The "brain" — e.g. Claude. You give it text, it
produces text. It has no memory and no ability to act on its own; it just predicts
the next words. Everything else in an "agent" exists to give the LLM memory, tools,
and a loop.

**Token.** LLMs don't read letters or words — they read *tokens*, which are chunks
of text (roughly ¾ of a word). "hello" might be one token; "internationalization"
might be four. **You are billed per token**, both for what you send in (input) and
what the model writes back (output). This is why the app measures tokens so
carefully — tokens are the main cost.

**Prompt.** The text you send to the LLM. In this app it's your question, plus a
line like `Current date: 2026-07-02` that the app adds automatically.

**System prompt.** A hidden instruction block that shapes how the model behaves
("You are a weather assistant. Never use emojis. Reply in the user's language.").
The user never sees it. Both agents here load their system prompt from a markdown
file — [agents/managed/system-prompt.md](../agents/managed/system-prompt.md) and
[agents/custom/prompts/system.md](../agents/custom/prompts/system.md) — and the two
files are kept nearly identical on purpose.

**Tool / tool call / function calling.** LLMs can't actually check the weather or
open a web page. Instead we describe "tools" to the model (name + what inputs it
takes). When the model wants weather, it doesn't answer — it emits a structured
request: *"call `getForecast` with latitude=48.8, longitude=2.3"*. Our code runs
the real function, hands the result back, and the model continues. That
back-and-forth is a **tool call**.

**Agent loop (a.k.a. the ReAct loop).** The repeating cycle that turns a plain LLM
into an "agent":
1. Send prompt + conversation so far to the model.
2. Model either answers, or asks to call a tool.
3. If it called a tool, run the tool, append the result, go back to step 1.
4. Stop when the model gives a final answer (or a safety cap is hit).
Each pass through this loop is called a **cycle** or **iteration**. The Managed
agent caps it at `max_iterations = 12`; the Custom agent caps it at
`MAX_TOOL_CALLS = 12`. Same idea, two names.

**Reasoning / "extended thinking."** Some models can produce hidden scratch-pad
"thinking" before their real answer. You pay tokens for it, but it improves hard
answers. In this app it's a toggle ("Reasoning (extended thinking)"). When on,
the model streams `reasoning` text that the app shows in the **Trace** panel, not
the final answer.

**Streaming.** Instead of waiting for the whole answer, the model sends it a few
tokens at a time so the UI can print it live, like a person typing. This repo
streams everything (text, reasoning, tool activity) so you watch both agents work
in real time.

---

## 3. Amazon Bedrock AgentCore vocabulary (the AWS side)

**Amazon Bedrock.** AWS's service for calling foundation models (Claude, etc.).
When we say "the model," it is served by Bedrock.

**AgentCore.** A family of AWS services for *running agents* (not just calling a
model). The four pieces this repo uses:

- **AgentCore Harness** — the *managed* agent runner. You give it a system prompt,
  a model id, and a tool list; AWS runs the agent loop for you. This is the
  "Managed agent."
- **AgentCore Runtime** — a place to run *your own* container that behaves like an
  agent. You build a Docker image, push it, and Runtime hosts it. This is the
  "Custom agent." It's **serverless**: you don't pick a server size; AWS bills you
  for the vCPU-seconds and memory your container actually uses.
- **AgentCore Gateway** — turns an AWS **Lambda function** into a set of **tools**
  that any agent can call over a standard protocol (**MCP**, below). Both agents
  reach the weather tools through the *same* Gateway.
- **AgentCore Browser** — a managed, sandboxed Chrome that the agent can drive to
  read web pages.

**Lambda.** AWS's "run a function without a server" service. Here, one small Python
function ([open_meteo_lambda.py](../infrastructure/gateway/open_meteo_lambda.py))
calls the free Open-Meteo weather API and returns the result. The Gateway exposes
it as the `getForecast` and `searchLocation` tools.

**ARN (Amazon Resource Name).** A globally unique ID string for any AWS thing,
like `arn:aws:bedrock-agentcore:eu-west-1:554025156005:runtime/custom_agent-…`.
The app needs the Harness's ARN and the Runtime's ARN to call them.

**Region.** A physical AWS location (`eu-west-1` = Ireland, `eu-central-1` =
Frankfurt). The two agents deliberately live in different regions here.

**Session id.** A long unique string that ties all the log records of one run
together. The app generates one per agent per run
(`streamlit_demo-<uuid>--<uuid>`) and shows it so you can find that run in AWS
CloudWatch.

---

## 4. Protocols and security vocabulary

**MCP (Model Context Protocol).** A standard "language" for an agent to discover
and call tools over the network. The Gateway speaks MCP; the Custom agent has an
MCP *client* to talk to it. Think of MCP as USB for tools: a universal plug.

**SSE (Server-Sent Events).** A simple way for a server to stream many small
messages to a client over one HTTP connection. Each message is a line that starts
with `data:` followed by JSON. The Custom agent streams its events as SSE; the
app's `parse_sse_events` reads them. (See `data:` lines in
[backends/common.py](../apps/comparison/backends/common.py).)

**SigV4 (AWS Signature Version 4).** How you prove "I'm allowed to call this AWS
thing" without a password: you cryptographically **sign** each HTTP request with
your AWS credentials. The Gateway here uses `AWS_IAM` authorization, so the Custom
agent must sign its MCP requests with SigV4 — that's the entire job of
[mcp_client/streamable_http_sigv4.py](../agents/custom/mcp_client/streamable_http_sigv4.py).

**IAM (Identity and Access Management) / role / policy.** AWS's permission system.
A **role** is an identity a service can assume; a **policy** is a list of allowed
actions ("may call `bedrock-agentcore:InvokeGateway`"). The agents run *as* a role
that is granted just enough permission to reach the Gateway and the model.

**Credentials / AWS SSO.** The temporary keys that prove who you are. On your
laptop these usually come from `aws sso login`. They **expire**, which is why the
app has special handling to say "run `aws sso login`" instead of a scary stack
trace (see [backends/errors.py](../apps/comparison/backends/errors.py)).

**OIDC (in GitHub Actions).** A way for GitHub's CI to assume an AWS role for a
few minutes **without storing any AWS password** in GitHub. The deploy workflows
use it.

---

## 5. Cost / FinOps vocabulary

**FinOps.** "Financial operations" — understanding and controlling cloud cost.
This repo is unusually careful about it. The rule the author follows (and that this
guide follows): **measure, don't guess.** Every number the cost panel shows is a
*measured* quantity (tokens actually used, tool calls actually made, vCPU-hours
AWS actually reported) multiplied by a published price. The code never invents a
utilization percentage.

**Per-token pricing.** The big cost. Claude Sonnet here is billed about **\$3 per
million input tokens** and **\$15 per million output tokens**. See
`BEDROCK_TOKEN_PRICING` in [config.py](../apps/comparison/config.py).

**Prompt caching.** If you send the *same* big chunk of prompt again (e.g. the
system prompt and tool definitions on every turn), AWS can **cache** it and charge
a fraction of the price to re-read it — but a small premium to first write it.
Cache **read** ≈ 0.1× input price; cache **write** ≈ 1.25× input price. The app
measures four token buckets — fresh input, cache read, cache write, output — and
computes whether caching actually saved money this run.

**Gateway pricing.** The Gateway bills per **1,000 tool operations**, not per
token. Tiny compared to tokens, but the app still counts it.

**Runtime (compute) pricing.** The Custom agent's container bills per
**vCPU-hour** and **GB-hour** of memory. You can't know these until *after* the
run, because AWS reports them asynchronously in special log records
(**USAGE_LOGS**). The app fetches those after the fact — see
[services/runtime_usage.py](../apps/comparison/services/runtime_usage.py).

---

## 6. Observability vocabulary

**Logs / CloudWatch.** AWS's log store. Everything the agents do is logged there.
The app builds deep links and ready-made queries so you can jump straight to a
run's logs.

**Tracing / spans / OpenTelemetry (OTEL).** A "trace" is the timeline of one
request broken into nested **spans** ("the whole invocation" contains "a tool
call" contains "an HTTP request"). **OpenTelemetry** is the industry-standard way
to emit traces. The Custom agent is instrumented with OTEL so you can see its
internal timeline.

**ADOT (AWS Distro for OpenTelemetry).** AWS's build of OpenTelemetry. The Custom
agent's Docker image runs under `opentelemetry-instrument`, which auto-sends traces
to CloudWatch.

**MLflow.** An open-source experiment/trace viewer. Optionally, the Custom agent
*also* mirrors its traces to MLflow (in addition to CloudWatch), so you can inspect
them in a nicer UI. This is why you'll see `MLFLOW_…` env vars and
`_annotate_*_span` helpers in [agent.py](../agents/custom/agent.py).

---

## 7. Infrastructure-as-Code (IaC) vocabulary

**Infrastructure as Code.** Instead of clicking around the AWS console to create
the Gateway, roles, and agents, you *describe* them in code and a tool creates
them. Benefits: repeatable, reviewable, version-controlled.

**Pulumi.** The IaC tool this repo uses. You write ordinary Python
(`__main__.py`) that declares resources ("I want an ECR repo, a Gateway, a
Runtime"), then run `pulumi up` to make AWS match. See
[infrastructure/pulumi/](../infrastructure/pulumi/).

**Stack.** One deployable instance of a Pulumi program with its own config (e.g.
the `production` stack). This repo has three stacks: `gateway`, `managed_agent`,
`custom_agent`.

**StackReference / outputs.** A stack can **export** values (the gateway stack
exports its URL and ARN). Another stack can **reference** those outputs. That's how
the agent stacks learn the Gateway's address without hard-coding it — and why the
gateway stack must be deployed first.

**Import / `ignore_changes`.** These resources existed *before* Pulumi (created by
hand or by the legacy script). Pulumi can "adopt" an existing resource with an
`import_` id instead of creating a duplicate, and `ignore_changes` tells Pulumi not
to overwrite certain fields until you've reviewed them.

**ECR (Elastic Container Registry).** AWS's Docker image store. CI builds the
Custom agent image and pushes it here, tagged with the Git commit SHA so every
deploy is traceable to exact source.

**CI/CD.** *Continuous Integration* = automatically test every change.
*Continuous Deployment* = automatically (or on a button press) ship it. Here CI
(`ci.yml`) runs on every pull request; deployment is three separate **manual**
workflows so nobody deploys to AWS by accident.

---

## 8. Python / tooling vocabulary you'll meet

- **Streamlit** — a Python library that turns a script into a web app. No HTML/JS
  needed; you call `st.button(...)`, `st.markdown(...)`, and it renders. The whole
  comparison UI is Streamlit.
- **boto3 / botocore** — the official AWS SDK for Python. Every AWS call
  (`invoke_harness`, `invoke_agent_runtime`, reading logs) goes through it.
- **Strands** — a lightweight Python framework for building the *agent loop*
  (model + tools + hooks). The Custom agent is a Strands `Agent`.
- **Pydantic** — a library that validates data against typed models. Used in
  [models/events.py](../apps/comparison/models/events.py) to guarantee every event
  has the right shape.
- **TypedDict** — a Python typing feature: a plain `dict` with a documented set of
  keys. The app's mutable "state" and "event" objects are TypedDicts/dicts.
- **Queue / thread** — a `queue.Queue` is a thread-safe mailbox. The app runs each
  agent on its own **thread** and they drop events into a queue that the main
  thread reads. (Needed because Streamlit can only draw from the main thread.)
- **Generator (`yield`)** — a function that produces values lazily, one at a time.
  The Custom agent's entrypoint is a generator: each `yield` becomes one SSE event.
- **Docker** — packages code + dependencies into a portable image that runs the
  same everywhere. The Custom agent ships as a Docker image.
- **uv** — a fast Python package manager. `uv sync` installs the locked
  dependencies; CI uses it.
- **ruff** — a fast linter/formatter for Python. CI checks the code with it.
- **pytest** — the test runner. All unit tests live in `tests/unit/`.

---

## 9. The one idea that ties the whole codebase together

Both agents are wildly different inside, but the app forces them to speak **one
tiny shared vocabulary of six event types**:

```python
{"type": "reasoning",  "content": "..."}          # hidden thinking
{"type": "text",       "content": "..."}          # a piece of the answer
{"type": "tool_start", "name": "...", "input": {}} # about to call a tool
{"type": "tool_end",   "name": "...", "result": ""} # tool returned
{"type": "metrics",    "usage": {...}}             # token counts, tool counts
{"type": "error",      "content": "..."}           # something failed
```

- The **Custom agent** *emits* these directly (it's the producer).
- The **Managed agent adapter** *translates* AWS's stream into these.
- The **UI** *consumes* these and never has to know which agent it's talking to.

Once you understand these six events, the entire data flow makes sense. Every
other doc in this folder refers back to them. Keep them in mind as you read
[the README](README.md) and [01-streamlit-app.md](01-streamlit-app.md).

---

### Where to go next

| If you want to understand… | Read |
|---|---|
| The whole picture + how files connect | [README.md](README.md) |
| The web app you actually click | [01-streamlit-app.md](01-streamlit-app.md) |
| How the two AWS agents are called | [02-backends.md](02-backends.md) |
| The code-defined agent in detail | [03-custom-agent.md](03-custom-agent.md) |
| How AWS resources are created | [04-infrastructure.md](04-infrastructure.md) |
| CI, tests, and the release gate | [05-ci-tests-config.md](05-ci-tests-config.md) |
| Cost, prompt versioning, security | [06-services-and-finops.md](06-services-and-finops.md) |
| The quality/evaluation framework | [07-evaluations.md](07-evaluations.md) |