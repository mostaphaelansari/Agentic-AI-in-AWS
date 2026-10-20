#!/usr/bin/env python3
"""Deploy Open-Meteo AgentCore Gateways for managed and custom agents.

Legacy imperative deployer for the Open-Meteo Lambda target only. New MCP-server
targets (mcp-tools Runtime, RapidAPI, Geoapify), API-key credential providers,
and RapidAPI x-api-host proxies are managed by Pulumi in
``infrastructure/pulumi/gateway/`` — do not extend this script for those.
"""

from __future__ import annotations

import argparse
import io
import json
import time
import zipfile
from pathlib import Path

import boto3

GATEWAY_NAME = "benchma-open-meteo"
GATEWAY_ROLE_NAME = "AgentCoreGatewayExecutionRole"
LAMBDA_FUNCTION_NAME = "benchma-open-meteo-tools"
LAMBDA_ROLE_NAME = "benchma-open-meteo-lambda-role"
TARGET_NAME = "open-meteo-tools"
HARNESS_ID = "harness_single-txRrfzwzCI"
HARNESS_REGION = "eu-central-1"
RUNTIME_REGION = "eu-west-1"
HARNESS_EXECUTION_ROLE = "arn:aws:iam::554025156005:role/service-role/AmazonBedrockAgentCoreRuntimeDefaultServiceRole-lwbq4"
RUNTIME_EXECUTION_ROLE = (
    "arn:aws:iam::554025156005:role/AmazonBedrockAgentCoreSDKRuntime-eu-west-1-d4f0bc5a29"
)

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[1]
STATE_PATH = ROOT / "deployed-state.json"

TOOL_SCHEMA = {
    "inlinePayload": [
        {
            "name": "getForecast",
            "description": "Get current weather or forecast from Open-Meteo for latitude/longitude.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "current_weather": {"type": "boolean"},
                    "daily": {"type": "string"},
                    "hourly": {"type": "string"},
                    "forecast_days": {"type": "integer"},
                    "timezone": {"type": "string"},
                },
                "required": ["latitude", "longitude"],
            },
        },
        {
            "name": "searchLocation",
            "description": "Resolve a city or place name to coordinates using Open-Meteo geocoding.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                    "language": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    ]
}


def _wait_ready(get_fn, resource_name: str, max_attempts: int = 60, delay: int = 5) -> dict:
    for attempt in range(max_attempts):
        response = get_fn()
        status = response.get("status", "UNKNOWN")
        if status == "READY":
            return response
        if status in {"FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"}:
            raise RuntimeError(f"{resource_name} failed with status {status}: {response}")
        print(f"  waiting for {resource_name} ({status}) attempt {attempt + 1}/{max_attempts}")
        time.sleep(delay)
    raise TimeoutError(f"{resource_name} did not become READY in time")


def _ensure_gateway_role(iam, sts, region: str) -> str:
    account_id = sts.get_caller_identity()["Account"]
    # Wildcard region so the same role works for gateways in any region.
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:*:{account_id}:*"},
                },
            }
        ],
    }
    try:
        role = iam.create_role(
            RoleName=GATEWAY_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Execution role for AgentCore Gateway",
        )
        print(f"Created gateway role: {role['Role']['Arn']}")
    except iam.exceptions.EntityAlreadyExistsException:
        role = iam.get_role(RoleName=GATEWAY_ROLE_NAME)
        # Always refresh the trust policy so region wildcard is applied on reuse.
        iam.update_assume_role_policy(
            RoleName=GATEWAY_ROLE_NAME,
            PolicyDocument=json.dumps(trust_policy),
        )
        print(f"Reusing gateway role (trust policy refreshed): {role['Role']['Arn']}")

    policy_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:*", "lambda:InvokeFunction"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": "*",
            },
        ],
    }
    iam.put_role_policy(
        RoleName=GATEWAY_ROLE_NAME,
        PolicyName="BedrockAgentCoreGatewayAccess",
        PolicyDocument=json.dumps(policy_doc),
    )
    return role["Role"]["Arn"]


