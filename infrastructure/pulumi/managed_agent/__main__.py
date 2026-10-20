"""Pulumi program for the managed AgentCore Harness agent."""

from __future__ import annotations

from pathlib import Path

import pulumi
import pulumi_aws as aws
import pulumi_aws_native as aws_native

config = pulumi.Config()
region = aws.get_region().region
account_id = aws.get_caller_identity().account_id
gateway_stack = pulumi.StackReference(config.require("gateway_stack"))
gateway_arn = gateway_stack.require_output("gateway_arn")
execution_role_arn = config.require("execution_role_arn")
execution_role_name = execution_role_arn.rsplit("/", 1)[-1]
harness_name = config.get("harness_name") or "harness_single"
model_id = config.get("model_id") or "eu.anthropic.claude-sonnet-4-6"
max_iterations = config.get_int("max_iterations") or 12
timeout_seconds = config.get_int("timeout_seconds") or 900
enable_browser = config.get_bool("enable_browser") or False
import_harness_id = config.get("import_harness_id")
gateway_policy_name = (
    config.get("gateway_policy_name") or "InvokeGateway-eu-central-1-benchma-open-meteo-pb9jwtynsj"
)
prompt_path = Path(__file__).resolve().parents[3] / "agents" / "managed" / "system-prompt.md"
system_prompt = prompt_path.read_text().strip()
skills_path = Path(__file__).resolve().parents[3] / "opensource" / "agents" / "custom" / "skills"
skill_names = sorted(
    child.name for child in skills_path.iterdir() if (child / "SKILL.md").is_file()
)
skill_prefix = "harness-skills"

aws_native_provider = aws_native.Provider("aws-native", region=region)

skills_bucket = aws.s3.BucketV2(
    "harness-skills",
    bucket=f"agentic-blueprint-harness-skills-{account_id}-{region}",
)

aws.s3.BucketPublicAccessBlock(
    "harness-skills-public-access-block",
    bucket=skills_bucket.id,
    block_public_acls=True,
    block_public_policy=True,
    ignore_public_acls=True,
    restrict_public_buckets=True,
)

aws.s3.BucketServerSideEncryptionConfigurationV2(
    "harness-skills-encryption",
    bucket=skills_bucket.id,
    rules=[
        {
            "apply_server_side_encryption_by_default": {
                "sse_algorithm": "AES256",
            }
        }
    ],
)

skill_objects = [
    aws.s3.BucketObjectv2(
        f"harness-skill-{skill_name}",
        bucket=skills_bucket.id,
        key=f"{skill_prefix}/{skill_name}/SKILL.md",
        source=pulumi.FileAsset(str(skills_path / skill_name / "SKILL.md")),
        content_type="text/markdown; charset=utf-8",
    )
    for skill_name in skill_names
]

skill_uris = [
    skills_bucket.bucket.apply(
        lambda bucket, skill_name=skill_name: f"s3://{bucket}/{skill_prefix}/{skill_name}/"
    )
    for skill_name in skill_names
]

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
                    "Resource": gateway_arn.apply(
                        lambda arn: arn.replace(f":{account_id}:", ":*:")
                    ),
                }
            ],
        }
    ),
    opts=pulumi.ResourceOptions(import_=config.get("import_gateway_policy_id")),
)

skills_bucket_read_policy = aws.iam.RolePolicy(
    "read-harness-skills",
    name=f"{harness_name}-ReadSkills",
    role=execution_role_name,
    policy=pulumi.Output.json_dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": skills_bucket.arn,
                    "Condition": {
                        "StringLike": {
                            "s3:prefix": [
                                f"{skill_prefix}/*",
                            ]
                        }
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    "Resource": skills_bucket.arn.apply(
                        lambda arn: f"{arn}/{skill_prefix}/*",
                    ),
                },
            ],
        }
    ),
)

code_interpreter_policy = aws.iam.RolePolicy(
    "code-interpreter-sessions",
    name=f"{harness_name}-CodeInterpreterSessions",
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
                            f"arn:aws:bedrock-agentcore:{region}:{account_id}:"
                            "code-interpreter-custom/*"
                        ),
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

# The AWS-managed default interpreter uses sandbox networking and cannot fetch
# the comparison UI's short-lived S3 URL. A public-network custom interpreter
# keeps the file transfer out of the model context and avoids large tool-call
# arguments exhausting the model's output-token budget.
code_interpreter = aws_native.bedrockagentcore.CodeInterpreterCustom(
    "managed-code-interpreter",
    name=f"{harness_name}_public",
    description="Managed Harness interpreter for temporary comparison uploads",
    network_configuration={"network_mode": "PUBLIC"},
    opts=pulumi.ResourceOptions(provider=aws_native_provider),
)

harness_tools = [
    {
        "type": "agentcore_code_interpreter",
        "name": "code_interpreter",
        "config": {
            "agent_core_code_interpreter": {
                "code_interpreter_arn": code_interpreter.code_interpreter_arn,
            }
        },
    },
    {
        "type": "agentcore_gateway",
        "name": "shared_gateway_tools",
        "config": {"agent_core_gateway": {"gateway_arn": gateway_arn}},
    },
]
if enable_browser:
    # Browser is an AWS-native extra. Keep it disabled for parity benchmarks;
    # opt in only for a separately labelled native-capabilities comparison.
    harness_tools.insert(
        0,
        {
            "type": "agentcore_browser",
            "name": "browser",
            "config": {
                "agent_core_browser": {
                    "browser_arn": (
                        f"arn:aws:bedrock-agentcore:{region}:aws:browser/aws.browser.v1"
                    )
                }
            },
        },
    )

harness = aws_native.bedrockagentcore.Harness(
    "managed-agent",
    harness_name=harness_name,
    execution_role_arn=execution_role_arn,
    model={"bedrock_model_config": {"model_id": model_id, "api_format": "converse_stream"}},
    system_prompt=[{"text": system_prompt}],
    skills=[{"s3": {"uri": skill_uri}} for skill_uri in skill_uris],
    environment={
        "agent_core_runtime_environment": {
            "lifecycle_configuration": {
                "idle_runtime_session_timeout": 900,
                "max_lifetime": 28800,
            },
            "network_configuration": {"network_mode": "PUBLIC"},
        }
    },
    truncation={
        "strategy": "sliding_window",
        "config": {"sliding_window": {"messages_count": 150}},
    },
    max_iterations=max_iterations,
    timeout_seconds=timeout_seconds,
    allowed_tools=["*"],
    tools=harness_tools,
    opts=pulumi.ResourceOptions(
        import_=import_harness_id,
        provider=aws_native_provider,
        aliases=[pulumi.Alias(type_="aws:bedrock/agentcoreHarness:AgentcoreHarness")],
        depends_on=[
            code_interpreter,
            code_interpreter_policy,
            skills_bucket_read_policy,
            *skill_objects,
        ],
        ignore_changes=(["maxTokens", "memory", "model"] if import_harness_id else None),
    ),
)

pulumi.export("harness_id", harness.harness_id)
pulumi.export("harness_arn", harness.arn)
pulumi.export("code_interpreter_arn", code_interpreter.code_interpreter_arn)
pulumi.export("skills_bucket", skills_bucket.bucket)
pulumi.export("skill_uris", skill_uris)
