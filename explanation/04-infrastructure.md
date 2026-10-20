# 04 — `infrastructure/` — Provisioning (Infrastructure as Code)

Everything the agents need in AWS — the Gateway, the weather Lambda, IAM roles, the
Harness, the ECR repo, the Runtime — is **described in code** and created by
**Pulumi**. New to Pulumi, stacks, StackReference, IAM, ECR, Lambda? See
[00-concepts-for-beginners.md §7](00-concepts-for-beginners.md).

```text
pulumi/gateway/        shared AgentCore Gateway + Open-Meteo Lambda + IAM   (deploy FIRST)
pulumi/managed_agent/  AgentCore Harness (the config-defined agent)
pulumi/custom_agent/   ECR repo + AgentCore Runtime (the code-defined agent)
gateway/open_meteo_lambda.py  the Lambda source (packaged by the gateway stack)
gateway/deploy.py      legacy boto3 script (superseded; kept for reference)
scripts/               check_streamlit.py (CI health check) + Agent Registry tool
otel/                  optional local MLflow tracing setup
```

**Dependency direction (why order matters):** both agent stacks take a
`StackReference` to the gateway stack and read its exported `gateway_arn` /
`gateway_url`. So the gateway must exist first. The README's deploy section spells
out the order: gateway → managed → custom.

**The "import" theme.** These resources originally existed *before* Pulumi (created
by hand or by the legacy script). Every stack therefore supports **adopting** a
live resource via an `import_*` config id plus `ignore_changes`, so Pulumi takes
ownership without deleting-and-recreating. Once adopted, the import ids are removed
and normal drift management takes over.

---

## `pulumi/gateway/__main__.py` — the shared tool plane

[infrastructure/pulumi/gateway/__main__.py](../infrastructure/pulumi/gateway/__main__.py)
declares, in order:

1. **Gateway execution role** — assumable by `bedrock-agentcore.amazonaws.com`,
   with an inline policy allowing `bedrock-agentcore:*`, `lambda:InvokeFunction`,
   and `secretsmanager:GetSecretValue`.
2. **Lambda role + function** — `benchma-open-meteo-tools`, Python 3.13, 30s
   timeout. The code is packaged with `pulumi.FileAsset` pointing at
   [gateway/open_meteo_lambda.py](../infrastructure/gateway/open_meteo_lambda.py) —
   the "legacy" folder still holds the live Lambda source.
3. **`AgentcoreGateway`** — protocol `MCP`, authorizer **`AWS_IAM`**. That single
   choice is *why* the Custom agent needs the SigV4 MCP transport in
   [agents/custom/mcp_client/](../agents/custom/mcp_client/) — see
   [03-custom-agent.md](03-custom-agent.md).
4. **`AgentcoreGatewayTarget`** — binds the Lambda behind the Gateway with an
   inline tool schema declaring `getForecast` and `searchLocation`.

**The name-matching rule.** The tool names `getForecast` / `searchLocation` must be
identical in **four** places or the whole thing silently breaks: this schema, the
Lambda's dispatch on `bedrockAgentCoreToolName`
([open_meteo_lambda.py](../infrastructure/gateway/open_meteo_lambda.py)), both
system prompts, and the Prompt Management tool config in
[services/prompt_version.py](../apps/comparison/services/prompt_version.py).

**Exports:** `gateway_id/arn/url`, role ARN, lambda ARN, target id — consumed by the
agent stacks and, for `gateway_url`, ultimately by the running Custom agent.

---

## `gateway/open_meteo_lambda.py` — the actual tool implementation

[infrastructure/gateway/open_meteo_lambda.py](../infrastructure/gateway/open_meteo_lambda.py)
is a **stdlib-only** Lambda (no dependencies → trivially packageable). The Gateway
passes the called tool's name in
`context.client_context.custom["bedrockAgentCoreToolName"]`, prefixed with the
target name (`open-meteo-tools___getForecast`); `_normalize_tool_name` strips
everything before `___`. Then:

