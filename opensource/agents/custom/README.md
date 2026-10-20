# Open-source custom agent

Port of `agents/custom/` off AWS: the same Strands weather agent, served by
FastAPI over SSE instead of AgentCore Runtime. LiteLLM Auto Router sends simple
requests to local Ollama and medium, complex, or reasoning requests to
Kimi/Moonshot. Emits the identical application event vocabulary, so the
comparison app can consume it with a thin HTTP adapter.

## Run locally

```bash
# from the repo root, with KIMI_API_KEY or MOONSHOT_API_KEY set
uv run uvicorn service:app --port 8080 --app-dir opensource/agents/custom
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `MODEL_PROVIDER` | `litellm` | `litellm`/`auto` for per-turn LiteLLM Auto Router selection, or `kimi`/`ollama` to pin one provider |
| `MODEL_ID` | provider default | Optional generic model override; prefer `OLLAMA_MODEL_ID` or `KIMI_MODEL_ID` for clarity |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL_ID` | `qwen3:1.7b` | Any tool-calling-capable Ollama model |
| `OLLAMA_WARMUP_ENABLED` | `false` | Load the configured Ollama model before readiness; opt in because it increases startup time and reserves model memory |
| `OLLAMA_KEEP_ALIVE` | `10m` | How long an Ollama model loaded by startup warm-up should remain resident |
| `KIMI_API_KEY` / `MOONSHOT_API_KEY` | *(empty)* | Kimi/Moonshot credential required by `litellm` and `kimi` modes; keep it in the ignored Compose `.env` |
| `KIMI_MODEL_ID` | `kimi-k2.6` | Kimi model used by the router or pinned Kimi mode |
| `KIMI_BASE_URL` | `https://api.moonshot.ai/v1` | Kimi OpenAI-compatible base URL |
| `KIMI_TEMPERATURE` | `1` | Sampling temperature for Kimi; `kimi-k2.6` rejects `0` |
| `KIMI_TIMEOUT` | `60` | Kimi API request timeout in seconds |
| `KIMI_MAX_TOKENS` | *(empty)* | Optional Kimi max output tokens |
| `LITELLM_ROUTER_MODEL_ID` | `opensource-auto-router` | LiteLLM model-group name exposed to Strands |
| `LITELLM_ROUTER_SESSION_TTL_SECONDS` | `600` | Per-turn affinity TTL; all tool cycles in one turn stay on the initially selected provider |
| `MCP_URL` | `http://localhost:8000/mcp` | Weather + DDGS + OpenStreetMap MCP server (`opensource/mcp-tools`); empty/unreachable = in-process weather fallback only |
| `BOOKING_MCP_URL` | `https://mcp.rapidapi.com` | RapidAPI MCP endpoint; active only when `RAPIDAPI_KEY` is set |
| `RAPIDAPI_HOST` | `booking-com15.p.rapidapi.com` | RapidAPI product host sent to the MCP endpoint |
| `RAPIDAPI_KEY` | *(empty)* | RapidAPI credential; keep only in the ignored Compose `.env`, never Git |
| `AERODATABOX_MCP_URL` | `https://mcp.rapidapi.com` | RapidAPI MCP endpoint for AeroDataBox flight/airport data |
| `AERODATABOX_RAPIDAPI_HOST` | `aerodatabox.p.rapidapi.com` | RapidAPI product host for AeroDataBox |
| `AERODATABOX_RAPIDAPI_KEY` / `FLIGHT_RAPIDAPI_KEY` | `RAPIDAPI_KEY` | Optional separate RapidAPI credential for AeroDataBox; empty reuses `RAPIDAPI_KEY` |
| `AIR_SCRAPER_MCP_URL` | `https://mcp.rapidapi.com` | RapidAPI MCP endpoint for Air Scraper flight shopping/search |
| `AIR_SCRAPER_RAPIDAPI_HOST` | `sky-scrapper.p.rapidapi.com` | RapidAPI product host for Air Scraper |
| `AIR_SCRAPER_RAPIDAPI_KEY` / `FLIGHT_RAPIDAPI_KEY` | `RAPIDAPI_KEY` | Optional separate RapidAPI credential for Air Scraper; empty reuses `RAPIDAPI_KEY` |
| `GEOAPIFY_MCP_URL` | `https://api.geoapify.com/v1/mcp` | Geoapify MCP endpoint for geocoding; the agent exposes `geocode_address` and `reverse_geocode_coordinates` |
| `GEOAPIFY_API_KEY` | *(empty)* | Geoapify credential; keep only in the ignored Compose `.env`, never Git |
| `OPEN_TERMINAL_URL` | *(empty)* | Open Terminal base URL. Empty disables the `code_interpreter` tool |
| `OPEN_TERMINAL_API_KEY` | *(empty)* | Bearer token used only between the agent and Open Terminal |
| `CODE_INTERPRETER_TIMEOUT` | `15` | Python execution deadline in seconds (clamped to 1–60); timed-out processes are killed |
| `CODE_INTERPRETER_MAX_CODE_CHARS` | `20000` | Maximum Python source size accepted by the tool |
| `CODE_INTERPRETER_MAX_OUTPUT_CHARS` | `12000` | Maximum serialized interpreter result returned to the model |
| `CODE_INTERPRETER_MAX_ATTACHMENT_BYTES` | `1048576` | Maximum decoded size of the single UTF-8 CSV or mmCIF file accepted by `/invocations` |
| `MAX_TOOL_CALLS` | `12` | Tool-loop runaway cap |
| `GUARDRAILS_ENABLED` | `true` | Enforce prompt-policy blocking, sensitive-data redaction, and code-interpreter controls |
| `GUARDRAIL_MAX_PROMPT_CHARS` | `12000` | Reject prompts larger than this before model or session access |
| `LOG_LEVEL` | `INFO` | Service log level |
| `SKILLS_DIR` | `<app>/skills` | Folder of `*/SKILL.md` skill packages (see below). Missing dir = instructions only |
| `API_KEY` | *(empty)* | Shared secret required on `/invocations` and `/agui` (`X-API-Key` or `Authorization: Bearer`); `/ping` stays open. Empty = auth off |
| `SESSIONS_DIR` | *(empty)* | Local folder for per-session conversation history (Strands `FileSessionManager` JSON files, no external service). Empty = stateless, every request starts fresh |
| `CONVERSATION_WINDOW` | `20` | Max messages replayed per turn (sliding window), so long chats do not drown the small model |
| `MLFLOW_TRACKING_URI` | *(empty)* | MLflow server for SDK traces tagged with `mlflow.trace.user` / `mlflow.trace.session`. Empty = off |
| `MLFLOW_EXPERIMENT_NAME` | `opensource-custom-agent` | MLflow experiment the SDK traces land in |
| `OPENSOURCE_AGENT_VERSION` | `v3` | Human-readable open-source agent version suffix used in MLflow app versioning |
| `MLFLOW_ACTIVE_MODEL_NAME` | `opensource-custom-agent-${OPENSOURCE_AGENT_VERSION}` | MLflow LoggedModel name representing the active GenAI app version; traces are linked to this model |
| `MLFLOW_LOG_MODEL_PARAMS` | `true` | Log public agent config, system prompt, prompt hash, skill manifest/hash, model settings, and tool/guardrail flags onto the active MLflow model |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(empty)* | OTLP collector for Strands span export. Empty = off |

