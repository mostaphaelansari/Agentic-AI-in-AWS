# AWS Agentic Blueprint

Reference implementation for comparing two Amazon Bedrock AgentCore agents:

- A configuration-defined agent using AgentCore Harness.
- A code-defined Strands agent hosted on AgentCore Runtime.

The supported user interface is a Streamlit application that submits the same
prompt to both architectures and compares responses, latency, tool activity,
reasoning events, token usage, and prompt-cache behavior.

## Architecture

```text
                       +--------------------------+
User -> Streamlit UI ->| Managed agent adapter    |-> AgentCore Harness
                       +--------------------------+
                       | Custom agent adapter     |-> AgentCore Runtime
                       +--------------------------+
```

Both backends emit the same application event vocabulary:

```python
{"type": "reasoning", "content": "..."}
{"type": "text", "content": "..."}
{"type": "tool_start", "name": "...", "input": {...}}
{"type": "tool_end", "name": "...", "result": "..."}
{"type": "metrics", "usage": {...}}
{"type": "error", "content": "..."}
```

## Repository Layout

```text
apps/
  comparison/
    app.py                    # Supported Streamlit entrypoint
    config.py                 # UI/backend environment configuration
    backends/                 # Harness and Runtime API adapters
    components/               # Streamlit presentation
    formatting/               # Trace and metric formatting
    models/                   # Event and state definitions
    services/                 # Framework-independent state reduction
    assets/                   # CSS

agents/
  managed/
    system-prompt.md          # Harness-defined agent instructions
  custom/
    agent.py                  # Code-defined Strands agent
    mcp_client/               # SigV4 MCP Gateway transport
    prompts/                  # Custom-agent system prompt
    requirements.txt          # AgentCore Runtime build dependencies

infrastructure/
  gateway/
    deploy.py                 # Legacy boto3 provisioning script
    open_meteo_lambda.py      # Open-Meteo tool Lambda
  pulumi/
    gateway/                  # AgentCore Gateway, target, Lambda, IAM
    managed_agent/            # AgentCore Harness configuration
    custom_agent/             # AgentCore Runtime and image configuration

tests/
  manual/
    check_thinking_events.py  # Live AWS reasoning diagnostic

docs/                         # Architecture and demo documentation
requirements.txt              # Local development dependencies
uv.lock                       # Reproducible Python dependency lock
```

## Run The Comparison App

Install local dependencies and provide valid AWS credentials:

```bash
uv sync --dev
uv run streamlit run apps/comparison/app.py \
  --server.fileWatcherType=none
```

The explicit watcher setting is intentional. Streamlit's module-path watcher
probes lazily imported submodules, which is unsafe for some optional
dependencies. Passing the setting on the command line also works when the app is
started outside the repository root. The `llmrouter` extra is no longer needed to
run the app; it is only required by the archived learned-router experiment in
[`evaluations/llmrouter/`](evaluations/llmrouter/README.md).

The UI navigation has separate **Comparison** and **Custom agent** pages. The
Comparison page invokes Managed and Custom agents side by side; the Custom agent
page invokes only the repository-owned AgentCore Runtime. Each page keeps its own
conversation tree and active run.

A running comparison can be abandoned with **Stop comparison**. Stopping closes
both response streams immediately and calls `StopRuntimeSession` to terminate
the Custom agent's Runtime session server-side, so the identity running the UI
needs `bedrock-agentcore:StopRuntimeSession` on the runtime ARN in addition to
`bedrock-agentcore:InvokeAgentRuntime`. `InvokeHarness` has no equivalent stop
operation, so the Managed agent is cancelled client-side only and may keep
billing until its turn ends. Whatever streamed before the stop is kept in
history; token usage that never arrived is shown as **pending** rather than
zero, and the sidebar's *Refresh session cost* resolves it from the Custom
agent's logs once they land.

Completed runs form a conversation tree rather than a flat transcript. A new
prompt continues from the currently selected node, while **Edit and resubmit**
creates a sibling branch and keeps the original path intact. Each architecture
receives only its own bounded answers from the selected ancestor path; new
AgentCore session IDs are still used for every node so cost and CloudWatch
usage remain attributable to the correct run.

Each completed Managed and Custom answer also has a thumbs-up/down control.
Ratings are upserted by chat ledger, run, and architecture into the
`agent_feedback` SQLite table, together with the prompt, rendered answer,
session ID, and branch metadata. It uses `SESSION_COST_DB_PATH` by default;
set `FEEDBACK_DB_PATH` to keep ratings in a separate database file.

