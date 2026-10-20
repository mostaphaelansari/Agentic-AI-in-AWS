# Custom Agent Design

This is a living design document for the open-source custom agent. It records
the reasoning behind its instruction architecture and safety controls. As the
design evolves, relevant conclusions from the design conversation should be
added here rather than left only in chat history.

## TL;DR

We evolved the agent in layers:

1. **We started with one system prompt.** It contained the agent identity,
   permanent rules, tool descriptions, and every task procedure.
2. **We added `SKILL.md` files.** Universal instructions stayed in the system
   prompt, while task-specific workflows moved into conditional skills such as
   forecast lookup, city comparison, map research, web research, and code
   execution.
3. **We added runtime skill selection.** For each request, the service matches
   prompt keywords and appends only the relevant skill bodies to the system
   prompt.
4. **We standardized every skill.** Each `SKILL.md` now explains when to use
   it, the required outcome, the procedure to follow, and what must never be
   done.
5. **We added DDGS web search through MCP.** The `webSearch` tool gives the
   agent current public titles, URLs, and snippets without a paid search API.
6. **We added OpenStreetMap through MCP.** Nominatim resolves named places and
   Overpass finds nearby attractions, food, lodging, parks, and transport.
7. **We added optional travel MCPs.** Booking.com handles hotel search,
   AeroDataBox handles flight status/airport boards, Air Scraper handles flight
   shopping/search, and Geoapify handles geocoding when credentials are set.
8. **We added a code interpreter.** The agent receives one Python-only Strands
   tool backed by an isolated, resource-limited Open Terminal container.
9. **We added deterministic service-boundary guardrails.** They reject
   high-confidence prompt attacks, redact common PII and credentials, and
   restrict unsafe code-interpreter calls without a model dependency.
10. **We added LiteLLM Auto Router.** Its heuristic complexity classifier sends
    simple turns to local Ollama and higher-complexity turns to Kimi, with one
    model pinned across the complete tool loop.
11. **We kept the remaining features optional and testable.** Skills degrade to
   the base system prompt when unavailable.

In short:

```text
one large system prompt
    -> permanent system prompt + conditional skills
    -> composed instructions + isolated code execution + LLM
```

## From one system prompt to system prompt plus skills

The agent originally used one static system prompt containing everything:

- its identity and permanent rules;
- its tool inventory;
- exact weather tool-call procedures;
- required forecast parameters;
- city-name normalization rules;
- multi-city comparison behavior.

That complete prompt was sent on every request, including requests for which
the weather procedures were irrelevant.

In commit `ad20ea0` (2026-07-20), the prompt was split into two layers.

### Permanent system prompt

[`prompts/system.md`](prompts/system.md) contains instructions that must apply
to every request:

- the agent's identity and general capabilities;
- language, style, and confidentiality rules;
- current-date handling;
- the available tool inventory and universal tool-safety rules;
- general answer style.

These rules must not be moved into a conditional skill. If a skill is not
selected, its instructions cannot protect or constrain the agent.

### Conditional skills

Task-specific workflows live in `skills/<name>/SKILL.md`:

- [`forecast-lookup`](skills/forecast-lookup/SKILL.md) defines how to retrieve
  current weather and forecasts;
- [`compare-cities`](skills/compare-cities/SKILL.md) defines how to compare
  separately retrieved weather data;
- [`map-researcher`](skills/map-researcher/SKILL.md) defines how to resolve
  locations and discover nearby points of interest;
- [`web-research`](skills/web-research/SKILL.md) defines how to verify current
  public facts.
- [`code-interpreter`](skills/code-interpreter/SKILL.md) defines when exact
  computation is required and how to use executed Python results safely.

In this repository, `SKILL.md` is a convention implemented by our own Python
loader. It is not an Ollama or Strands feature, and it is not an
Anthropic-only feature. Other agent platforms, including OpenAI Codex, also use
skills as reusable task workflows. We could rename the files if we changed the
loader; the behavior comes from the runtime, not from the filename itself.

