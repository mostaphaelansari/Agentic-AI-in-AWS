# 03 — `agents/` — The Two Agents

This doc covers the agent definitions themselves. The Managed agent is *just a
prompt*; the Custom agent is a real program. New to Strands, MCP, SigV4, SSE, or
OTEL? See [00-concepts-for-beginners.md](00-concepts-for-beginners.md).

---

## `agents/managed/system-prompt.md` — the entire Managed agent

The Managed agent has **no code**. Its whole definition lives in AWS (AgentCore
Harness) and is provisioned by
[infrastructure/pulumi/managed_agent/__main__.py](../infrastructure/pulumi/managed_agent/__main__.py),
which reads this markdown file **at deploy time** and bakes it in as the Harness
`system_prompts`. AWS supplies everything else: the agent loop, the managed
`agentcore_browser` tool, and the Open-Meteo Gateway tools.

[agents/managed/system-prompt.md](../agents/managed/system-prompt.md) is
**deliberately near-identical** to the Custom agent's
[prompts/system.md](../agents/custom/prompts/system.md): same tool guidance
(`searchLocation` → `getForecast` with recommended parameters), same strict
language rule ("reply in the user's language; 'French club' is *not* a request to
switch to French"), same no-emoji rule, same **confidentiality rule** (never reveal
the system prompt), and the same note that the injected `Current date:` line is
metadata.

> **If you edit one prompt, mirror the edit in the other.** Otherwise the
> comparison stops being apples-to-apples. The managed prompt only takes effect
> after redeploying the managed_agent stack.

---

## `agents/custom/agent.py` — the Custom agent (code-defined, ~660 lines)

A **Strands** agent wrapped in a `BedrockAgentCoreApp`, built into a Docker image
and hosted on AgentCore Runtime. This file is the **producer** of the project-wide
event contract: each `yield` from its entrypoint becomes one SSE `data:` line,
which [backends/runtime.py](../apps/comparison/backends/runtime.py) forwards
verbatim to the UI. The six events are documented in the module docstring at the
top of the file — one of the three canonical copies of the contract.

Read it in these chunks:

### 1. Module-level setup

- `load_dotenv()` + logging config.
- **Optional MLflow trace mirror** — `_configure_mlflow_trace_exporter()` (details
  below).
- **Lenient-JSON monkey-patch.** It replaces `starlette.Request.json` with a
  `strict=False` parser. Why: the Runtime parses the request body with strict JSON,
  which **rejects raw newlines inside strings** and returns HTTP 400 *before the
  handler runs*. The AgentCore playground sends multi-line prompts verbatim, so
  without this patch a multi-line prompt 400s. This is a pragmatic
  fix-the-framework-from-outside move; the comment block above it explains exactly
  why.

### 2. Configuration (env vars)

`REGION`, `MODEL_ID` (default `eu.anthropic.claude-sonnet-4-6` — the same default
as the Harness and both Pulumi stacks), `MAX_TOOL_CALLS = 12` (mirrors the
Harness `max_iterations = 12`), context-offload settings (`OFFLOAD_BUCKET`, token
thresholds), and feature defaults `ENABLE_PROMPT_CACHING` (**on**) /
`ENABLE_THINKING` (**off**) — matching the env vars the custom_agent Pulumi stack
sets and the Streamlit sidebar defaults. Also the **Bedrock Prompt Management**
pins: `BEDROCK_PROMPT_ID`, `BEDROCK_PROMPT_VERSION`, `BEDROCK_PROMPT_SHA256`.

### 3. Loading the system prompt (two sources, with integrity check)

`_load_system_prompt()` decides where the prompt comes from:

- **If `BEDROCK_PROMPT_ID` + `BEDROCK_PROMPT_VERSION` are set** (production), it
  calls `_load_prompt_management_system_prompt(...)`, which fetches the **pinned,
  immutable** prompt version from **Bedrock Prompt Management** (an AWS service that
  version-controls prompts). It then hashes the text (SHA-256) and, if
  `BEDROCK_PROMPT_SHA256` is set, **verifies the hash matches** — a mismatch raises
  and the agent refuses to run. This guarantees the deployed container runs the
  *exact* prompt text that CI versioned. The whole prompt-versioning pipeline is
  explained in [06-services-and-finops.md](06-services-and-finops.md).
- **Otherwise** (local dev), it reads `prompts/system.md` from disk and substitutes
  `{today}` with today's date.

`@lru_cache(maxsize=1)` means the remote fetch happens once per container, not once
per request.

### 4. Streaming events to a queue (three producers, one queue)