The comparison prompt accepts one UTF-8 CSV or mmCIF (`.cif`) file up to 1 MB.
The UI stages it as an encrypted, short-lived S3 object, gives both
architectures the same presigned read URL, and removes the object after the
run. Each agent uses its code interpreter for tabular or molecular-structure
analysis; compact chart results are rendered as interactive Vega-Lite
visualizations in the UI. Uploaded mmCIF structures are also previewed in an
interactive Mol* 3D viewer before analysis. Residue clicks return through a
bidirectional Streamlit component, are validated in Python, and can be included
with the next prompt without replacing the existing agent SSE transport. After
an mmCIF run, a dedicated dashboard places the Managed agent's analysis beside
its validated, agent-directed 3D view; if no usable filter is returned, the
dashboard keeps the analysis visible and falls back to the full structure.

The comparison sidebar also contains an **AWS read-only console** for inspecting
the exact AgentCore, CloudWatch, and CloudWatch Logs resources behind the cost
figures. It executes AWS CLI commands with the UI backend identity, never inside
the Harness interpreter, and accepts only an explicit read-operation allowlist.
Set `ENABLE_AWS_CLI_CONSOLE=false` to hide it. The AWS CLI must be installed in
the environment that runs Streamlit.

Model routing is shared by both AgentCore architectures. **Auto** uses LiteLLM's
`auto_router/complexity_router` in route-only mode: Ministral 3B classifies the
prompt through Amazon Bedrock, and LiteLLM maps the resulting
`SIMPLE`/`MEDIUM`/`COMPLEX`/`REASONING` tier to Claude Haiku 4.5 or Claude Sonnet
4.6. Only `SIMPLE` goes to Haiku by default. Every request in **Auto**, including
file analysis, extended thinking, and long contexts, is scored the same way.
**Economy** and **Quality** are explicit user overrides for evaluation. A routing
decision is sticky for the complete agent turn and tool loop, and the selected
model plus reason are recorded with run metrics. An unavailable router or a model
group outside the configured allowlist fails safely to Sonnet. A failed,
timed-out, or malformed classifier response falls back to LiteLLM's local
heuristic scorer.

When the custom Runtime's MLflow OTLP mirror is enabled, each root trace also
records the routing mode, router name/version, predicted candidate, outcome,
fallback status, selected model, and decision latency as
`agent.model.router.*` / `agent.model.selected_*` span attributes. Router version
defaults to the installed LiteLLM version; set `MODEL_ROUTER_VERSION` to pin a
policy version so decisions remain attributable across LiteLLM upgrades.

The tier policy and score boundaries are optional JSON overrides:
`COMPLEXITY_TIERS` accepts only the `cheap` and `strong` model groups (unset
keeps `SIMPLE=cheap` with every stronger tier on `strong`), and
`COMPLEXITY_TIER_BOUNDARIES` overrides LiteLLM's `simple_medium` 0.15,
`medium_complex` 0.35, and `complex_reasoning` 0.60 defaults. Configure the
Bedrock models with `BEDROCK_CHEAP_MODEL_ID`, `BEDROCK_STRONG_MODEL_ID`, and,
when the Harness needs a different regional ID, `HARNESS_CHEAP_MODEL_ID`.
The classifier defaults to `mistral.ministral-3-3b-instruct` in `eu-west-1`
with a 750 ms timeout; configure it with `MODEL_ROUTER_CLASSIFIER_MODEL_ID`,
`MODEL_ROUTER_CLASSIFIER_REGION`, and `MODEL_ROUTER_CLASSIFIER_TIMEOUT_MS`.
Its Ireland input/output rates default to $0.12 per million tokens through
`MODEL_ROUTER_CLASSIFIER_INPUT_PER_MTOK` and
`MODEL_ROUTER_CLASSIFIER_OUTPUT_PER_MTOK`. The app uses LiteLLM's bundled model
map by default rather than waiting on an optional network refresh.
`MODEL_ROUTING_MODE` sets the non-UI default. LiteLLM router internals are
version-sensitive, so re-check the scorer when upgrading LiteLLM.

A reproducible 7,212-query cold-start corpus for the Travel Agent, Protein
Analysis, and general routing boundaries is provided in
[`evaluations/llmrouter/`](evaluations/llmrouter/README.md). Its checked-in
scores are explicitly marked policy bootstrap utilities, not measured model
quality. The same tooling builds Longformer embeddings and converts repeated
Haiku/Sonnet evaluations into production-grade routing records. That corpus and
the `llmrouter` extra now back only the learned route-only experiment
([`apps/comparison/services/llmrouter_adapter.py`](apps/comparison/services/llmrouter_adapter.py));
they are no longer on the application's routing path.

Configuration can be supplied through environment variables. See
`.env.example` for the supported application settings.

If the application reports expired AWS credentials, refresh the SSO session
before retrying:

```bash
aws sso login
```

## Deploy The Custom Agent

The custom agent source is self-contained under `agents/custom/`. AgentCore
Runtime is the AWS hosting service; the folder contains the agent code deployed
to that service. Run AgentCore deployment commands from this directory so its
`requirements.txt`, prompt, and MCP client are included in the build context.

## Provision The Gateway

