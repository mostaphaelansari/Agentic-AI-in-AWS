# 02 — `apps/comparison/backends/` — The AWS Adapters

This is the layer that makes **two completely different AWS APIs look identical**
to the UI. The Managed agent and the Custom agent are called with different
functions, return different stream formats, and live in different regions — but by
the time events reach the UI, they're the same six event types from
[00-concepts §9](00-concepts-for-beginners.md).

No file here imports Streamlit. Each adapter is handed a `queue.Queue` and pushes
`(kind, payload)` items onto it: `("event", <one of the six>)`,
`("elapsed", seconds)`, `("done", None)`. The `RunController` in
[services/run_controller.py](../apps/comparison/services/run_controller.py) owns
those queues and the worker threads; the fragment in
[components/comparison.py](../apps/comparison/components/comparison.py) drains
them on each short rerun. Each adapter also hands its blocking stream handle to
the controller via `register_stream`, which is how **Stop** closes a read that
would otherwise sit there for the full 600-second timeout.

```text
dispatcher.py   picks harness vs runtime, and turns ANY error into an error event
harness.py      Managed agent: invoke_harness + translate its Converse stream
runtime.py      Custom agent: invoke_agent_runtime + pass its SSE straight through
common.py       boto3 client factory, credential preflight, SSE parser, date prefix
errors.py       classify auth failures + produce user-safe messages
```

---

## `dispatcher.py` — routing + the safety net

[backends/dispatcher.py](../apps/comparison/backends/dispatcher.py) is tiny but
does one crucial thing:

```python
def dispatch(config, session_id, user_msg, event_queue, options=None,
             *, controller=None, arch_name=None):
    try:
        if config["service"] == "harness":
            invoke_harness(..., controller=controller, arch_name=arch_name)
        else:
            invoke_runtime(..., controller=controller, arch_name=arch_name)
    except Exception as exc:
        # A cancelled run closed the stream on purpose: end the arch quietly
        # instead of surfacing the resulting read error to the user.
        ...
        event_queue.put_nowait(("event", {"type": "error", "content": public_error_message(exc)}))
        event_queue.put_nowait(("elapsed", 0))
        event_queue.put_nowait(("done", None))
```

It switches on `config["service"]` (the field from `ARCHITECTURES` in
[config.py](../apps/comparison/config.py)) and — critically — wraps **everything**
in a try/except that converts *any* exception into an `error` event followed by
`done`. This guarantees the UI poll loop always terminates: a backend thread can
never die silently and leave its column stuck on "Waiting". The raw exception is
logged; the user only sees a safe message.

---

## `harness.py` — the Managed adapter (the bigger, harder one)

[backends/harness.py](../apps/comparison/backends/harness.py) has to **translate**
AWS's native stream into our event vocabulary. Two parts: setting up the call, and
normalizing the stream.

### Invocation

`invoke_harness` calls `bedrock-agentcore`'s `invoke_harness` operation with the
harness ARN, the shared `runtimeSessionId`, `actorId` (from `ACTOR_ID`), and the
user message prefixed by `with_current_date(...)`.

### Extended-thinking plumbing (the genuinely tricky bit)

Turning on reasoning requires an `additionalParams: {thinking: {...}}` field inside
`model.bedrockModelConfig`. The catch: **whether the installed botocore version
even knows that field exists** varies. So there are two code paths:

1. **Modern botocore** — `_supports_additional_params(client)` introspects the
   botocore *service model shape* for the `InvokeHarness` operation. If the field
   is supported, it's passed the normal way via `_invocation_overrides`.
2. **Older botocore** — `_register_thinking_injector` registers a botocore event
   hook on `before-sign.bedrock-agentcore.InvokeHarness`
   (`_inject_thinking_body`). This hook rewrites the raw JSON request body **after
   serialization but before SigV4 signing**, sneaking the field past client-side
   validation. It is unregistered in a `finally` block because boto3 clients can be
   reused.

`_validate_thinking_config` enforces `budget ≥ 1024` and `max_tokens > budget` —
the **exact same constraints** the Custom agent enforces in
[agent.py](../agents/custom/agent.py) `_bedrock_model_kwargs`. Keep them in sync;
they exist so the reasoning budget can't swallow the whole output allowance.

### Stream normalization (Converse stream → six events)

The response is a **Converse-style event stream** (AWS's structured streaming
format). The loop walks each `stream_event` and tracks tool-call state across many
small deltas:

