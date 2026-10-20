# 06 — Services: Measured Cost, Prompt Versioning, and Security

The `services/` layer holds the app's **pure logic** — no Streamlit, no drawing.
[services/event_reducer.py](../apps/comparison/services/event_reducer.py) is
covered in [01-streamlit-app.md](01-streamlit-app.md); this doc covers the rest:
the cost model, the measured-compute lookup, the CloudWatch helper, the security
guard, and the prompt-versioning pipeline.

The unifying principle for the cost pieces is the FinOps rule from
[00-concepts §5](00-concepts-for-beginners.md): **measure, don't guess.** Every
dollar figure = a *measured* quantity × a *published* rate.

---

## `services/cost.py` — turning measured usage into dollars

[services/cost.py](../apps/comparison/services/cost.py) takes the normalized token
buckets from
[formatting/metrics.py](../apps/comparison/formatting/metrics.py) `normalized_usage`
(`fresh_input`, `cache_read`, `cache_write`, `output`) and the rate lists from
[config.py](../apps/comparison/config.py), and computes:

- **`inference_cost(usage)`** — the token bill, itemized:
  ```
  input  = fresh_input  / 1e6 × $3.00
  cache_read  = cache_read  / 1e6 × $0.30   (0.1× input — cheap to re-read)
  cache_write = cache_write / 1e6 × $3.75   (1.25× input — small premium to store)
  output = output / 1e6 × $15.00
  total  = sum of the above
  ```
- **`uncached_inference_cost(usage)`** — the hypothetical bill if **every**
  input-side token had been charged at the full fresh-input rate (i.e., caching did
  nothing).
- **`cache_savings(usage)`** = `uncached − cached`. **Positive means caching saved
  money this run; negative means it cost extra.** This is the honest number: cache
  *reads* save (\$3.00→\$0.30) but cache *writes* carry a premium (\$3.75), so on a
  one-shot run with no reuse, caching can be a small net loss — and the app says so.
- **`gateway_operations(metrics)`** — counts billable Gateway MCP operations,
  **excluding browser actions** (those are billed as browser compute, not Gateway).
  It prefers per-tool `tool_names`, falls back sensibly when only counts are known.
- **`gateway_cost(operations)`** = `operations / 1000 × $0.005`.

All of this is unit-tested in [test_cost.py](../tests/unit/test_cost.py) with clean
million-token inputs so the arithmetic is obvious (1M input+read+write+output =
\$22.05 cached vs \$24.00 uncached).

**Where the numbers surface:** `render_run_cost` in
[components/comparison.py](../apps/comparison/components/comparison.py) builds the
per-run cost table from these functions;
[components/metrics.py](../apps/comparison/components/metrics.py) `token_block`
shows the headline token cost per column; and `inference_cost`'s `total_cost` powers
the "Token cost" metric in the comparison chart.

---

## `services/runtime_usage.py` — the *measured* compute cost (Custom agent only)

The Custom agent runs in a container, billed per **vCPU-hour** and **GB-hour**. You
cannot know those at the end of a run because AgentCore reports them
**asynchronously** into a CloudWatch **USAGE_LOGS** group minutes later. So this
service is a *deferred lookup*, triggered by the "Refresh runtime cost" button.

[services/runtime_usage.py](../apps/comparison/services/runtime_usage.py):

- **`fetch_runtime_usage_for_session(region, log_group, session_id, started_at,
  completed_at)`** — calls CloudWatch `filter_log_events` on the USAGE_LOGS group,
  filtering by the exact `session_id`, over a padded time window
  (`_window_ms` reaches ~5 min before start and ~15 min after completion, because
  the records land late). It paginates through all matching events.
- **`_usage_from_events(events, session_id)`** — parses each JSON log line, keeps
  only records whose `attributes["session.id"]` matches this run, and sums the
  vended metrics `agent.runtime.vcpu.hours.used` and
  `agent.runtime.memory.gb_hours.used`. If none are found yet, it returns
  `{"status": "pending"}` — the UI then shows `pending` and you click refresh
  later.
- **`runtime_usage_cost(vcpu_hours, memory_gb_hours)`** — multiplies by the
  `$/vCPU-hour` and `$/GB-hour` rates from `AGENTCORE_RUNTIME_PRICING`.

Three status constants (`RUNTIME_USAGE_PENDING/AVAILABLE/ERROR`) let
`render_run_cost` decide what to display: real numbers, `pending`, or an error
caption.

> This is the file behind the memory note *"AgentCore Runtime: no machine/GPU
> selection, measured-telemetry FinOps only."* There is no server-size dropdown and
> no assumed utilization — the vCPU/GB-hours come straight from AWS's own vended
> USAGE_LOGS.

---

## `services/log_events.py` — a standalone CloudWatch log CLI

[services/log_events.py](../apps/comparison/services/log_events.py) is a **debugging
tool**, importable *and* runnable as `python -m` from the command line. It is not
wired into the Streamlit UI; it's for a developer poking at raw logs.

- `get_log_events(...)` — thin wrapper over CloudWatch `get_log_events` for one log
  stream (times are Unix ms, matching the API).
- `latest_log_stream(region, log_group)` — finds the newest stream (for `--tail`).
- `runtime_consumption_from_events(events)` — the same vCPU/GB-hour summing as
  `runtime_usage.py`, but standalone with its own rate constants, for
  spot-checking a stream from the terminal.
