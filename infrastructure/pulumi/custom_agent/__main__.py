"""Pulumi program for the code-defined AgentCore Runtime agent."""

from __future__ import annotations

import os

import pulumi
import pulumi_aws as aws

config = pulumi.Config()
region = aws.get_region().region
account_id = aws.get_caller_identity().account_id
gateway_stack = pulumi.StackReference(config.require("gateway_stack"))
gateway_url = gateway_stack.require_output("gateway_url")
# Use the same Gateway stack as the Managed Harness by default.  An explicit
# override remains available for migration/debugging, but benchmark stacks
# must leave it unset so both AWS architectures discover the identical MCP
# targets and schemas.
runtime_gateway_url = config.get("runtime_gateway_url") or gateway_url
runtime_name = config.get("runtime_name") or "agent"
ecr_repository_name = config.get("ecr_repository_name") or "bedrock-agentcore-agent"
execution_role_arn = config.require("execution_role_arn")
execution_role_name = execution_role_arn.rsplit("/", 1)[-1]
offload_bucket = config.require("offload_bucket")
skills_bucket = config.require("skills_bucket")
skills_prefix = config.get("skills_prefix") or "harness-skills"
model_id = config.get("model_id") or "eu.anthropic.claude-sonnet-4-6"
cheap_model_id = (
    config.get("cheap_model_id") or "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
)
image_uri = os.environ.get("IMAGE_URI") or config.get("image_uri")
if not image_uri:
    raise pulumi.RunError("IMAGE_URI or custom_agent:image_uri must identify a published image")
import_runtime_id = config.get("import_runtime_id")
mlflow_otlp_traces_endpoint = config.get("mlflow_otlp_traces_endpoint")
mlflow_experiment_id = config.get("mlflow_experiment_id") or "0"
bedrock_prompt_id = os.environ.get("BEDROCK_PROMPT_ID") or config.get("bedrock_prompt_id")
bedrock_prompt_version = os.environ.get("BEDROCK_PROMPT_VERSION") or config.get(
    "bedrock_prompt_version"
)
bedrock_prompt_sha256 = os.environ.get("BEDROCK_PROMPT_SHA256") or config.get(
    "bedrock_prompt_sha256"
)
gateway_policy_name = (
    config.get("gateway_policy_name") or "InvokeGateway-eu-central-1-benchma-open-meteo-pb9jwtynsj"
)
enable_code_interpreter = config.get("enable_code_interpreter") or "true"
enable_browser = config.get("enable_browser") or "false"
max_tool_calls = config.get_int("max_tool_calls") or 12
if max_tool_calls < 1:
    raise pulumi.RunError("custom_agent:max_tool_calls must be at least 1")
bedrock_max_tokens = config.get_int("bedrock_max_tokens") or 4096
if bedrock_max_tokens < 1024:
    raise pulumi.RunError("custom_agent:bedrock_max_tokens must be at least 1024")


def _runtime_environment_variables() -> dict[str, str]:
    """Build Runtime env vars, optionally mirroring in-process spans to MLflow."""
    env: dict[str, str] = {
        "AWS_REGION": region,
        "BEDROCK_MODEL_ID": model_id,
        "BEDROCK_CHEAP_MODEL_ID": cheap_model_id,
        "GATEWAY_URL": runtime_gateway_url,
        "OFFLOAD_BUCKET": offload_bucket,
        "SKILLS_S3_BUCKET": skills_bucket,
        "SKILLS_S3_PREFIX": skills_prefix,
        "ENABLE_PROMPT_CACHING": "true",
        "ENABLE_THINKING": "false",
        "ENABLE_CODE_INTERPRETER": enable_code_interpreter,
        "ENABLE_BROWSER": enable_browser,
        "MAX_TOOL_CALLS": str(max_tool_calls),
        "BEDROCK_MAX_TOKENS": str(bedrock_max_tokens),
    }
    if bedrock_prompt_id and bedrock_prompt_version:
        env.update(
            {
                "BEDROCK_PROMPT_ID": bedrock_prompt_id,
                "BEDROCK_PROMPT_VERSION": bedrock_prompt_version,
            }
        )
        if bedrock_prompt_sha256:
            env["BEDROCK_PROMPT_SHA256"] = bedrock_prompt_sha256
    if mlflow_otlp_traces_endpoint:
        env.update(
            {
                "MLFLOW_OTLP_TRACES_ENDPOINT": mlflow_otlp_traces_endpoint,
                "MLFLOW_EXPERIMENT_ID": mlflow_experiment_id,
            }
        )
    return env