Each skill answers four questions:

1. **When should I use this skill?**
2. **What outcome am I responsible for?**
3. **What procedure should I follow?**
4. **What must I never do?**

The frontmatter provides the skill name, activation description, and keywords.
The Markdown body uses `When to use`, `Outcome`, `Procedure`, and `Never`
sections to make the task contract explicit.

Generic role statements such as "You are a weather expert" are not sufficient
skill instructions. They do not define activation, a deliverable, a procedure,
or boundaries. Permanent identity belongs in the system prompt; a skill should
describe observable expert behavior for one kind of task.

### Runtime composition

The loader in [`service.py`](service.py) performs deterministic, server-side
injection:

1. Load `prompts/system.md` at service startup.
2. Rescan every `skills/*/SKILL.md` for each request.
3. Parse the frontmatter and body.
4. Lowercase the user prompt and match it against each skill's keywords.
5. Append every matching skill body to the permanent system prompt.
6. Add the runtime-provided current date.
7. Give the composed prompt to the Strands agent.

Conceptually:

```text
effective system prompt
    = permanent system prompt
    + current date
    + zero or more matching skill bodies
```

Examples:

```text
"Will it rain in Lisbon tomorrow?"
    -> system prompt + forecast-lookup

"Compare today's weather in Paris and Casablanca."
    -> system prompt + forecast-lookup + compare-cities

"Explain the ReAct pattern."
    -> system prompt only
```

The server selects skills instead of asking the small local model to call a
meta-tool. This reduces irrelevant context and makes selection predictable.
The trade-off is that keyword matching can miss unusual wording or activate on
an ambiguous word; it is not semantic routing.

Skill files are rescanned for every request, so edits take effect without
restarting the service. A missing skills directory produces a system-prompt-only
agent, and a malformed skill is logged and skipped.

## LiteLLM Auto Router

Model selection is separate from skill selection. `service.py` builds the
official LiteLLM Python `Router` with an `auto_router/complexity_router` model:

- `SIMPLE` routes to `ollama_chat/qwen3:1.7b` by default;
- `MEDIUM`, `COMPLEX`, and `REASONING` route to `openai/kimi-k2.6` through
  Moonshot's OpenAI-compatible endpoint;
- provider failure falls back once to the other model group.

The classifier is LiteLLM's zero-call heuristic scorer, not the repository's
former KNN router and not the skill-keyword matcher described above. A unique
affinity ID is created for each user turn. LiteLLM therefore classifies the
turn once and keeps all follow-up model cycles after tool results on the same
provider, while the next user turn receives a fresh routing decision.
FastAPI's lifespan constructs the cached router before readiness, moving the
one-time LiteLLM import cost out of the first user request. Ollama inference
warm-up remains opt-in through `OLLAMA_WARMUP_ENABLED` because keeping the local
model resident consumes memory even when a turn routes to Kimi.

Strands' `LiteLLMModel` already translates OpenAI-format text, reasoning, and
tool-call deltas into the agent event loop. `litellm_auto_router.py` subclasses
that formatter only to invoke `Router.acompletion()` instead of the global
single-model completion function. The final SSE metrics event exposes the
actual model returned by LiteLLM.

## Adding DDGS and OpenStreetMap through MCP

DDGS web search and OpenStreetMap place discovery are remote MCP tools. They
are implemented in [`mcp-tools/server.py`](../../mcp-tools/server.py), served
over streamable HTTP at `/mcp`, and discovered by the agent through `MCP_URL`.

For each request, `service.py` starts a Strands `MCPClient`, asks the server for
its tool list, and gives the returned tool specifications to the model. This is
different from the code interpreter, whose wrapper is registered locally:

```text
mcp-tools container
    -> searchLocation
    -> getForecast
    -> webSearch
    -> searchMapPlaces
    -> findNearbyPlaces

agent container
    -> local code_interpreter wrapper
```

### DDGS web search

`webSearch` uses the Python `ddgs` package to search public web indexes. It
accepts a focused query, result count, region, and optional day/week/month/year
recency filter.