- `main()` — an argparse CLI (`--region`, `--log-group`, `--log-stream`, `--tail`,
  time window, limit) that prints events and a runtime-usage summary.

Think of it as the "manual version" of `runtime_usage.py`: the UI path is automated
and session-scoped; this one is interactive and stream-scoped.

---

## `services/security.py` — blocking prompt-exfiltration before you spend

[services/security.py](../apps/comparison/services/security.py) is a small,
**client-side** guard. `prompt_security_message(prompt)` runs the prompt against a
few compiled regexes that catch attempts to make the agents leak their hidden
instructions:

- "system/developer/hidden/internal **prompt/instructions/policy/rules**"
- "reveal/show/print/dump… **prompt/instructions/policy/configuration**"
- "ignore… previous/all… **instructions/rules/prompt/policy**" (classic prompt
  injection)

If any match, [app.py](../apps/comparison/app.py) shows the block message and **never
calls AWS** — so a hostile prompt costs zero tokens. It's *defense in depth*: the
system prompts themselves also refuse (the CONFIDENTIALITY RULE), but this stops the
request before it's even sent. Tested in
[test_security.py](../tests/unit/test_security.py). It's deliberately simple — a
demo guard, not a complete jailbreak defense.

---

## `services/prompt_version.py` — the prompt-versioning pipeline

[services/prompt_version.py](../apps/comparison/services/prompt_version.py) (568
lines, the largest service) turns the Custom agent's local `system.md` into an
**immutable, hash-verified Bedrock Prompt Management version**. This is what the
`deploy-custom-agent.yml` workflow calls (see [05](05-ci-tests-config.md)), and it
closes the loop with the agent's `_load_prompt_management_system_prompt` in
[agent.py](../agents/custom/agent.py) (see [03](03-custom-agent.md)).

Why bother versioning a prompt in AWS at all? So that the exact prompt text a
deployed agent runs is **pinned, auditable, and hash-checked** — not "whatever was
in the file when someone built the image." The SHA-256 travels from CI → Pulumi env
→ the running container, which verifies it on startup and refuses to run on a
mismatch.

The pieces:

- **`PromptVersion` / `PromptRegistry`** — small local dataclasses to track prompt
  version metadata in memory (id, version, description, short content hash).
- **`TemplatePromptVersion`** — a versioned template with reproducible
  `template_hash` and `render_hash` (hash the rendered output too, so you can prove
  what a given set of variables produced).
- **`build_chat_prompt_variant(...)`** — constructs a Bedrock Prompt Management
  **CHAT** variant from the system-prompt text: the `system` block, a `user`
  message with a `{{user_input}}` variable, optional inference config, optional tool
  configuration, and — importantly — an optional **cache point**. The comments note
  a real AWS gotcha: `SystemContentBlock` and `Tool` are *tagged unions*, so a
  `cachePoint` must be its **own** block/list-item, not a field.
- **`build_open_meteo_tool_configuration(...)`** — the `getForecast` /
  `searchLocation` tool schemas *again* (the 4th place the tool names must match —
  see [04](04-infrastructure.md)), optionally followed by a cache point.
- **`build_inference_config(...)`** — assembles temperature / top-p / max-tokens /
  stop-sequences into the Prompt Management shape.
- **`create_or_update_prompt_draft_from_file(...)`** — reads the file, hashes it,
  builds the variant, and calls `create_prompt` or `update_prompt`. It then reads
  the prompt back from AWS and re-hashes it, returning `prompt_hash_match` so you
  can confirm AWS stored *exactly* what you sent.
- **`create_prompt_version(...)`** — freezes the draft into an **immutable
  version** (a snapshot that can't change later).
- **`version_prompt_file_in_aws(...)`** — the end-to-end helper CI uses: draft →
  version → return `prompt_id`, `version_id`, `version_arn`, and the SHA-256.
- **`main()`** — an argparse CLI exposing all of the above, including
  `--include-open-meteo-tools`, `--cache-system-prompt`, `--cache-tools`, and
  `--draft-only`.

**The full prompt lifecycle (a nice cross-file story):**

```text
agents/custom/prompts/system.md
        │  (deploy-custom-agent.yml runs prompt_version.py main())
        ▼
Bedrock Prompt Management  ── immutable version + SHA-256 ──┐
        │                                                    │  BEDROCK_PROMPT_ID/VERSION/SHA256
        ▼                                                    ▼  (passed as env by custom_agent Pulumi stack)
custom_agent stack sets env + read-prompt IAM policy  ──►  AgentCore Runtime container
                                                                     │  agent.py _load_prompt_management_system_prompt
                                                                     ▼  fetch by id+version, verify SHA-256, refuse on mismatch
                                                               the model runs the exact, audited prompt
```

---

### Why these live in `services/` and not in the UI

Every function here is **pure or boto3-only** — none import Streamlit. That's what
lets `test_cost.py`, `test_security.py`, and `test_agentcore_evaluation.py` exercise
real logic offline, and it's the same discipline that keeps the cost math honest:
the numbers are computed in one testable place and merely *displayed* by the
components layer.

Next: [07-evaluations.md](07-evaluations.md) — proving the deployed agent is
actually good, not just cheap.
