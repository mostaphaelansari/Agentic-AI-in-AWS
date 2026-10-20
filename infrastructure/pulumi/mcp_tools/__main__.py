"""Pulumi program: host opensource/mcp-tools on AgentCore Runtime (MCP protocol).

Bootstrap path
--------------
AgentCore Runtime needs an image URI, but the ECR repository is created by this
stack. To break that cycle:

1. `BOOTSTRAP_ECR_ONLY=true pulumi up` creates the ECR repository (and optional
   execution role) without a Runtime.
2. Build/push a `linux/arm64` image tagged with the git SHA.
3. `IMAGE_URI=... pulumi up` creates/updates the Runtime.

The deploy-mcp-tools GitHub Actions workflow automates that sequence.
"""

from __future__ import annotations

import os

import pulumi
import pulumi_aws as aws
from endpoint import runtime_mcp_endpoint

config = pulumi.Config()
region = aws.get_region().region
account_id = aws.get_caller_identity().account_id
runtime_name = config.get("runtime_name") or "opensource_mcp_tools"
ecr_repository_name = config.get("ecr_repository_name") or "bedrock-agentcore-mcp-tools"
bootstrap_ecr_only = os.environ.get("BOOTSTRAP_ECR_ONLY", "").lower() in {
    "1",
    "true",
    "yes",
} or bool(config.get_bool("bootstrap_ecr_only"))
image_uri = os.environ.get("IMAGE_URI") or config.get("image_uri")
if not bootstrap_ecr_only and not image_uri:
    raise pulumi.RunError(
        "IMAGE_URI or mcp_tools:image_uri must identify a published linux/arm64 "
        "image. For a clean checkout, first run with BOOTSTRAP_ECR_ONLY=true to "
        "create the ECR repository, push an image, then redeploy with IMAGE_URI."
    )

execution_role_arn = config.get("execution_role_arn")
osm_user_agent = config.get("osm_user_agent") or ("Aws-agentic-blueprint/1.0 (AgentCore mcp-tools)")
osm_contact_email = config.get("osm_contact_email") or ""
overpass_url = config.get("overpass_url") or (
    "https://overpass.kumi.systems/api/interpreter,"
    "https://overpass-api.de/api/interpreter,"
    "https://overpass.osm.ch/api/interpreter,"
    "https://overpass.openstreetmap.fr/api/interpreter"
)
ddgs_backend = config.get("ddgs_backend") or "auto"
ddgs_timeout = config.get("ddgs_timeout") or "8"
ddgs_max_results = config.get("ddgs_max_results") or "10"
# Weather is served here by default (DoD: the hosted server is the full MCP
# tool set, and the open-source agent can point at this endpoint directly).
# Gateway tool names are prefixed per target (mcp-tools___getForecast vs
# open-meteo-tools___getForecast), so the Lambda target does not collide.
# Set "false" only if you explicitly want a web-and-map-only Runtime.
enable_weather_tools = config.get("enable_weather_tools") or "true"


repository = aws.ecr.Repository(
    "mcp-tools-images",
    name=ecr_repository_name,
    # SHA tags from CI are immutable; mutability would only help floating tags.
    image_tag_mutability="IMMUTABLE",
    image_scanning_configuration={"scan_on_push": True},
    force_delete=False,
)

if execution_role_arn:
    role_arn: pulumi.Input[str] = execution_role_arn
    runtime_dependencies: list[pulumi.Resource] = [repository]