The server applies several boundaries before returning results:

- queries must be non-empty and no longer than 500 characters;
- result count is clamped to the configured maximum, 10 by default and at most
  20;
- timeout is configurable from 1 to 30 seconds;
- moderate safe search is enabled;
- only valid HTTP and HTTPS result URLs are retained;
- titles are limited to 300 characters, URLs to 2,000, and snippets to 2,000.

The result is a small JSON document containing the query plus titles, source
URLs, and snippets. It is discovery evidence rather than fetched page content.
DDGS depends on upstream public search services, so it can be rate-limited and
does not provide a production availability guarantee.

Search results are untrusted data. The permanent system prompt and
`web-research` skill tell the model never to follow instructions found in a
snippet, to prefer primary sources, and to disclose search failures or source
conflicts rather than guess.

### OpenStreetMap place discovery

OpenStreetMap support is split into two MCP tools because resolving a place and
searching around coordinates are different operations.

`searchMapPlaces` calls Nominatim. Given a place, landmark, or address, it
returns up to 10 candidates with names, coordinates, OSM identifiers, category,
and type. The response keeps the required OpenStreetMap attribution.

`findNearbyPlaces` calls Overpass. It accepts coordinates—normally selected
from `searchMapPlaces`—plus a bounded radius and one supported category:

```text
attractions, cafes, hotels, museums, parks,
pharmacies, restaurants, transport
```

The server constructs a bounded Overpass query, ignores unnamed elements,
handles node/way/relation centers, calculates distance with the haversine
formula, sorts nearest first, and returns at most 20 places. Results can include
an OSM URL, website, and opening-hours tag.

To respect shared public infrastructure, Nominatim requests carry an identifying
user agent, uncached requests are serialized to at most one per second, and
responses are cached for one hour by default. Overpass is configured with two
public endpoints and retries the second after rate-limit, gateway, availability,
timeout, or connection failures. Production or material traffic should use a
provider or self-hosted endpoints.

OpenStreetMap is community-maintained. Place entries, websites, and opening
hours can be incomplete or stale, so the map-researcher skill treats them as
discovery leads and uses web search or official sources to verify critical
details.

### How skills coordinate the MCP tools

Tool registration tells the model that `webSearch`, `searchMapPlaces`,
`findNearbyPlaces`, and optional remote travel tools exist. Skills provide
task-specific orchestration:

- `web-research` selects `webSearch` for current or source-backed facts;
- `map-researcher` resolves a location with Geoapify when available, discovers
  nearby places through OpenStreetMap, and verifies critical operational
  details with web search when necessary;
- `booking-search` uses Booking.com MCP for accommodation inventory;
- `flight-search` uses Air Scraper for flight shopping/search and AeroDataBox
  for operational status or airport boards.

A typical map-research flow is:

```text
natural-language place-research request
    -> map-researcher skill injected
    -> searchMapPlaces(destination)
    -> findNearbyPlaces(coordinates, category)
    -> webSearch(official/current verification)
    -> focused place results with distances and verification status
```

This is deliberately not called a travel planner. The current tools can locate
places and research nearby points of interest, but they do not provide routing,
transport schedules, inventory, booking, or itinerary optimization. Naming the
skill `map-researcher` keeps its promise aligned with its actual capabilities.

If the MCP server is unreachable, the agent falls back only to its local
weather tools. DDGS, OpenStreetMap, Booking.com, AeroDataBox, Air Scraper, and
Geoapify have no in-process fallback and are therefore unavailable until their
MCP connections are restored.

## Adding the code interpreter

The code interpreter gives the model exact computation without allowing it to
run code inside the agent service. Its implementation is split across two
containers:

```text
user asks for a calculation
    -> LLM selects the `code_interpreter` Strands tool
    -> agent sends Python to Open Terminal over internal HTTP
    -> Open Terminal runs it in its isolated container
    -> bounded JSON result returns to the agent
    -> LLM explains the result to the user
```