def _ensure_lambda_role(iam) -> str:
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        role = iam.create_role(
            RoleName=LAMBDA_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Execution role for Benchma Open-Meteo gateway Lambda",
        )
    except iam.exceptions.EntityAlreadyExistsException:
        role = iam.get_role(RoleName=LAMBDA_ROLE_NAME)

    iam.attach_role_policy(
        RoleName=LAMBDA_ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    return role["Role"]["Arn"]


def _lambda_zip_bytes() -> bytes:
    source = (ROOT / "open_meteo_lambda.py").read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lambda_function.py", source)
    return buffer.getvalue()


def _wait_lambda_active(
    lambda_client, function_name: str, max_attempts: int = 30, delay: int = 3
) -> str:
    for attempt in range(max_attempts):
        response = lambda_client.get_function(FunctionName=function_name)
        state = response["Configuration"]["State"]
        if state == "Active":
            return response["Configuration"]["FunctionArn"]
        if state == "Failed":
            raise RuntimeError(f"Lambda {function_name} failed: {response['Configuration']}")
        print(f"  waiting for Lambda ({state}) attempt {attempt + 1}/{max_attempts}")
        time.sleep(delay)
    raise TimeoutError(f"Lambda {function_name} did not become Active in time")


def _ensure_lambda(lambda_client, iam, region: str, gateway_role_arn: str) -> str:
    role_arn = _ensure_lambda_role(iam)
    zip_bytes = _lambda_zip_bytes()

    try:
        response = lambda_client.get_function(FunctionName=LAMBDA_FUNCTION_NAME)
        lambda_client.update_function_code(FunctionName=LAMBDA_FUNCTION_NAME, ZipFile=zip_bytes)
        lambda_arn = response["Configuration"]["FunctionArn"]
        print(f"Updated Lambda in {region}: {lambda_arn}")
    except lambda_client.exceptions.ResourceNotFoundException:
        for attempt in range(10):
            try:
                response = lambda_client.create_function(
                    FunctionName=LAMBDA_FUNCTION_NAME,
                    Runtime="python3.13",
                    Role=role_arn,
                    Handler="lambda_function.lambda_handler",
                    Code={"ZipFile": zip_bytes},
                    Description="Open-Meteo proxy tools for Benchma AgentCore Gateway",
                    Timeout=30,
                )
                lambda_arn = response["FunctionArn"]
                print(f"Created Lambda in {region}: {lambda_arn}")
                break
            except lambda_client.exceptions.InvalidParameterValueException:
                if attempt == 9:
                    raise
                time.sleep(3)

    lambda_arn = _wait_lambda_active(lambda_client, LAMBDA_FUNCTION_NAME)

    try:
        lambda_client.add_permission(
            FunctionName=LAMBDA_FUNCTION_NAME,
            StatementId="AllowAgentCoreGatewayInvoke",
            Action="lambda:InvokeFunction",
            Principal=gateway_role_arn,
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass

    return lambda_arn


def _ensure_invoke_gateway_policy(iam, role_arn: str, gateway_id: str, region: str) -> None:
    role_name = role_arn.split("/")[-1]
    policy_name = f"InvokeGateway-{region}-{gateway_id}"
    policy_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:InvokeGateway"],
                "Resource": f"arn:aws:bedrock-agentcore:{region}:*:gateway/{gateway_id}",
            }
        ],
    }
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(policy_doc),
    )
    print(f"  attached InvokeGateway policy to {role_name}")


def _ensure_code_interpreter_policy(iam, role_arn: str, region: str) -> None:
    role_name = role_arn.split("/")[-1]
    account_id = boto3.client("sts").get_caller_identity()["Account"]
    policy_doc = {
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
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="AgentCoreCodeInterpreterSessions",
        PolicyDocument=json.dumps(policy_doc),
    )
    print(f"  attached Code Interpreter session policy to {role_name}")