else:
    assume_role = aws.iam.get_policy_document(
        statements=[
            {
                "effect": "Allow",
                "actions": ["sts:AssumeRole"],
                "principals": [
                    {
                        "type": "Service",
                        "identifiers": ["bedrock-agentcore.amazonaws.com"],
                    }
                ],
                "conditions": [
                    {
                        "test": "StringEquals",
                        "variable": "aws:SourceAccount",
                        "values": [account_id],
                    },
                    {
                        "test": "ArnLike",
                        "variable": "aws:SourceArn",
                        "values": [f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"],
                    },
                ],
            }
        ]
    )
    execution_role = aws.iam.Role(
        "mcp-tools-runtime-role",
        name=f"AmazonBedrockAgentCore-mcp-tools-{region}",
        description="Execution role for the opensource mcp-tools AgentCore Runtime",
        assume_role_policy=assume_role.json,
    )
    execution_policy = aws.iam.RolePolicy(
        "mcp-tools-runtime-policy",
        name=f"{runtime_name}-ExecutionPolicy",
        role=execution_role.id,
        policy=pulumi.Output.json_dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "ECRImageAccess",
                        "Effect": "Allow",
                        "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                        "Resource": [
                            f"arn:aws:ecr:{region}:{account_id}:repository/{ecr_repository_name}"
                        ],
                    },
                    {
                        "Sid": "ECRTokenAccess",
                        "Effect": "Allow",
                        "Action": ["ecr:GetAuthorizationToken"],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                        "Resource": [
                            f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["logs:DescribeLogGroups"],
                        "Resource": [f"arn:aws:logs:{region}:{account_id}:log-group:*"],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                        "Resource": [
                            f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "xray:PutTraceSegments",
                            "xray:PutTelemetryRecords",
                            "xray:GetSamplingRules",
                            "xray:GetSamplingTargets",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": "cloudwatch:PutMetricData",
                        "Resource": "*",
                        "Condition": {
                            "StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}
                        },
                    },
                    {
                        "Sid": "GetAgentAccessToken",
                        "Effect": "Allow",
                        "Action": [
                            "bedrock-agentcore:GetWorkloadAccessToken",
                            "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                            "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                        ],
                        "Resource": [
                            f"arn:aws:bedrock-agentcore:{region}:{account_id}:workload-identity-directory/default",
                            (
                                f"arn:aws:bedrock-agentcore:{region}:{account_id}:"
                                f"workload-identity-directory/default/workload-identity/{runtime_name}-*"
                            ),
                        ],
                    },
                ],
            }
        ),
    )
    role_arn = execution_role.arn
    runtime_dependencies = [repository, execution_policy]

pulumi.export("ecr_repository_url", repository.repository_url)
pulumi.export("ecr_repository_name", repository.name)
pulumi.export("bootstrap_ecr_only", bootstrap_ecr_only)
if not execution_role_arn:
    pulumi.export("execution_role_arn", role_arn)

if bootstrap_ecr_only:
    pulumi.log.info(
        "BOOTSTRAP_ECR_ONLY: created ECR (and role if needed); skipping AgentCore Runtime. "
        "Push a linux/arm64 image, then redeploy with IMAGE_URI set."
    )
else:
    runtime = aws.bedrock.AgentcoreAgentRuntime(
        "mcp-tools",
        agent_runtime_name=runtime_name,
        description="Open-source MCP tools (web search + OpenStreetMap)",
        role_arn=role_arn,
        agent_runtime_artifact={"container_configuration": {"container_uri": image_uri}},
        network_configuration={"network_mode": "PUBLIC"},
        protocol_configuration={"server_protocol": "MCP"},
        environment_variables={
            "MCP_HOST": "0.0.0.0",
            "MCP_PORT": "8000",
            "MCP_STATELESS_HTTP": "true",
            "ENABLE_WEATHER_TOOLS": enable_weather_tools,
            "DDGS_BACKEND": ddgs_backend,
            "DDGS_TIMEOUT": ddgs_timeout,
            "DDGS_MAX_RESULTS": ddgs_max_results,
            "OSM_USER_AGENT": osm_user_agent,
            "OSM_CONTACT_EMAIL": osm_contact_email,
            "OVERPASS_URL": overpass_url,
        },
        region=region,
        opts=pulumi.ResourceOptions(depends_on=runtime_dependencies),
    )

    mcp_url = runtime.agent_runtime_arn.apply(lambda arn: runtime_mcp_endpoint(arn, region))
    pulumi.export("runtime_id", runtime.agent_runtime_id)
    pulumi.export("runtime_arn", runtime.agent_runtime_arn)
    pulumi.export("mcp_endpoint", mcp_url)
    pulumi.export("image_uri", image_uri)
