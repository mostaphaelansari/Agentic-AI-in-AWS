# 01 — `apps/comparison/` — The Streamlit Comparison App

This is the web app you actually click. It runs **on your laptop** (it is never
deployed by CI — that rule is enforced by
[tests/unit/test_workflows.py](../tests/unit/test_workflows.py)) and talks to AWS
using *your* local credentials (usually AWS SSO).

New to the vocabulary here (Streamlit, boto3, TypedDict, queue, thread, tokens,
session id)? Read [00-concepts-for-beginners.md](00-concepts-for-beginners.md)
first.

---

## The layered design (and why it matters)

The package is split into layers on purpose. The rule: **lower layers know
nothing about the layers above them.**

```text
app.py            entrypoint — wires the page together
config.py         reads .env, holds ARNs + price lists
components/       everything that draws on screen  (imports streamlit)
backends/         AWS adapters                      (imports boto3, no streamlit)  → 02-backends.md
services/         pure logic: state, cost, security (no streamlit, no drawing)
models/           the shapes of "state" and "event" (pure data)
formatting/       string/number tidying helpers      (pure)
assets/           styles.css
```

Why bother? The **pure** layers (`services/`, `models/`, `formatting/`) have no
Streamlit and no AWS, so they can be **unit-tested in isolation** (see
[test_cost.py](../tests/unit/test_cost.py),
[test_security.py](../tests/unit/test_security.py),
[test_history_state.py](../tests/unit/test_history_state.py)). If cost math lived
inside a Streamlit drawing function, you couldn't test it without a browser.

> **Import quirk to know up front.** The app is launched *from inside*
> `apps/comparison/` (`streamlit run apps/comparison/app.py`), so its imports are
> written as top-level (`from backends.common import …`, not
> `from apps.comparison.backends…`). That's why the unit tests do
> `sys.path.insert(0, ".../apps/comparison")` before importing.

---

## `app.py` — the entrypoint