### Conditional tool registration

[`code_interpreter.py`](code_interpreter.py) exposes
`build_code_interpreter_tool(session_id)`. The service registers the resulting
tool only when `OPEN_TERMINAL_URL` is configured. With an empty URL, the model
does not receive the tool at all.

The code interpreter is not supplied by the MCP server. The agent currently
has two tool sources:

```text
MCP tools
    -> weather, web search, and OpenStreetMap

local Strands tool
    -> code_interpreter
```

`service.py` retrieves the remote MCP tool list and then appends the locally
built `code_interpreter` tool. Keeping it local lets the wrapper bind execution
to the current session, enforce code and output limits, handle timeouts, and
keep Open Terminal's broader API hidden from the model.

The [`code-interpreter` skill](skills/code-interpreter/SKILL.md) is separate
from the executable tool. The Python function gives the model the capability
to execute code; the skill provides conditional guidance about when and how to
use it. Calculation and data-processing keywords cause the service to inject
the skill, while the local Strands tool remains the execution mechanism.

This separation lets users request an outcome naturally, such as "calculate
the mean and standard deviation," without naming an internal tool. The skill
instructs the agent to select `code_interpreter`, verify the returned status,
and base its answer on successful execution. Adding the skill changes
instructions, not tool transport; the interpreter remains a local tool rather
than an MCP tool.

The tool accepts one argument, `code`, containing a complete Python program.
Its description tells the model to use it for arithmetic, statistics, data
transformation, or executable verification and to print the values needed for
the answer.

### How the model knows the tool exists

The skill does not create or advertise executable capability by itself. During
agent construction, Strands receives the decorated Python function in its
`tools` list. Strands converts that function into a tool specification containing
approximately:

```json
{
  "name": "code_interpreter",
  "description": "Execute Python for arithmetic, statistics, data transformation, or verification.",
  "input": {
    "code": "Complete Python program. Print the values needed for the answer."
  }
}
```

The Ollama request therefore contains both independent inputs:

```text
effective system prompt
    -> includes the matching code-interpreter skill

available tool specifications
    -> includes the executable code_interpreter function and its code argument
```

The skill teaches the model when and how to use the capability. The registered
tool specification tells the model that the capability exists and gives it the
callable name and argument schema. The model can then emit a structured tool
call such as:

```json
{"name":"code_interpreter","input":{"code":"print(sum([12, 17, 19]))"}}
```

Strands intercepts that structured call, invokes the local Python wrapper, adds
the Open Terminal result to the conversation, and asks the model to produce the
final answer.

There is one configuration edge case: skill matching is currently independent
of tool availability. If `OPEN_TERMINAL_URL` is empty, the service omits the
tool but a calculation keyword can still inject the skill. The model would then
receive instructions to use a tool that is absent. The normal Compose stack
sets `OPEN_TERMINAL_URL`, so the tool is present there, but robust standalone
behavior should make skill injection capability-aware or keep a clear fallback
instruction for unavailable execution.

Each tool instance is bound to the current conversation `session_id`. That ID
is forwarded to Open Terminal as `X-Session-ID`, keeping execution workspaces
separate across conversations.

### Request and execution flow

`execute_python()` validates the request before contacting Open Terminal:

1. Return a disabled message if `OPEN_TERMINAL_URL` is empty.
2. Reject empty source code.
3. Reject source longer than `CODE_INTERPRETER_MAX_CODE_CHARS` (20,000 by
   default).
4. Base64-encode the Python source.
5. Send `POST /execute` with a bounded wait time and output tail.
6. Serialize and truncate the returned JSON to
   `CODE_INTERPRETER_MAX_OUTPUT_CHARS` (12,000 by default).

The generated command has this shape:

```text
python -c "import base64;exec(base64.b64decode('<encoded source>'))"
```

Base64 prevents quotes, semicolons, command substitutions, and other characters
inside model-generated source from being interpolated as shell syntax. The
decoded content is executed by Python inside the Open Terminal container.