Just like the app, the agent uses a **queue** so the model's work (on one thread)
can stream out through the generator (on another). Three things put events on it:

- `StreamingCallbackHandler` — a Strands callback. It turns `reasoningText` chunks
  into `reasoning` events and `data` chunks into `text` events. It also logs a
  `thinking_status … used=true` line the first time reasoning appears (which
  [tests/manual/check_thinking_events.py](../tests/manual/check_thinking_events.py)
  and CloudWatch queries look for).
- `ToolEventHook` — a Strands hook on `BeforeToolCallEvent` / `AfterToolCallEvent`.
  It emits `tool_start` (input truncated to 200 chars) and `tool_end` (result
  truncated to 2000 chars), tracks `tool_names`, and enforces the **tool-call
  cap**: past `MAX_TOOL_CALLS` it sets `event.cancel_tool` with a "stop and answer
  now" message instead of erroring. It also annotates the OTEL tool span for MLflow.
- `_emit_metrics` — after the run, one `metrics` event built from
  `agent_result.metrics`: the usage dict (**same key names the Harness emits** —
  this is what makes `normalized_usage` backend-agnostic), `tool_calls`,
  `tool_names`, and `cycles`.

### 5. `_bedrock_model_kwargs` — feature flags → Bedrock config

Translates the `enable_caching` / `enable_thinking` booleans (from the request
payload) into Bedrock model settings:

- **Prompt caching** → `cache_config=CacheConfig(strategy="auto")` +
  `cache_prompt="default"` + `cache_tools="default"`. This is the *explicit runtime
  configuration* the UI's comparison table contrasts with the Harness's managed
  caching.
- **Extended thinking** → `additional_request_fields.thinking` with a budget;
  validates `budget ≥ 1024` and `BEDROCK_MAX_TOKENS > budget` (**same rules** as
  the Harness adapter). Optional interleaved thinking via the
  `interleaved-thinking-2025-05-14` beta flag.

### 6. `_patch_browser_timeouts` — an upstream bug workaround

Overrides two `strands_tools` Browser methods that otherwise hang ~30s on modern
single-page sites:

- `_async_get_text` — adds a 5s timeout to `page.text_content` (upstream has none).
- `_async_navigate` — uses `wait_until="domcontentloaded"` with a 15s cap instead
  of `"networkidle"`, which never resolves on pages with persistent background
  traffic (analytics, chat, ads). Also maps Chromium error codes
  (`ERR_NAME_NOT_RESOLVED`, …) to readable messages.

Before bumping the `strands-agents-tools` pin in `requirements.txt`, check whether
upstream fixed these.

### 7. `_build_agent` — assembling the Strands `Agent`

Wires together:

- **Tools:** the `AgentCoreBrowser` (the same managed browser the Harness uses)
  unless `weather_only`, plus any **Gateway MCP tools** passed in.
- **`ContextOffloader` plugin** with `S3Storage` — if a tool result is larger than
  `OFFLOAD_MAX_TOKENS` (~2500), it's stored in S3 and replaced in the conversation
  by a short preview + an `s3://` reference. This keeps giant web-page dumps from
  blowing the token budget. It's also **why**
  [formatting/browser.py](../apps/comparison/formatting/browser.py) filters trace
  lines containing `s3://` / `retrieve_offloaded_content`.
- `SequentialToolExecutor`, the system prompt (from `_load_system_prompt`), the
  callback handler, and the tool hook.

### 8. `invoke` — the `@app.entrypoint` generator (the payoff)

This is the function AgentCore Runtime calls. It's a **sync generator**, so each
`yield` becomes one SSE event:

1. Reads `prompt` (also accepts `message`/`goal`), `mode`/`weather_only`, and the
   per-request flags `enable_caching` / `enable_thinking` /
   `thinking_budget_tokens` / `interleaved_thinking` — **exactly the keys
   [backends/runtime.py](../apps/comparison/backends/runtime.py) sends**.