def _get_gateway_by_name(client, name: str) -> dict | None:
    token = None
    while True:
        kwargs = {}
        if token:
            kwargs["nextToken"] = token
        response = client.list_gateways(**kwargs)
        for item in response.get("items", []):
            if item.get("name") == name:
                return client.get_gateway(gatewayIdentifier=item["gatewayId"])
        token = response.get("nextToken")
        if not token:
            return None


def _ensure_gateway(client, iam, sts, region: str) -> dict:
    existing = _get_gateway_by_name(client, GATEWAY_NAME)
    if existing:
        print(f"Gateway already exists in {region}: {existing['gatewayId']}")
        return existing

    role_arn = _ensure_gateway_role(iam, sts, region)
    response = client.create_gateway(
        name=GATEWAY_NAME,
        description="Open-Meteo weather API for Benchma harness/runtime benchmark",
        roleArn=role_arn,
        protocolType="MCP",
        authorizerType="AWS_IAM",
    )
    print(f"Created gateway in {region}: {response['gatewayId']}")
    return _wait_ready(
        lambda: client.get_gateway(gatewayIdentifier=response["gatewayId"]),
        f"gateway/{response['gatewayId']}",
    )


def _get_target_id(client, gateway_id: str, name: str) -> str | None:
    token = None
    while True:
        kwargs = {"gatewayIdentifier": gateway_id}
        if token:
            kwargs["nextToken"] = token
        response = client.list_gateway_targets(**kwargs)
        for item in response.get("items", []):
            if item.get("name") == name:
                return item["targetId"]
        token = response.get("nextToken")
        if not token:
            return None


def _ensure_lambda_target(
    client, gateway: dict, lambda_arn: str, stored_target_id: str | None = None
) -> str:
    gateway_id = gateway["gatewayId"]

    # Prefer the stored target ID from state — list_gateway_targets has eventual-consistency
    # gaps where a target exists but doesn't appear in listing results.
    existing_id = stored_target_id or _get_target_id(client, gateway_id, TARGET_NAME)
    if existing_id:
        try:
            client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=existing_id)
            print(f"  deleted stale target: {TARGET_NAME} ({existing_id})")
            time.sleep(3)
        except Exception as del_err:
            print(f"  warning: could not delete target {existing_id}: {del_err}")

    target_config = {
        "gatewayIdentifier": gateway_id,
        "name": TARGET_NAME,
        "description": "Open-Meteo getForecast and searchLocation tools",
        "targetConfiguration": {
            "mcp": {
                "lambda": {
                    "lambdaArn": lambda_arn,
                    "toolSchema": TOOL_SCHEMA,
                }
            }
        },
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    }

    for attempt in range(6):
        try:
            response = client.create_gateway_target(**target_config)
            break
        except client.exceptions.ConflictException:
            wait = 10 * (attempt + 1)
            print(
                f"  ConflictException; waiting {wait}s for deletion to propagate (attempt {attempt + 1}/6)…"
            )
            time.sleep(wait)
            # Re-check list in case eventual consistency has caught up
            fresh_id = _get_target_id(client, gateway_id, TARGET_NAME)
            if fresh_id:
                client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=fresh_id)
                print(f"  deleted late-appearing target {fresh_id}")
                time.sleep(5)
    else:
        raise RuntimeError(f"Could not create target {TARGET_NAME} after 6 attempts")

    target_id = response["targetId"]
    _wait_ready(
        lambda: client.get_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
        ),
        f"target/{TARGET_NAME}",
    )
    print(f"  created target: {TARGET_NAME} ({target_id})")
    return target_id