The HTTP request optionally carries `OPEN_TERMINAL_API_KEY` as a bearer token.
Connection failures and non-success HTTP responses become bounded tool errors
that the agent can report rather than crashing the FastAPI service.

### Code walkthrough

`code_interpreter.py` has three layers:

1. `_request()` is the private HTTP client for Open Terminal.
2. `execute_python()` validates code, manages execution, timeouts, and bounded
   output.
3. `build_code_interpreter_tool()` turns that executor into a session-bound
   Strands tool visible to the model.

Configuration is read once when the module is imported. The URL selects the
Open Terminal service, the optional API key authenticates calls, timeout is
clamped to 1–60 seconds, and separate limits bound input source and returned
output.

`_request()` adds `X-Session-ID` to every request so Open Terminal can isolate
conversation workspaces. It JSON-encodes an optional request body, adds bearer
authentication when configured, and calls the requested Open Terminal endpoint.
The HTTP client waits five seconds longer than the requested execution window
so the service has time to return its timeout response. HTTP and connection
errors are converted into `RuntimeError` messages suitable for a tool failure.

`_python_command()` base64-encodes model-generated source before inserting it
into the wrapper command. This prevents quotes, semicolons, `$()`, and similar
characters in the source from escaping into the wrapper shell command. Base64
is not a sandbox and does not make the Python program safe: after decoding, the
program can exercise whatever permissions exist inside the Open Terminal
container. The container restrictions provide the security boundary.

`execute_python()` rejects disabled, empty, and oversized requests, then sends:

```text
POST /execute?wait=<seconds>&tail=200
X-Session-ID: <conversation id>
{"command":"python -c ..."}
```

`wait` asks Open Terminal to wait for completion up to the configured deadline,
while `tail=200` bounds the returned process-output tail. If the service still
reports `running`, the wrapper sends a force-delete request for that process and
returns a synthetic `timed_out` result. Finally, it serializes the service
response to JSON and truncates the string before returning it to the model.

`build_code_interpreter_tool(session_id)` uses a closure: the nested tool
function remembers the session ID supplied when the agent was built. The
`@tool(name="code_interpreter")` decorator makes the function name, docstring,
and typed `code` argument available to Strands as a model-callable schema. When
the model emits that structured tool call, Strands calls the nested function,
which forwards to `execute_python()` with the captured session ID.

Important implementation limits remain: there is no retry, malformed or
truncated JSON from Open Terminal can fail parsing, and failure of the
force-delete request is intentionally ignored even though the returned message
says the process was terminated. These are acceptable for the current local
comparison stack but should be tightened for a stronger production contract.

### Timeout handling

`CODE_INTERPRETER_TIMEOUT` defaults to 15 seconds and is clamped between 1 and
60 seconds. If Open Terminal reports that a process is still running after the
wait period, the agent sends:

```text
DELETE /execute/<process-id>?force=true
```

The returned tool result is then marked `timed_out`. This prevents an infinite
loop from silently continuing after the model's tool call has finished.

### Container boundary

Docker Compose runs `open-terminal` separately from the agent. The model sees
only the Python wrapper; Open Terminal's broader shell and file APIs are not
registered as model tools.

The execution container has:

- a read-only root filesystem;
- temporary `/tmp` and `/home` filesystems;
- no Docker socket or host filesystem mount;
- no-new-privileges enabled;
- all Linux capabilities dropped except those needed to enter its
  unprivileged runtime user;
- limits of 128 processes, 512 MB memory, and one CPU.

This is useful isolation for the local comparison stack, but it is not claimed
to be a hostile multi-tenant production sandbox.

### How to test it

Run the focused unit tests from `opensource/`:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  ../tests/unit/test_opensource_code_interpreter.py -q
```

These tests do not need a running container. They verify:

- untrusted source is base64-encoded instead of interpolated into the shell;
- execution sends the correct session ID and returns the service result;
- a process that exceeds the timeout is force-killed;
- a missing `OPEN_TERMINAL_URL` disables execution cleanly.

For an end-to-end test, start the Compose stack and pull the model once:

```bash
cd opensource/compose
docker compose up -d --build
docker compose exec ollama ollama pull qwen3:1.7b
```

The agent is internal by default. To call it from the host, create
`docker-compose.override.yml` in `opensource/compose`:

```yaml
services:
  agent:
    ports:
      - "127.0.0.1:18080:8080"