## Invoke

```bash
curl -N -X POST http://localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "What is the weather in Casablanca?", "user_id": "me", "session_id": "chat-1"}'
```

Reusing the same `session_id` continues the conversation (when `SESSIONS_DIR`
is set) and groups the traces into one MLflow session; omitting it gets a
fresh generated id per request, so anonymous calls never share history.
`user_id`/`session_id` may also be sent as `X-User-ID` / `X-Session-ID`
headers.

When `MLFLOW_TRACKING_URI` is set, the service creates or selects the MLflow
LoggedModel named by `MLFLOW_ACTIVE_MODEL_NAME`, logs the public agent version
parameters, and links SDK traces from `/invocations` and `/agui` to that active
GenAI app version.

Each `/invocations` stream also emits its MLflow `trace_id`. The `POST
/feedback` endpoint uses the same API-key policy as `/invocations`; it accepts
that ID plus an `up` or `down` rating and records `user_satisfaction` as an
MLflow human `Feedback` assessment on the same trace. Sending the returned
`assessment_id` with a later vote updates the existing assessment instead of
creating a duplicate.

Streams SSE `data:` lines: `trace` for MLflow correlation, `tool_start` /
`tool_end` for each weather, web-search, map, or code-interpreter call, `text`
deltas for the answer, and a final `metrics` event with token usage, the actual
routed model ID, `model_router=litellm-auto`, and
`request_to_model_dispatch_ms`. That timing starts when the FastAPI endpoint
receives the request and stops when the first model or router invocation begins;
it intentionally excludes LiteLLM classification and provider time. Later model
calls in the same tool loop do not overwrite it. It is also logged, shown as
**Model dispatch** in the UI, and attached to the active MLflow span.