repository = aws.ecr.Repository(
    "custom-agent-images",
    name=ecr_repository_name,
    image_tag_mutability="MUTABLE",
    image_scanning_configuration={"scan_on_push": False},
    force_delete=False,
    opts=pulumi.ResourceOptions(import_=config.get("import_ecr_repository_id")),
)

aws.iam.RolePolicy(
    "invoke-gateway",
    name=gateway_policy_name,
    role=execution_role_name,
    policy=pulumi.Output.json_dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["bedrock-agentcore:InvokeGateway"],
                    "Resource": gateway_stack.require_output("gateway_arn").apply(
                        lambda arn: arn.replace(f":{account_id}:", ":*:")
                    ),
                }
            ],
        }
    ),
    opts=pulumi.ResourceOptions(import_=config.get("import_gateway_policy_id")),
)

skills_bucket_read_policy = aws.iam.RolePolicy(
    "read-shared-skills",
    name=f"{runtime_name}-ReadSkills",
    role=execution_role_name,
    policy=pulumi.Output.json_dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": f"arn:aws:s3:::{skills_bucket}",
                    "Condition": {
                        "StringLike": {"s3:prefix": [f"{skills_prefix}/*"]},
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    "Resource": f"arn:aws:s3:::{skills_bucket}/{skills_prefix}/*",
                },
            ],
        }
    ),
)

# Runtime-used subset of the documented Code Interpreter policy; resource
# pattern per the AgentCore devguide (code-interpreter-resource-session-management).
code_interpreter_policy = aws.iam.RolePolicy(
    "code-interpreter-sessions",
    name=f"{runtime_name}-CodeInterpreterSessions",
    role=execution_role_name,
    policy=pulumi.Output.json_dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:StartCodeInterpreterSession",
                        "bedrock-agentcore:InvokeCodeInterpreter",
                        "bedrock-agentcore:GetCodeInterpreterSession",
                        "bedrock-agentcore:StopCodeInterpreterSession",
                    ],
                    "Resource": [
                        f"arn:aws:bedrock-agentcore:{region}:{account_id}:code-interpreter/*",
                        (
                            f"arn:aws:bedrock-agentcore:{region}:aws:"
                            "code-interpreter/aws.codeinterpreter.v1"
                        ),
                    ],
                }
            ],
        }
    ),
)

runtime_dependencies: list[pulumi.Resource] = [
    repository,
    code_interpreter_policy,
    skills_bucket_read_policy,
]
if bedrock_prompt_id:
    prompt_resource_arns = [
        f"arn:aws:bedrock:{region}:{account_id}:prompt/{bedrock_prompt_id}",
    ]
    if bedrock_prompt_version:
        prompt_resource_arns.append(
            f"arn:aws:bedrock:{region}:{account_id}:prompt/{bedrock_prompt_id}:{bedrock_prompt_version}"
        )

    read_prompt_policy = aws.iam.RolePolicy(
        "read-bedrock-prompt",
        name=f"{runtime_name}-ReadPrompt",
        role=execution_role_name,
        policy=pulumi.Output.json_dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["bedrock:GetPrompt"],
                        "Resource": prompt_resource_arns,
                    }
                ],
            }
        ),
    )
    runtime_dependencies.append(read_prompt_policy)

runtime = aws.bedrock.AgentcoreAgentRuntime(
    "custom-agent",
    agent_runtime_name=runtime_name,
    description="Code-defined Strands comparison agent",
    role_arn=execution_role_arn,
    agent_runtime_artifact={"container_configuration": {"container_uri": image_uri}},
    network_configuration={"network_mode": "PUBLIC"},
    protocol_configuration={"server_protocol": "HTTP"},
    environment_variables=_runtime_environment_variables(),
    region=region,
    opts=pulumi.ResourceOptions(
        import_=import_runtime_id,
        depends_on=runtime_dependencies,
        # Preserve the imported description, but allow reviewed runtime env changes to deploy.
        ignore_changes=(["description"] if import_runtime_id else None),
    ),
)

pulumi.export("ecr_repository_url", repository.repository_url)
pulumi.export("runtime_id", runtime.agent_runtime_id)
pulumi.export("runtime_arn", runtime.agent_runtime_arn)