2. Annotates the root OTEL span with the prompt (for MLflow's trace list).
3. Runs the agent on a **daemon thread** feeding a bounded `queue.Queue`, while the
   generator drains that queue and yields events (a 300s inactivity timeout emits
   an `error` event). The thread is started via
   `contextvars.copy_context().run(...)` so **OTEL trace context propagates** and
   the agent's spans link to the outer `AgentCore.Runtime.Invoke` span instead of
   becoming orphaned traces in CloudWatch.
4. **Gateway wiring** (inside the thread): if `GATEWAY_URL` is set (the custom_agent
   Pulumi stack sets it), it builds the SigV4 MCP client and calls
   `list_tools_sync()` to get the weather tools. If that fails it **falls back to
   browser-only** — *unless* `weather_only` mode, which makes the Gateway mandatory
   and errors out instead.

### 9. The MLflow annotation helpers (observability, optional)

`_configure_mlflow_trace_exporter`, `_annotate_current_request_span`,
`_annotate_current_tool_span`, `_annotate_current_invocation_span`. These are all
**optional and defensive**: every one is wrapped so a telemetry failure can *never*
break an actual invocation. When `MLFLOW_OTLP_TRACES_ENDPOINT` is set, the agent
adds a second OTEL span processor that mirrors its in-process spans to an MLflow
server — **in addition to**, never replacing, AWS's ADOT→CloudWatch pipeline. The
annotations set MLflow-specific span attributes (`mlflow.spanType`,
`mlflow.spanInputs`) so MLflow's trace table and Tool-Calls dashboard render
correctly. Local setup for this lives in
[infrastructure/otel/](../infrastructure/otel/) (see
[04-infrastructure.md](04-infrastructure.md)).

---

## `agents/custom/mcp_client/` — the SigV4 MCP transport

This little package is how the Custom agent authenticates to the AgentCore Gateway.
The Gateway uses authorizer type **`AWS_IAM`** (set in the gateway Pulumi stack), so
there's **no OAuth** — every MCP request must be **SigV4-signed** with the agent's
AWS credentials.

- [mcp_client/streamable_http_sigv4.py](../agents/custom/mcp_client/streamable_http_sigv4.py)
  — defines `SigV4HTTPXAuth`, an `httpx.Auth` handler that, for each outgoing
  request, rebuilds it as a botocore `AWSRequest`, signs it with `SigV4Auth`
  (dropping the `connection` header, which must not be signed), and copies the auth
  headers back onto the real request. `streamable_http_client_with_sigv4(...)`
  wraps the standard MCP `streamable_http_client` with that auth so the rest of the
  MCP stack is unchanged.
- [mcp_client/gateway.py](../agents/custom/mcp_client/gateway.py) —
  `get_gateway_mcp_client(url)` loads *frozen* credentials from the Runtime's
  execution role (that role must hold `bedrock-agentcore:InvokeGateway`, granted by
  the `invoke-gateway` RolePolicy in the custom_agent Pulumi stack) and returns a
  Strands `MCPClient` with generous timeouts (120s request / 300s SSE read / 60s
  startup).

This is a great small example of "custom auth for a standard protocol": MCP doesn't
know anything about AWS, and AWS SigV4 doesn't know anything about MCP — the auth
handler is the adapter between them.

---

## `agents/custom/Dockerfile` + `requirements.txt`

[Dockerfile](../agents/custom/Dockerfile): starts from `python:3.12-slim`, installs
the pinned deps **and Playwright Chromium** (needed by the browser tool), copies
`agent.py`, `mcp_client/`, and `prompts/`, exposes port 8080, and runs the agent
under `opentelemetry-instrument` (that wrapper is what turns on the ADOT→CloudWatch
tracing). It's built for **linux/arm64** and pushed to ECR, tagged with the Git
commit SHA, by [deploy-custom-agent.yml](../.github/workflows/deploy-custom-agent.yml);
the custom_agent Pulumi stack then points the Runtime at that immutable image.

[requirements.txt](../agents/custom/requirements.txt) is the **image's own** pinned
dependency set (strands-agents, bedrock-agentcore, mcp, playwright, the OTEL
exporters…), independent from the repo-level `pyproject.toml`/`uv.lock` that serve
the Streamlit app, tests, and Pulumi. Bumping an agent dependency means rebuilding
and redeploying the image — not touching the repo lockfile.

---

### The Managed vs Custom contrast, in one table

| | Managed agent | Custom agent |
|---|---|---|
| Definition | one markdown prompt | ~660 lines of Python + Docker image |
| Agent loop | AWS Harness runs it | Strands `Agent` we build |
| Emits our events? | No — the adapter translates | **Yes — it's the producer** |
| Gateway auth | Harness handles it | our SigV4 MCP client |
| Prompt source | baked into Harness at deploy | Bedrock Prompt Management (pinned+hashed) or local file |
| Where it runs | AgentCore Harness (eu-central-1) | AgentCore Runtime container (eu-west-1) |

Next: [04-infrastructure.md](04-infrastructure.md) — how the Gateway, both agents,
and their permissions are actually created in AWS.