- `contentBlockDelta.reasoningContent` → `reasoning` event
  (`_reasoning_text` also handles `redactedContent`, which becomes
  `"[redacted reasoning]"`).
- `contentBlockDelta.text` and `textOutput` → `text` events, tagged
  `source: "managed"`.
- A tool call arrives in pieces: `contentBlockStart.toolUse` opens it (recording
  `toolUseId → name`), its arguments stream in as string fragments across later
  `toolUse` deltas, and `contentBlockStop` closes it and emits a `tool_start` with
  a **summarized** input. The matching `toolResult` block (correlated by
  `toolUseId`) emits a `tool_end` with a summarized result.
- `metadata.usage` is **accumulated across every model turn** into `token_totals`
  (input, output, cache read, cache write). At stream end, `totalTokens` is summed
  and a single `metrics` event is emitted.

### Summarization helpers

`_parse_nested_json` unwraps up to three levels of JSON-encoded strings — the
Harness often double- or triple-encodes tool payloads. `_summarize_tool_input` and
`_summarize_tool_result` use [formatting/browser.py](../apps/comparison/formatting/browser.py)
for browser tools and a 300-char JSON dump otherwise. These summaries travel on the
event as a `summary` field so the reducer doesn't have to recompute them.

---

## `runtime.py` — the Custom adapter (the small, easy one)

[backends/runtime.py](../apps/comparison/backends/runtime.py) is short **because
the Custom agent already speaks our event contract** (it's the producer — see
[03-custom-agent.md](03-custom-agent.md)). It:

1. Sends `{"prompt", "enable_caching", "enable_thinking"}` as the JSON payload to
   `invoke_agent_runtime`. **These keys are exactly what
   [agent.py](../agents/custom/agent.py) `invoke()` reads** — that's the contract
   between the app and the container.
2. If the response is SSE (`contentType` contains `event-stream`), every `data:`
   line is already a normalized event, so it goes straight onto the queue via
   `parse_sse_events`.
3. Fallbacks: a non-streaming JSON *list* of events is forwarded item by item; a
   plain dict is wrapped into one `text` event.

Compare the two adapters side by side and you can *see* the design payoff: the
Custom agent moved the translation work *into itself*, so its adapter is trivial;
the Managed agent is a black box, so its adapter carries all the translation.

---

## `common.py` — shared plumbing

[backends/common.py](../apps/comparison/backends/common.py):

- `with_current_date(msg)` → prepends `Current date: YYYY-MM-DD\n`. **Both** system
  prompts explicitly say this line is metadata, so the model uses it to resolve
  "today"/"this weekend" but doesn't treat it as part of the question.
- `get_client(region)` → a `bedrock-agentcore` boto3 client tuned for agents: a
  **600-second read timeout** (agent runs are long), TCP keepalive, and 2 retries.
- `ensure_aws_credentials()` → resolves and *freezes* credentials once, raising
  `AWSAuthenticationError` on failure. Called by `run_comparison` **before** any
  threads spawn, so an expired session fails fast and once.
- `parse_sse_events(body)` → a minimal SSE reader: for each non-empty `data:` line
  it `json.loads`es the payload and yields the dict, warning-and-skipping anything
  malformed.

---

## `errors.py` — the security/UX boundary

[backends/errors.py](../apps/comparison/backends/errors.py) exists because **raw
AWS exceptions can leak account ids and ARNs** into the UI, and because a stack
trace isn't actionable for a user whose SSO token just expired. So the UI only ever
shows one of two messages:

- `is_authentication_error(exc)` → `True` for botocore credential exception types
  *and* for message substrings like `"token has expired"`, `"unable to locate
  credentials"` (some auth failures arrive as generic exceptions).
- `public_error_message(exc)` → auth failures become the actionable "run
  `aws sso login`, then retry" message (`AWS_AUTH_MESSAGE`); everything else becomes
  a generic "check the application logs." The real exception is still logged by the
  dispatcher.

Covered by [tests/unit/test_backend_errors.py](../tests/unit/test_backend_errors.py).

---

### The synergy in one sentence

`dispatcher` guarantees the loop always ends, `common` centralizes the boto3/auth
details, `errors` keeps failures safe and friendly, and the two `invoke_*`
functions converge on the **same six events** — so
[services/event_reducer.py](../apps/comparison/services/event_reducer.py) can update
the UI without ever knowing which agent it's watching.

Next: [03-custom-agent.md](03-custom-agent.md) — the code-defined agent that *emits*
those events.