- `getForecast` → `api.open-meteo.com/v1/forecast` with the passthrough params.
- `searchLocation` → `geocoding-api.open-meteo.com/v1/search`; results are stripped
  of bloat fields (`postcodes` alone can be 200+ entries per city) via
  `_clean_location`. Keeping results small matters because large tool results would
  trigger the Custom agent's S3 context offloading and waste tokens for the Managed
  one.
- Unknown tool → HTTP 400; any exception → 500 with the error in the body.

This is the one place where the "tools" are *real code* doing a *real* HTTP call.
Everything else about tools (schemas, prompts, MCP) is plumbing around this ~60-line
function.

---

## `pulumi/managed_agent/__main__.py` — the whole Managed agent

[infrastructure/pulumi/managed_agent/__main__.py](../infrastructure/pulumi/managed_agent/__main__.py):

- **Reads `agents/managed/system-prompt.md` at deploy time** and passes it as the
  Harness `system_prompts`. Prompt changes ship through *this stack*, not app code.
- Declares the **`AgentcoreHarness`** with `model_id` (default
  `eu.anthropic.claude-sonnet-4-6`), `max_iterations=12` (the managed counterpart of
  the Custom agent's `MAX_TOOL_CALLS=12`), `timeout_seconds=900`, and two tools:
  the AWS-managed browser (`agentcore_browser`, ARN `…:aws:browser/aws.browser.v1`)
  and the shared Gateway (`agentcore_gateway`, pointing at the gateway stack's ARN).
- Adds an `invoke-gateway` **RolePolicy** on the (externally created)
  `execution_role_arn`, allowing `bedrock-agentcore:InvokeGateway` on the Gateway
  ARN **with the account segment wildcarded** (`:*:`). That wildcard exists because
  the Harness (eu-central-1) invokes a Gateway in a different region/account
  segment.
- Exports `harness_id` and a constructed `harness_arn` — the value the Streamlit app
  reads as `HARNESS_ARN`.

---

## `pulumi/custom_agent/__main__.py` — ECR + Runtime + prompt wiring

[infrastructure/pulumi/custom_agent/__main__.py](../infrastructure/pulumi/custom_agent/__main__.py):

- **ECR repository** `bedrock-agentcore-agent` (tags are MUTABLE, but CI tags by Git
  SHA and never overwrites → effectively immutable, traceable images).
- **`AgentcoreAgentRuntime`** pointing at `IMAGE_URI` (from CI, or the
  `custom_agent:image_uri` config; **required** — the stack refuses to guess). It
  sets the env contract that [agent.py](../agents/custom/agent.py) reads, via
  `_runtime_environment_variables()`:
  - Always: `AWS_REGION`, `BEDROCK_MODEL_ID`, `GATEWAY_URL`, `OFFLOAD_BUCKET`,
    `ENABLE_PROMPT_CACHING=true`, `ENABLE_THINKING=false`.
  - If a versioned prompt is provided: `BEDROCK_PROMPT_ID`,
    `BEDROCK_PROMPT_VERSION`, and `BEDROCK_PROMPT_SHA256` — these switch the agent
    from "read local file" to "load the pinned, hash-verified Prompt Management
    version" (see [03](03-custom-agent.md) and [06](06-services-and-finops.md)).
  - If MLflow is configured: `MLFLOW_OTLP_TRACES_ENDPOINT`, `MLFLOW_EXPERIMENT_ID`.
- **IAM:** the same `invoke-gateway` RolePolicy pattern as the Managed stack, **plus**
  — when a prompt id is set — a `read-bedrock-prompt` RolePolicy granting
  `bedrock:GetPrompt` on just that prompt's ARNs, so the agent can fetch its pinned
  prompt at startup. Least privilege: it's scoped to the specific prompt, not `*`.
- Note: `runtime_gateway_url` is a config value (the Runtime calls the Gateway
  through its regional URL), while the `StackReference` is still used to build the
  IAM policy's `gateway_arn`.
- Exports `ecr_repository_url`, `runtime_id`, `runtime_arn` — the last is the app's
  `RUNTIME_ARN`.

`Pulumi.example.yaml` in each stack documents the expected config keys;
`Pulumi.production.yaml` holds the real production values.

---

## `infrastructure/scripts/`

### `check_streamlit.py` — the CI smoke test

[infrastructure/scripts/check_streamlit.py](../infrastructure/scripts/check_streamlit.py)
launches `streamlit run apps/comparison/app.py` **headless** on port 18501, polls
`/_stcore/health` for up to 45s expecting `ok`, prints the app's output on failure,
and always tears the process down. It needs **no AWS** — it just proves the app
imports and starts cleanly (catching syntax errors, bad imports, config-time
crashes). Referenced by name in `ci.yml` and asserted by
[test_workflows.py](../tests/unit/test_workflows.py).

### `register_agent_registry_record.py` — publishing the tools to Agent Registry

[infrastructure/scripts/register_agent_registry_record.py](../infrastructure/scripts/register_agent_registry_record.py)
is an operational CLI (not part of CI) that registers the repo's Open-Meteo
capability in **AWS Agent Registry** — a catalog where other teams can *discover*
this tool set. It:

- Builds a descriptor (either a `CUSTOM` JSON blob or a structured `MCP` descriptor)
  describing the two tools, their input schemas, owner, `side_effect_class:
  read-only`, source paths, and the evaluation gate that guards them.
- Calls `create_registry_record`, then **polls** the record through its lifecycle
  (`DRAFT` → `PENDING_APPROVAL` → `APPROVED`) with `wait_for_record`, optionally
  submitting/approving, and finally verifies the record shows up in
  `search_registry_records`.
- Supports `--dry-run` (print the request, call nothing) and `--skip-*` flags.

It's a self-contained example of **provision-then-poll-until-consistent**, a common
shape when an AWS resource becomes usable only after asynchronous state changes.

---

## `infrastructure/otel/` — optional local tracing

[infrastructure/otel/](../infrastructure/otel/) is a **local test helper**, not used
in production. Its `docker-compose.yml` starts an **MLflow** server and an
**OpenTelemetry Collector**; `collector-config.local.yaml` receives spans over OTLP
and fans them out to MLflow only (no AWS credentials required). This lets you run
the Custom agent on your laptop and watch its traces in MLflow.

The README there draws the key distinction: **production does not route AWS
telemetry through this collector.** The deployed Runtime keeps AWS's default
ADOT→CloudWatch pipeline *and* adds an independent MLflow span processor inside
[agent.py](../agents/custom/agent.py). CloudWatch stays the complete source (it's
what the evaluation gate reads); MLflow is a bonus mirror. Do **not** set
`DISABLE_ADOT_OBSERVABILITY`.

---

## `gateway/deploy.py` — the legacy script (do not run)

[infrastructure/gateway/deploy.py](../infrastructure/gateway/deploy.py) is the
original ~470-line boto3 script that first created the Gateway, roles, Lambda, and
target by hand-coded API calls, writing its record to
`aws/gateway/deployed-state.json`. It is **superseded by Pulumi** and kept only as
documentation of the original setup and as the source of the resource
names/ids the Pulumi stacks imported. Don't run it for new changes.

---

### The synergy in one picture

```text
gateway stack ──exports gateway_arn / gateway_url──┐
   (Gateway + Lambda + IAM)                         │  StackReference
                                                    ▼
   managed_agent stack ── reads system-prompt.md ── Harness  ── uses ──┐
   custom_agent stack  ── ECR image + prompt env ── Runtime  ── uses ──┤
                                                                        ▼
                                            both call getForecast / searchLocation
                                              via the SAME Gateway → Lambda → Open-Meteo
```

Next: [05-ci-tests-config.md](05-ci-tests-config.md) — how all of this is validated
and shipped.