[app.py](../apps/comparison/app.py) is short; `main()` runs top to bottom every
time Streamlit reruns the script (Streamlit re-executes the whole file on every
interaction — that's its model):

1. **Page setup + chrome.** `st.set_page_config(...)`, then `inject_styles()`
   (CSS), `render_header()` and `render_comparison_summary()` (the "How the two
   agents differ" table) from [components/layout.py](../apps/comparison/components/layout.py).
2. **Sidebar.** A `View` radio switches between two modes:
   - **Docs** → renders any markdown file from `docs/` inside the app
     ([components/docs.py](../apps/comparison/components/docs.py)).
   - **Compare** → a "New chat" button, and two **feature toggles**:
     - *Reasoning (extended thinking)* — default **off**
     - *Prompt caching* — default **on**
     plus the history selector.
3. **The prompt box + Run button.** Inside a bordered container.
4. **On Run:** it does a **security preflight** before spending any money:

```python
security_message = prompt_security_message(run_prompt)   # services/security.py
if security_message:
    st.warning(security_message)                         # blocked, no AWS call
else:
    options = {"enable_thinking": ..., "enable_caching": ...}
    states = run_comparison(run_prompt, options)         # the real work
    if states:
        add_history_entry(run_prompt, options, states)   # save to session history
```

**Correlations to notice:**
- `options` is *only* `{enable_thinking, enable_caching}`. These two booleans flow
  all the way to both backends and change how the model is configured.
- `prompt_security_message` (from
  [services/security.py](../apps/comparison/services/security.py)) blocks
  attempts to make the agents leak their hidden system prompt — a cheap,
  client-side guard **before** any token is spent. See
  [06-services-and-finops.md](06-services-and-finops.md).
- `current_prompt_key()` gives the text input a *new widget key* after each "New
  chat". Streamlit can't clear a widget in place, so re-keying it is how the box
  gets emptied.

---

## `config.py` — configuration + the price lists

[config.py](../apps/comparison/config.py) loads `.env` with `python-dotenv`, then
defines a handful of module-level constants. **Every value has an env-var
override**, so nothing is truly hard-coded.

- **`ARCHITECTURES`** — the two columns of the app. This dict *is* the app's list
  of agents. Each entry carries the fields the rest of the code branches on:
  ```python
  "Managed agent": {"id": "Managed", "service": "harness", "region": "eu-central-1", "arn": "…"}
  "Custom agent":  {"id": "Custom",  "service": "runtime",  "region": "eu-west-1", "arn": "…", "streaming": True}
  ```
  The `service` key (`"harness"` vs `"runtime"`) is what
  [backends/dispatcher.py](../apps/comparison/backends/dispatcher.py) switches on.
  The default ARNs point at the author's real deployed resources; override with
  `HARNESS_ARN` / `RUNTIME_ARN`.

- **`BEDROCK_TOKEN_PRICING`** — the dominant cost driver. Per-million-token list
  rates for Claude Sonnet: `$3` input, `$15` output, `$0.30` cache read (0.1×),
  `$3.75` cache write (1.25×). These are **list prices only** — the token
  *quantities* are measured per run.

- **`AGENTCORE_GATEWAY_PRICING`** — per-1,000-operation rates for MCP tool calls
  (`$0.005`) and Search (`$0.025`).

- **`RUNTIME_USAGE_LOG_GROUP` + `AGENTCORE_RUNTIME_PRICING`** — the CloudWatch log
  group where AgentCore writes the Custom agent's measured compute usage, and the
  `$/vCPU-hour` ($0.0895) and `$/GB-hour` ($0.00945) rates. Used by
  [services/runtime_usage.py](../apps/comparison/services/runtime_usage.py) *after*
  a run.

- **`ACTOR_ID`, `HARNESS_MODEL_ID`, `HARNESS_MAX_TOKENS`,
  `HARNESS_THINKING_BUDGET_TOKENS`** — used only by
  [backends/harness.py](../apps/comparison/backends/harness.py).

> **Important accuracy note (this replaced an older design).** Earlier versions of
> this app *estimated* Runtime cost with an invented "CPU-active fraction." That is
> **gone**. Cost is now **measured**: token counts from the model stream, tool
> counts from the event stream, and vCPU/GB-hours from AWS's own USAGE_LOGS. There
> are no assumed utilization knobs anywhere. If you see one mentioned in an old
> note, it's obsolete.

---

## `components/comparison.py` — the heart of the UI

[components/comparison.py](../apps/comparison/components/comparison.py) has two
jobs: **run** a live comparison, and **render** results (live, saved, and the
metric chart).

### Ordering: Managed always left, Custom always right

`AGENT_DISPLAY_ORDER = ("Managed agent", "Custom agent")` and
`_ordered_agent_items()` guarantee a stable left→right order regardless of dict
insertion order (Python dicts preserve insertion order, but this makes it
explicit and puts any unknown agents last). Locked down by
[test_comparison_order.py](../tests/unit/test_comparison_order.py).

### `run_comparison(prompt, options)` — the live engine

This is the trickiest function in the app because of a Streamlit constraint:
**only the main thread may touch the screen.** Here's the flow:

1. **Credential preflight.** `ensure_aws_credentials()`
   ([backends/common.py](../apps/comparison/backends/common.py)) resolves your AWS
   credentials **once**, up front. If your SSO session expired, you get **one**
   friendly error ("run `aws sso login`") instead of two ugly thread crashes.
2. **One mutable `state` dict per agent** via
   `new_comparison_state()` ([models/state.py](../apps/comparison/models/state.py)).
3. **One `queue.Queue` per agent**, and two columns of `st.empty()` placeholders
   (empty slots the code will fill and refill).
4. **Launch both agents on a `ThreadPoolExecutor`.** Each thread runs
   `dispatch(...)` and does nothing but drop items onto its queue. The backend
   threads **never draw**.
5. **The poll loop** (the whole point):
   ```python
   while not all(state["done"] for state in states.values()):
       for name, event_queue in queues.items():
           while True:
               try: item = event_queue.get_nowait()
               except queue.Empty: break
               apply_event(states[name], item)   # services/event_reducer.py
               changed = True
       if changed:
           # main thread re-renders the changed columns
       ...
       time.sleep(0.1)
   ```
   The main thread drains the mailboxes ~10×/second, updates state through the
   **reducer**, and redraws. There's a safety fallback: if both threads finish but
   never sent a `done`, the loop marks their states `"Stopped"` so the UI can't
   hang forever.

This is the classic "producer threads + consumer main-thread + queue" pattern from
[00-concepts §8](00-concepts-for-beginners.md). Draw it once in your head and the
rest is easy.

### `render_column(placeholders, state)` — drawing one agent

Given one `state` dict, it fills the five placeholders: a status pill + metric
chips + token block (HTML built in
[components/metrics.py](../apps/comparison/components/metrics.py)), any error box,
the answer container, the **Trace** expander, and the session-id block with a
CloudWatch deep link. It reads state; it never calls AWS.

### `render_run_cost(state, key_suffix)` — the measured cost table

This builds the per-run **Run cost** table. For **both** agents it shows the token
cost (broken into input / cache read / cache write / output), the "with cache vs
without cache" comparison, the **prompt-caching delta** (did caching save or cost
money this run?), and the Gateway MCP-operation cost. All of these come from
[services/cost.py](../apps/comparison/services/cost.py) applied to measured usage.

For the **Custom agent only** it adds a *Refresh runtime cost* button. Runtime
compute usage (vCPU-hours, GB-hours) is reported by AWS **asynchronously**, so it
isn't known when the run finishes. Clicking the button calls
`fetch_runtime_usage_for_session(...)`
([services/runtime_usage.py](../apps/comparison/services/runtime_usage.py)) to pull
that run's exact usage records from CloudWatch and turn them into dollars. Until
then the table shows `pending`. The Managed agent shows `not applicable` (AWS bills
its compute a different way, not exposed the same way).

### `render_metric_comparison(states)` — the bar chart

`METRIC_COMPARISONS` is a dict of `{label: {unit, value(state)}}` extractor
functions (Latency, Tools, Cycles, Input/Output/Total tokens, Token cost). A
selectbox picks one; the code builds an **Altair** bar chart plus a table
comparing that one metric across the two agents. It's used for live runs and for
each saved history entry — each needs a **unique `key_suffix`** because Streamlit
requires unique widget keys.

> Semantic quirk worth remembering: the two backends report tool activity slightly
> differently, so `tool_call_count()` prefers `browse_calls` then falls back to
> `tool_calls`, and `cycle_count()` prefers `cycles` then falls back to
> `tool_calls`. See [components/metrics.py](../apps/comparison/components/metrics.py).

---

## `components/metrics.py` — presentation + observability helpers

[components/metrics.py](../apps/comparison/components/metrics.py) is **pure
formatting** over a state dict (note: it no longer contains any cost *estimation* —
that moved to `services/`). It provides:

- `metric_value(n)` — `"1,234"` or `"0"`.
- `tool_call_count(state)` / `cycle_count(state)` — the fallback logic above.
- `cloudwatch_session_url(region, session_id)` — a deep link into **CloudWatch
  GenAI Observability** for that exact session.
- `cloudwatch_session_query(session_id)` — a ready-to-paste **Logs Insights**
  query that finds every log line mentioning the session id.
- `header_chips(state)` / `token_block(state)` / `session_block(state)` — the HTML
  snippets rendered by `render_column`. All user-controlled text is passed through
  `html.escape` (safety), and the CSS classes (`status-pill`, `token-block`,
  `session-id-block`, …) are defined in `assets/styles.css`.

---

## `models/` — the shapes of the data

### `models/state.py`

[models/state.py](../apps/comparison/models/state.py) defines the **one state-dict
shape** the whole UI reads and writes:

- `new_session_id()` → `streamlit_demo-<uuid>--<uuid>`. AgentCore wants long
  session ids; the same id is sent to AWS *and* shown in the UI for log
  correlation.
- `new_comparison_state(arch_name, config)` → the canonical dict with keys like
  `status`, `phase` (`pre_tool` / `in_tools` / `post_tool`), `answer`,
  `pending_text`, `trace`, `metrics`, `runtime_usage`, `elapsed`, `done`,
  `errors`. The `phase` field is central to the reducer's "is this text the answer
  or pre-tool narration?" decision (below).
- `snapshot_states(states)` → a deep-ish copy so a saved history entry can't be
  mutated by a later run.

### `models/events.py`

[models/events.py](../apps/comparison/models/events.py) is the **typed contract**
for the six event types from [00-concepts §9](00-concepts-for-beginners.md). It
uses **Pydantic** models with `extra="forbid"` (reject unknown fields) and a
**discriminated union** keyed on `type`, so `validate_agent_event({...})` will
raise if an event is malformed. `AgentEvent` is the looser `TypedDict` form used at
runtime; `QueueItem` is the `(kind, payload)` tuple placed on the queues, where
`kind` is `"event"`, `"elapsed"`, or `"done"`.

This file is one of the **three places the event contract is written down** (the
other two: the docstring atop [agents/custom/agent.py](../agents/custom/agent.py)
and the README). They must stay in sync.

---

## `services/event_reducer.py` — the pure state machine

[services/event_reducer.py](../apps/comparison/services/event_reducer.py) is the
**only writer** of comparison state. `apply_event(state, item)` takes the current
state and one queue item and mutates the state — no drawing, no AWS. This is what
makes the whole thing testable and backend-agnostic.

Key behaviors (and the subtle bits):

- `("elapsed", seconds)` → records wall-clock time.
- `("done", None)` → finalize. **Subtlety:** if the run ended while still in phase
  `pre_tool` with only `pending_text` (a plain answer with no tools), that text is
  promoted to `answer`.
- `text` events → this is the clever part. Text the model emits **before** any tool
  runs (`phase != "post_tool"`) is treated as *narration/commentary* and buffered
  in `pending_text`; text **after** a tool ran goes into the real `answer`. When a
  tool starts, `_append_tool_start` flushes the buffered narration into the
  **trace** panel, not the answer. This keeps "let me look that up…" chatter out of
  the final answer.
- `reasoning` events → appended to `trace`, status becomes "Thinking".
- `tool_start` / `tool_end` → build readable trace lines, preferring a
  backend-supplied `summary`, and using
  [formatting/browser.py](../apps/comparison/formatting/browser.py) to label
  browser actions and hide noisy page dumps.
- `metrics` events → merged into `state["metrics"]`.
- `error` events → appended to `state["errors"]`, status "Error".

---

## `formatting/` — pure tidying helpers

### `formatting/metrics.py`

[formatting/metrics.py](../apps/comparison/formatting/metrics.py):
`normalized_usage(usage)` maps the **raw Bedrock token keys** (`inputTokens`,
`cacheReadInputTokens`, `cacheWriteInputTokens`, `outputTokens`) into the buckets
the whole UI and cost model use: `fresh_input`, `cache_read`, `cache_write`,
`prompt_total` (= the three input buckets), `output`, `total`. **Both backends emit
these same raw keys** — that single fact is what makes every downstream cost/metric
calculation identical for both agents.

### `formatting/browser.py`

[formatting/browser.py](../apps/comparison/formatting/browser.py) is browser-trace
cosmetics, shared by the Managed adapter (which summarizes at the source) and the
reducer (which summarizes events that arrive raw):

- `is_browser_tool(name)` — substring check for `"browser"`.
- `format_browser_action(input)` — turns the browser tool's `browser_input`
  (often a *Python-literal string*, hence `ast.literal_eval`) into short labels
  like `navigate <url>`, `click \`sel\``, `get_text \`sel\``.
- `format_browser_result(str)` — extracts the first text/json item from a
  literal-eval'd result list, capped at 300 chars.
- `should_hide_trace_text(text, tool)` — drops noise: page boilerplate ("Main
  menu", "Jump to content"), **offloaded-content references** (`s3://`,
  `retrieve_offloaded_content` — produced by the Custom agent's ContextOffloader,
  see [03-custom-agent.md](03-custom-agent.md)), bot-detection text, and any
  browser text longer than 120 chars.

---

## `components/history.py` — session-scoped history

[components/history.py](../apps/comparison/components/history.py) keeps up to 12
past runs in `st.session_state` (newest first). Each entry stores the prompt, the
options, and a `snapshot_states()` copy so you can **re-view a past run without
calling AWS again**. `start_new_chat()` bumps the counter that re-keys the prompt
input; "Clear history" empties the list. Behavior pinned by
[test_history_state.py](../tests/unit/test_history_state.py).

## `components/docs.py` — the in-app documentation viewer

[components/docs.py](../apps/comparison/components/docs.py) renders every
`docs/**/*.md` file (plus the root README) inside the app's **Docs** view. Streamlit's
native markdown can't draw **Mermaid** diagrams, so this splits the markdown on
```` ```mermaid ```` fences and renders those blocks with the Mermaid ESM bundle in
an `st.components.html` iframe.

## `components/layout.py`, `components/styles.py`, `assets/styles.css`

The header hero, the "How the two agents differ" comparison table (built from
`ARCHITECTURES` and `BEDROCK_TOKEN_PRICING` so it always matches the live config),
and CSS injection. Every custom HTML class used by `metrics.py`/`comparison.py`
lives in `assets/styles.css`.

---

### Cross-file summary for this package

```text
app.py ──calls──> services/security.prompt_security_message   (block leaks pre-spend)
       ──calls──> components/comparison.run_comparison         (the live engine)
                     ├─ backends/common.ensure_aws_credentials (preflight)
                     ├─ backends/dispatcher.dispatch  (× threads)  → 02-backends.md
                     ├─ services/event_reducer.apply_event    (state machine)
                     ├─ services/cost.*                        (token/gateway $)
                     ├─ services/runtime_usage.*               (measured compute $)
                     └─ components/metrics.* + formatting/*     (drawing)
       ──calls──> components/history.add_history_entry         (save snapshot)
```

Next: [02-backends.md](02-backends.md) — how those two `dispatch` branches actually
talk to AWS.