```

Recreate the agent, then make a request that explicitly needs computation:

```bash
docker compose up -d agent open-terminal

curl -N -X POST http://127.0.0.1:18080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Use the code interpreter to calculate the mean and standard deviation of [12, 17, 19, 23, 29]. Show the result.","session_id":"code-test-1"}'
```

A successful SSE stream contains a `tool_start` event named
`code_interpreter`, a corresponding `tool_end` result containing printed
Python output, and then the model's text answer. If the tool is missing, check
that the agent container has `OPEN_TERMINAL_URL=http://open-terminal:8000` and
that both containers are running:

```bash
docker compose ps agent open-terminal
docker compose logs --tail=100 agent open-terminal
```

To exercise timeout handling end to end, ask the agent explicitly to run an
infinite loop only in a disposable local stack, or lower
`CODE_INTERPRETER_TIMEOUT` and use a deliberately slow finite program. The
unit test is the preferred safe and deterministic timeout check.

## Local service-boundary guardrails

The earlier GLiNER2 experiment was removed because optional torch-sized
dependencies could be absent while the service continued without enforcement.
The replacement in `guardrails.py` has no model, network, or optional-package
dependency and is enabled by default, so its behavior is identical with Ollama
and Kimi.

The request boundary enforces prompt size and valid text, blocks
high-confidence instruction override, system-prompt exfiltration, and malware
execution requests, then redacts common PII and credentials before session or
model access. The tool boundary redacts arguments/results and cancels unsafe
code-interpreter operations. Streamed reasoning and answer chunks are also
redacted before being emitted to clients. `/ping` reports guardrail status so
deployment checks can verify enforcement is enabled.

Policy blocks return explicit `guardrail_blocked` errors rather than silently
rewriting the request. PII/credential redaction is reported only by category in
logs; the sensitive value itself is never logged. Unit tests cover allowed,
blocked, redacted, oversized, non-text, tool, SSE, and AG-UI paths.

## Docker Compose stack

[`compose/docker-compose.yml`](../../compose/docker-compose.yml) is the
deployment source of truth for the local open-source stack. Compose places all
services on one private network, where service names become DNS names such as
`agent`, `ollama`, `mcp-tools`, and `open-terminal`.

The services have these responsibilities:

| Service | Responsibility |
|---|---|
| `ui` | Streamlit interface exposed to the host on port 8501 |
| `agent` | FastAPI/Strands orchestration, skills, sessions, and SSE |
| `ollama` | Local `qwen3:1.7b` model inference |
| `mcp-tools` | Weather, DDGS web-search, and OpenStreetMap MCP tools |
| optional remote MCPs | Booking.com hotels, AeroDataBox status/airport boards, Air Scraper flight search, Geoapify geocoding |
| `open-terminal` | Isolated Python execution for `code_interpreter` |
| `mlflow` | Durable SDK traces and artifacts |
| `otel-collector` | Receives OpenTelemetry spans and forwards observability data |

A normal request follows:

```text
browser -> ui:8501 -> agent:8080
                         |-> LiteLLM Auto Router
                         |      |-> ollama:11434 (SIMPLE)
                         |      `-> api.moonshot.ai (higher tiers)
                         |-> mcp-tools:8000/mcp
                         |-> open-terminal:8000
                         |-> mlflow:5000
                         `-> otel-collector:4318 -> mlflow
```

Compose expressions such as `${MODEL_ID:-qwen3:1.7b}` mean "use the
environment or `.env` value when set; otherwise use this default." `expose`
makes a port available only to other Compose services, whereas `ports`
publishes it to the host. Consequently, the UI, MLflow, and OTel receivers are
host-accessible by default, while the agent, model, MCP server, and code runner
remain internal.

