# mcp-tools on AgentCore Runtime

Hosts [`opensource/mcp-tools`](../../../opensource/mcp-tools) on **AgentCore
Runtime** with `server_protocol=MCP` so the AgentCore Gateway can attach it as
an MCP-server target, and so the open-source agent can use the endpoint
directly as its full MCP tool server.

The Runtime serves the **full tool set by default**, including the weather
tools (`ENABLE_WEATHER_TOOLS=true`). The Gateway prefixes tool names per
target (`mcp-tools___getForecast` vs `open-meteo-tools___getForecast`), so
they coexist with the Open-Meteo Lambda target. Set
`enable_weather_tools: "false"` only if you explicitly want a
web-and-map-only Runtime — and pass `EXPECT_WEATHER_TOOLS=false` to the smoke
test in that case.

## Prerequisites

- AWS credentials that can manage AgentCore Runtime, ECR, and IAM in the stack
  region (usually `eu-central-1`, same as the Gateway).
- Pulumi stack created from [`Pulumi.example.yaml`](Pulumi.example.yaml).
- Docker Buildx with QEMU for `linux/arm64` (required by AgentCore MCP Runtime).
- The deploy role (CI OIDC role or your local principal) additionally needs
  **data-plane invoke permission** for the post-deploy smoke test:

```json
{
  "Effect": "Allow",
  "Action": "bedrock-agentcore:InvokeAgentRuntime",
  "Resource": "arn:aws:bedrock-agentcore:REGION:ACCOUNT_ID:runtime/opensource_mcp_tools-*"
}
```

Without it, `pulumi up` succeeds but the smoke-test step fails with an
authorization error.

Repository variables used by [`.github/workflows/deploy-mcp-tools.yml`](../../../.github/workflows/deploy-mcp-tools.yml):

| Variable | Purpose |
|---|---|
| `PULUMI_MCP_TOOLS_STACK` | Pulumi stack name |
| `MCP_TOOLS_REGION` / `GATEWAY_REGION` | AWS region |
| `AWS_MCP_TOOLS_DEPLOY_ROLE_ARN` / `AWS_GATEWAY_DEPLOY_ROLE_ARN` | OIDC deploy role |
| `MCP_TOOLS_ECR_REPOSITORY` | Optional override (default `bedrock-agentcore-mcp-tools`) |

## Clean-checkout bootstrap (ECR first)

The Runtime resource needs an image URI, but ECR is created by this stack. Break
the cycle with an explicit bootstrap:

```bash
cd infrastructure/pulumi/mcp_tools
pulumi stack init <org>/agentic-blueprint-mcp-tools/<stack>   # once
pulumi config set aws:region eu-central-1

# 1) Create ECR (+ execution role). No Runtime yet.
BOOTSTRAP_ECR_ONLY=true pulumi up

ECR_URL="$(pulumi stack output ecr_repository_url)"
REPO="$(pulumi stack output ecr_repository_name)"
REGION="$(pulumi config get aws:region)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
TAG="$(git rev-parse HEAD)"

# 2) Build and push linux/arm64, tagged with the commit SHA (immutable tags).
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
docker buildx build --platform linux/arm64 \
  -t "$ECR_URL:$TAG" \
  --push \
  ../../../opensource/mcp-tools

# 3) Refuse amd64-only images before deploy.
docker buildx imagetools inspect "$ECR_URL:$TAG" | grep -Eiq 'linux/arm64|architecture:\s*arm64'

# 4) Create / update the Runtime.
IMAGE_URI="$ECR_URL:$TAG" pulumi preview
IMAGE_URI="$ECR_URL:$TAG" pulumi up
```

The GitHub Actions workflow runs the same sequence automatically.

## Authentication and invocation

Inbound auth is **IAM SigV4** (`service=bedrock-agentcore`) against the exported
`mcp_endpoint`. Example smoke test (also used in CI after deploy):