LiteLLM's heuristic classifier runs without an extra model call. `SIMPLE`
requests use Ollama; `MEDIUM`, `COMPLEX`, and `REASONING` use Kimi. The same
selection is pinned for the complete Strands model/tool loop, then a new user
turn is classified again. If the selected provider is unavailable, LiteLLM
tries the other provider once. LiteLLM import and router construction run in the
FastAPI lifespan before readiness, so the first user request does not pay that
one-time initialization cost.

The Compose stack runs Open Terminal in a separate, resource-limited container.
The model receives only a Python tool; it does not receive Open Terminal's
general shell or file APIs. This local single-container sandbox is intended for
the repository comparison, not hostile multi-tenant production execution.

Health check: `GET /ping`.

## Guardrails

Guardrails are deterministic and local, so they do not require AWS, Kimi, a
second model, or optional ML packages. Before an invocation reaches the model,
the service rejects prompt-instruction override, confidential-prompt
exfiltration, malware-execution requests, malformed control characters, and
oversized input. Email addresses, international phone numbers, payment-card
numbers, AWS access keys, bearer tokens, and `sk-...` API keys are replaced
with redaction labels.

The same redaction runs on tool arguments, tool results, and streamed model
events. The code interpreter additionally rejects network clients, subprocess
execution, dynamic `eval`/`exec`, destructive file operations, and reads from
sensitive system paths. Blocked `/invocations` requests emit an SSE `error`
with code `guardrail_blocked`; blocked `/agui` requests end with `RUN_ERROR`.
`GET /ping` reports `"guardrails": "enabled"` when enforcement is active.

## AG-UI endpoint

`POST /agui` exposes the same agent over the [AG-UI protocol](https://docs.ag-ui.com),
so AG-UI clients (e.g. CopilotKit) work without custom client code. It accepts
a `RunAgentInput` body (the last `user` message becomes the prompt; `threadId`
doubles as the conversation `session_id`) and streams AG-UI events:
`RUN_STARTED`, `THINKING_*` for reasoning deltas, `TEXT_MESSAGE_*` for the
answer, `TOOL_CALL_*` per tool call, `CUSTOM` events named `metrics`, and
`RUN_FINISHED` (or `RUN_ERROR`). Auth applies exactly as on `/invocations`;
the internal event contract is unchanged (translation lives in `agui.py`).

```bash
curl -N -X POST http://localhost:8080/agui \
  -H 'Content-Type: application/json' \
  -d '{"threadId": "chat-1", "runId": "run-1", "state": {}, "tools": [], "context": [], "forwardedProps": {},
       "messages": [{"id": "1", "role": "user", "content": "What is the weather in Casablanca?"}]}'
```

## Prompt = instructions + skills

`prompts/system.md` holds only the universal instructions (identity, strict
rules, answer style, tool inventory). Task-specific procedures live in
`skills/<name>/SKILL.md` — frontmatter (`name`, `description`, comma-separated
`keywords`) plus a markdown body. Per request the service appends the body of
every skill whose keywords appear in the prompt ("inject" mode — the server
decides; the small model never calls a meta-tool). Skill files are rescanned
on every request, so editing one takes effect immediately without a restart.
Universal rules must never move into a skill: an unloaded skill cannot
enforce anything.