`ollama` stores downloaded model weights in the named `ollama` volume.
`agent-sessions` preserves conversation JSON files. MLflow deliberately uses a
host bind mount at `compose/data/mlflow`, so traces survive even
`docker compose down -v` and Compose project-name changes.

The stack uses two separate shared secrets:

- `API_KEY` authenticates UI-to-agent calls to `/invocations` and `/agui`;
- `OPEN_TERMINAL_API_KEY` authenticates agent-to-Open-Terminal calls and is
  never exposed in the model's tool schema.

Open Terminal is additionally constrained with a read-only root filesystem,
temporary `/tmp` and `/home`, no-new-privileges, dropped capabilities, and
process, memory, and CPU limits.

The observability variables intentionally split two paths: MLflow SDK traces
go directly to `mlflow:5000`, while Strands OpenTelemetry spans use HTTP/Protobuf
to `otel-collector:4318`. `MLFLOW_ENABLE_OTLP_EXPORTER=false` prevents MLflow
from mistakenly taking over the Strands OTLP endpoint and trying an unavailable
gRPC exporter.

`depends_on` controls startup ordering. Only the MLflow dependency has a health
condition; the other `service_started` conditions confirm container startup,
not full application readiness. Runtime connection handling must therefore
still tolerate a dependency that has started but is not yet ready.

## Agent memory and conversation sessions

The agent has conversation history, not semantic long-term memory. There is no
vector database, embedding retrieval, user-profile memory, or automatic summary
of older conversations. Continuity is keyed entirely by `session_id`.

The Streamlit UI generates one stable session ID per active chat and sends it
with every `/invocations` request. Follow-up messages reuse that ID. Starting a
new chat or clearing UI history rotates to a new ID. The AG-UI endpoint uses
`thread_id` as the same conversation session ID. Direct API callers can send
`session_id` in JSON or `X-Session-ID`; when neither is present, the endpoint
generates a fresh random ID for that request so unrelated anonymous requests
never share a default conversation.

When `SESSIONS_DIR` is configured, `service.py` constructs a Strands
`FileSessionManager`. It stores data approximately as:

```text
/data/sessions/
└── session_<session-id>/
    ├── session.json
    └── agents/
        └── agent_<agent-id>/
            ├── agent.json
            └── messages/
                ├── message_0.json
                ├── message_1.json
                └── ...
```

The message files contain user, assistant, and tool-use/tool-result conversation
messages. `agent.json` stores agent and conversation-manager state. Atomic
temporary-file replacement is used when writing JSON.

On each request, a new in-process Strands `Agent` object is built. The session
manager finds the matching session and agent files, restores recent messages,
and persists new messages as the turn progresses. The agent instance is
temporary; the JSON session is the durable memory.

`SlidingWindowConversationManager(window_size=20)` limits the active context to
the newest 20 messages. A message is not the same as a user turn: assistant
tool calls and user-role tool results also count. Strands preserves valid
tool-use/tool-result pairs while trimming. Older JSON message files can remain
on disk, but the stored removed-message count causes future restores to skip
them. They are not summarized or semantically retrieved, so once outside the
window they no longer influence the model.

Compose sets `SESSIONS_DIR=/data/sessions` and mounts the `agent-sessions` named
volume there. Conversation memory therefore survives agent-container
recreation and normal `docker compose down`, but `docker compose down -v`
removes the named volume. With an empty `SESSIONS_DIR`, `_session_manager()`
returns `None` and every request is stateless even when a session ID is sent.
A corrupt or unwritable session store logs an error and also degrades to a
stateless request.

Guardrails execute before the session-aware event stream. Blocked prompts are
never stored, and detected PII is replaced before the sanitized prompt reaches
the session manager. `user_id` and `session_id` are also attached to MLflow for
trace filtering, but MLflow is observability rather than conversational memory.