```bash
export MCP_ENDPOINT="$(pulumi stack output mcp_endpoint)"
export RUNTIME_ARN="$(pulumi stack output runtime_arn)"
export IMAGE_URI="$(pulumi stack output image_uri)"
export EXPECTED_IMAGE_TAG="$(git rev-parse HEAD)"
export AWS_REGION="$(pulumi config get aws:region)"
uv run python ../../../infrastructure/scripts/smoke_mcp_tools.py
```

The smoke test:

1. Initializes an MCP session over SigV4.
2. Asserts `webSearch`, `searchMapPlaces`, and `findNearbyPlaces` are listed.
3. Asserts `searchLocation` / `getForecast` are listed (default
   `EXPECT_WEATHER_TOOLS=true`; set it to `false` for a web-and-map-only
   Runtime).
4. Calls `searchMapPlaces` and `webSearch` (envelope check only for search).
5. Runs a deployed weather call end to end: `searchLocation("Lisbon")` then
   `getForecast` on the returned coordinates, asserting `current_weather` is
   present.

## Consuming `mcp_endpoint` from the Gateway stack

After a successful Runtime deploy:

```bash
# In infrastructure/pulumi/gateway
pulumi config set mcp_tools_stack <org>/agentic-blueprint-mcp-tools/<stack>
# or:
pulumi config set mcp_tools_endpoint "$(pulumi stack output mcp_endpoint -s <mcp-tools-stack>)"
pulumi up
```

Harness and custom Runtime agents pick up the new tools through the shared
Gateway (`allowed_tools=["*"]` / `list_tools_sync()`).

## Using the endpoint directly from the open-source agent

The open-source agent (`opensource/agents/custom/service.py`) can point its
`MCP_URL` straight at this Runtime:

```bash
export MCP_URL="$(pulumi stack output mcp_endpoint)"
```

The agent detects the `bedrock-agentcore.<region>.amazonaws.com` host and
switches to a SigV4-signed transport automatically (override with
`MCP_URL_AUTH=sigv4|none|auto`). The process needs AWS credentials with
`bedrock-agentcore:InvokeAgentRuntime` on the runtime ARN.

## Rollback to an earlier image

Tags are **immutable** and named by git SHA. Redeploy a previous commit’s image:

```bash
PREVIOUS_SHA=<known-good-sha>
IMAGE_URI="$ECR_URL:$PREVIOUS_SHA" pulumi up
```

If that SHA was never pushed, check out the commit and rebuild/push first (Buildx
`linux/arm64`), then redeploy.

## ECR scanning

`scan_on_push` is **enabled**, and the deploy workflow **enforces** the policy:
after pushing, it waits for `aws ecr wait image-scan-complete` and fails the
run if any **CRITICAL or HIGH** findings are reported
(`aws ecr describe-image-scan-findings`). Fix or rebase the base image and
re-run; do not deploy around the gate. For manual deploys, run the same two
commands before `pulumi up`.

## External provider limitations (deployed Runtime)

| Dependency | Notes |
|---|---|
| DuckDuckGo / DDGS (`webSearch`) | No production SLA; rate limits and empty results are possible. |
| Nominatim / Overpass (map tools) | Public instances are shared community infrastructure; set `OSM_CONTACT_EMAIL` and prefer self-hosted/provider URLs for material traffic. |
| Open-Meteo (weather tools) | Free public API without an SLA; enabled by default on this Runtime and also available via the Gateway Lambda target (name-prefixed per target). |

## Exports

| Export | Purpose |
|---|---|
| `ecr_repository_url` / `ecr_repository_name` | Image repository |
| `mcp_endpoint` | Streamable-HTTP MCP invoke URL (full deploy only) |
| `runtime_arn` / `runtime_id` | AgentCore Runtime identity (full deploy only) |
| `image_uri` | Active container URI (full deploy only) |
| `execution_role_arn` | Created when `execution_role_arn` config is omitted |
| `bootstrap_ecr_only` | Whether the last update was an ECR-only bootstrap |