Pulumi is the deployment source of truth. The original boto3 script remains
available only while existing resources are being imported and compared:

```bash
python infrastructure/gateway/deploy.py
```

Do not run the script after Pulumi adoption is complete. See
[`docs/cicd.md`](docs/cicd.md) for the import and deployment procedure.

## Define And Deploy Agents In AWS

AWS resources are defined as code in the Pulumi projects under
`infrastructure/pulumi/`. The GitHub Actions deployment workflows call these
same Pulumi projects from CI/CD.

```text
infrastructure/pulumi/gateway/
  __main__.py               # Defines AgentCore Gateway, target, Lambda, IAM
  Pulumi.yaml               # Pulumi project definition
  Pulumi.production.yaml    # Production stack configuration

infrastructure/pulumi/managed_agent/
  __main__.py               # Defines the managed AgentCore Harness agent
  Pulumi.yaml
  Pulumi.production.yaml

infrastructure/pulumi/custom_agent/
  __main__.py               # Defines ECR and AgentCore Runtime
  Pulumi.yaml
  Pulumi.production.yaml
```

Deploy manually from a terminal with valid AWS credentials:

```bash
aws sts get-caller-identity
eval "$(aws configure export-credentials --format env)"
```

Deploy the Gateway first:

```bash
cd infrastructure/pulumi/gateway
pulumi stack select mostaphaelansari/agentic-blueprint-gateway/production
pulumi preview --diff
pulumi up
```

Deploy the managed Harness agent second:

```bash
cd ../managed_agent
pulumi stack select mostaphaelansari/agentic-blueprint-managed-agent/production
pulumi preview --diff
pulumi up
```

Deploy the custom Runtime agent third:

```bash
cd ../custom_agent
pulumi stack select mostaphaelansari/agentic-blueprint-custom-agent/production
pulumi preview --diff
pulumi up
```

The deployment order matters because both agents read Gateway outputs through
Pulumi stack references. Always review `pulumi preview --diff` before running
`pulumi up`.

From GitHub, run the same deployments through Actions after the deployment
repository secret and variables are configured:

```text
Actions -> Deploy gateway -> Run workflow
Actions -> Deploy managed agent -> Run workflow
Actions -> Deploy custom agent -> Run workflow
```

Use the `main` branch for production workflow runs.

## CI/CD

Pull requests run formatting, linting, unit tests, Python compilation,
dependency auditing, secret scanning, and a local Streamlit health check.
Pushes to `main` additionally run the AgentCore on-demand dataset evaluation
against the Custom Runtime currently deployed in `dev`. This is a deployed-service
smoke check; the custom deployment workflow is the gate that evaluates the newly
deployed Git-SHA-tagged candidate.

Production AWS changes are deliberately separated into three manually
triggered workflows:

- Gateway and Open-Meteo Lambda
- Managed AgentCore Harness agent
- Custom AgentCore Runtime agent

Each deployment workflow runs `pulumi preview` before `pulumi up`. The custom
agent workflow also builds and publishes an immutable ECR image tagged with the
Git commit SHA, passes that image URI to the Pulumi Runtime stack, and runs the
AgentCore evaluation gate after deployment.

Deployments are serialized with GitHub Actions concurrency groups. For example,
`deploy-managed-agent.yml` uses `dev-managed-agent` with
`cancel-in-progress: false`, so a second managed-agent deployment waits instead
of interrupting one already in progress.

The workflows use GitHub OIDC and the `dev` GitHub Actions environment name.
The required GitHub Actions secret is `PULUMI_ACCESS_TOKEN`; required variables
include the AWS deploy role ARNs, AWS regions, ECR repository name, and Pulumi
stack names. The live evaluation also requires `CUSTOM_AGENT_RUNTIME_ARN` and
AgentCore Evaluation plus CloudWatch query permissions on the custom-agent role.
Without that GitHub Actions configuration, pull-request validation can run, but
the main-branch evaluation and CD are not executable from GitHub Actions.

Evaluation reports include the Runtime version/container URI and hashes of the
dataset and evaluation configuration. The current two-case suite uses live
Open-Meteo data and initial thresholds; see [`evaluations/README.md`](evaluations/README.md)
for its guarantees and limits.

The Streamlit UI is never packaged, published, or hosted by these workflows. It
is started only inside CI for a temporary health check.

For setup, required GitHub variables, Pulumi imports, deployment order, and
rollback instructions, see [`docs/cicd.md`](docs/cicd.md).

## Live Diagnostics

```bash
python tests/manual/check_thinking_events.py
```

This diagnostic makes real AWS calls for Custom thinking on/off and Managed
thinking on. It is intentionally separate from offline unit tests.

## Token Semantics

```text
Prompt = fresh input + cache read + cache write
Total  = Prompt + output
```

The UI computes these values consistently for both architectures instead of
depending on provider-specific `totalTokens` behavior.