The same session ID is forwarded to Open Terminal to separate execution
workspaces, but that workspace is not the agent's language memory and has no
persistent volume. The UI's local run-history snapshots are also distinct: they
support reviewing previous rendered runs, while server-side Strands session
files supply context to future model calls. Clearing UI history rotates the ID
but does not delete the old server-side session directory.

Session ownership is not bound to `user_id` by this implementation; the
session ID itself selects the stored conversation. Deployments beyond a trusted
local environment should use authentication, unpredictable IDs, and an
ownership/authorization check before restoring a client-supplied session.

## AG-UI protocol endpoint

AG-UI (Agent–User Interaction Protocol) is a standard event contract between
agent backends and user-facing applications. MCP standardizes agent-to-tool
communication; AG-UI standardizes agent-to-frontend communication. It lets an
AG-UI-compatible client render lifecycle, reasoning, text, tool calls, errors,
and custom events without learning this service's private event format.

This repository keeps `/invocations` unchanged and adds `POST /agui` as a
second wire format over the same agent stream. The current Streamlit UI still
uses `/invocations`; `/agui` is for clients such as CopilotKit or another AG-UI
HTTP client.

The endpoint accepts a `RunAgentInput` JSON object. `threadId` becomes the
Strands `session_id`, so runs in the same thread share conversation memory;
`runId` identifies one execution inside that thread. The adapter extracts the
last user message as the prompt. At present, other supplied messages are
validated as part of the input but are not individually imported into Strands
history; server-side history comes from `threadId`.

`agui.py` wraps the internal SSE stream and translates events:

| Internal event | AG-UI events |
|---|---|
| `reasoning` | `THINKING_START`, thinking content, thinking end |
| `text` | `TEXT_MESSAGE_START`, content deltas, message end |
| `tool_start` | `TOOL_CALL_START`, `TOOL_CALL_ARGS` |
| `tool_end` | `TOOL_CALL_END`, `TOOL_CALL_RESULT` |
| `error` | `RUN_ERROR` |
| `metrics` | `CUSTOM` named `metrics` |

A normal stream begins with `RUN_STARTED` and ends with `RUN_FINISHED`. A fatal
internal error closes any open text/thinking block and ends with `RUN_ERROR`
instead.

To expose the Compose-internal agent endpoint to the host, add
`compose/docker-compose.override.yml`:

```yaml
services:
  agent:
    ports:
      - "127.0.0.1:18080:8080"
```

Then start the stack and pull the Ollama model once:

```bash
cd opensource/compose
docker compose up -d --build
docker compose exec ollama ollama pull qwen3:1.7b
```

Call the endpoint with an SSE-capable client such as `curl -N`:

```bash
curl -N -X POST http://127.0.0.1:18080/agui \
  -H 'Content-Type: application/json' \
  -d '{
    "threadId":"thread-1",
    "runId":"run-1",
    "state":{},
    "messages":[{"id":"msg-1","role":"user","content":"What is the weather in Antibes?"}],
    "tools":[],
    "context":[],
    "forwardedProps":{}
  }'
```

When agent `API_KEY` authentication is enabled, add either `X-API-Key` or an
`Authorization: Bearer ...` header. Reuse `threadId` and change `runId` for a
follow-up in the same conversation. Use a new `threadId` for a new memory.

The Python dependency is pinned as `ag-ui-protocol==0.1.19`. This adapter
currently implements SSE output and a subset centered on lifecycle, text,
thinking, tool, custom, and metrics events. It does not yet consume
client-provided tools, synchronize `state`, use `context` or `forwardedProps`,
or implement interactive interrupts. Those fields are accepted for protocol
shape compatibility, not full feature support.

## Verification

The unit tests cover skill parsing and matching, prompt composition, disabled
skills, code-interpreter encoding and execution, timeout termination, and
disabled execution. They live in:

- `tests/unit/test_opensource_skills.py`
- `tests/unit/test_opensource_web_search.py`
- `tests/unit/test_opensource_map_tools.py`
- `tests/unit/test_opensource_code_interpreter.py`