def _update_harness(client, gateway_arn: str) -> None:
    tools = [
        {
            "type": "agentcore_browser",
            "name": "browser",
            "config": {
                "agentCoreBrowser": {
                    "browserArn": f"arn:aws:bedrock-agentcore:{HARNESS_REGION}:aws:browser/aws.browser.v1",
                },
            },
        },
        {
            "type": "agentcore_code_interpreter",
            "name": "code_interpreter",
            "config": {
                "agentCoreCodeInterpreter": {
                    "codeInterpreterArn": (
                        f"arn:aws:bedrock-agentcore:{HARNESS_REGION}:aws:"
                        "code-interpreter/aws.codeinterpreter.v1"
                    )
                }
            },
        },
        {
            "type": "agentcore_gateway",
            "name": "open_meteo_tools",
            "config": {"agentCoreGateway": {"gatewayArn": gateway_arn}},
        },
    ]
    prompt_path = REPO_ROOT / "agents" / "managed" / "system-prompt.md"
    # Keep the Managed prompt stable across deploy days. The current date is
    # injected per invocation by callers so relative-date handling stays fresh.
    system_prompt = prompt_path.read_text().strip()
    client.update_harness(
        harnessId=HARNESS_ID,
        tools=tools,
        allowedTools=["*"],
        systemPrompt=[{"text": system_prompt}],
        # Benchmark mode is orchestration-only: Managed and Custom should both
        # be stateless, so detach AgentCore Memory from the harness.
        memory={},
    )
    print(f"Updated harness {HARNESS_ID} with gateway tool, prompt, and memory disabled")


def deploy_region(region: str, update_harness: bool, stored_state: dict | None = None) -> dict:
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    lambda_client = boto3.client("lambda", region_name=region)
    iam = boto3.client("iam")
    sts = boto3.client("sts")

    gateway = _ensure_gateway(client, iam, sts, region)
    lambda_arn = _ensure_lambda(lambda_client, iam, region, gateway["roleArn"])
    stored_target_id = (stored_state or {}).get("targetId")
    target_id = _ensure_lambda_target(
        client, gateway, lambda_arn, stored_target_id=stored_target_id
    )

    _ensure_invoke_gateway_policy(iam, HARNESS_EXECUTION_ROLE, gateway["gatewayId"], region)
    _ensure_invoke_gateway_policy(iam, RUNTIME_EXECUTION_ROLE, gateway["gatewayId"], region)
    _ensure_code_interpreter_policy(iam, HARNESS_EXECUTION_ROLE, HARNESS_REGION)

    if update_harness:
        harness_client = boto3.client("bedrock-agentcore-control", region_name=HARNESS_REGION)
        _update_harness(harness_client, gateway["gatewayArn"])

    return {
        "region": region,
        "gatewayId": gateway["gatewayId"],
        "gatewayArn": gateway["gatewayArn"],
        "gatewayUrl": gateway["gatewayUrl"],
        "lambdaArn": lambda_arn,
        "targetId": target_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--regions",
        nargs="+",
        default=[HARNESS_REGION, RUNTIME_REGION],
        help="Regions to deploy gateways into",
    )
    parser.add_argument(
        "--skip-harness-update",
        action="store_true",
        help="Do not update the managed Harness configuration",
    )
    args = parser.parse_args()

    existing_by_region: dict = {}
    if STATE_PATH.exists():
        try:
            existing_by_region = {
                g["region"]: g for g in json.loads(STATE_PATH.read_text()).get("gateways", [])
            }
        except Exception:
            pass

    state = {"gateways": []}
    for region in args.regions:
        print(f"\n=== Deploying gateway in {region} ===")
        info = deploy_region(
            region,
            update_harness=(region == HARNESS_REGION and not args.skip_harness_update),
            stored_state=existing_by_region.get(region),
        )
        state["gateways"].append(info)
        print(f"Gateway URL: {info['gatewayUrl']}")

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
    print(f"\nWrote state to {STATE_PATH}")


if __name__ == "__main__":
    main()
