"""Register the repo's Open-Meteo capability in AWS Agent Registry."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import boto3

CREATING_STATUSES = {"CREATING", "UPDATING"}
FAILED_STATUSES = {"CREATE_FAILED", "UPDATE_FAILED"}
READY_STATUSES = {"DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED", "DEPRECATED"}


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def build_open_meteo_mcp_descriptors(args: argparse.Namespace) -> dict[str, Any]:
    """Build MCP descriptor content for the Open-Meteo Gateway tools."""
    server: dict[str, Any] = {
        "name": "com.benchma.open-meteo",
        "description": "Read-only Open-Meteo weather tools exposed through AgentCore Gateway.",
        "version": args.record_version,
        "transport": "streamable-http",
        "side_effect_class": "read-only",
        "owner": args.owner,
        "source": {
            "repository": "Aws-agentic-blueprint",
            "paths": [
                "infrastructure/pulumi/gateway/__main__.py",
                "infrastructure/gateway/open_meteo_lambda.py",
            ],
        },
    }
    if args.gateway_url:
        server["endpoint"] = args.gateway_url

    tools = {
        "tools": [
            {
                "name": "searchLocation",
                "description": "Resolve a city or place name to coordinates using Open-Meteo geocoding.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "City or place name."},
                        "count": {"type": "integer", "description": "Maximum number of matches."},
                        "language": {
                            "type": "string",
                            "description": "Preferred response language.",
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "getForecast",
                "description": "Get current weather or forecast from Open-Meteo for latitude/longitude.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "latitude": {"type": "number"},
                        "longitude": {"type": "number"},
                        "current_weather": {"type": "boolean"},
                        "forecast_days": {"type": "integer"},
                        "timezone": {"type": "string"},
                        "daily": {"type": "string"},
                        "hourly": {"type": "string"},
                    },
                    "required": ["latitude", "longitude"],
                    "additionalProperties": False,
                },
            },
        ],
        "metadata": {
            "domain": "weather",
            "side_effect_class": "read-only",
            "owner": args.owner,
            "evaluation_gate": "evaluations/agentcore_evaluation_config.json",
        },
    }

    return {
        "mcp": {
            "server": {"inlineContent": _json_dumps(server)},
            "tools": {"inlineContent": _json_dumps(tools)},
        }
    }


def build_open_meteo_custom_descriptors(args: argparse.Namespace) -> dict[str, Any]:
    """Build a custom descriptor for the Open-Meteo Gateway capability."""
    payload: dict[str, Any] = {
        "type": "agentcore-gateway-tools",
        "name": "com.benchma.open-meteo",
        "description": "Read-only Open-Meteo weather tools exposed through AgentCore Gateway.",
        "version": args.record_version,
        "domain": "weather",
        "runtime": "AgentCore Gateway",
        "transport": "MCP streamable HTTP",
        "side_effect_class": "read-only",
        "owner": args.owner,
        "source": {
            "repository": "Aws-agentic-blueprint",
            "paths": [
                "infrastructure/pulumi/gateway/__main__.py",
                "infrastructure/gateway/open_meteo_lambda.py",
            ],
        },
        "evaluation_gate": "evaluations/agentcore_evaluation_config.json",
        "tools": [
            {
                "name": "searchLocation",
                "description": "Resolve a city or place name to coordinates using Open-Meteo geocoding.",
                "side_effect_class": "read-only",
                "required_inputs": ["name"],
            },
            {
                "name": "getForecast",
                "description": "Get current weather or forecast from Open-Meteo for latitude/longitude.",
                "side_effect_class": "read-only",
                "required_inputs": ["latitude", "longitude"],
            },
        ],
    }
    if args.gateway_url:
        payload["endpoint"] = args.gateway_url

    return {"custom": {"inlineContent": _json_dumps(payload)}}


def build_descriptors(args: argparse.Namespace) -> dict[str, Any]:
    if args.descriptor_type == "MCP":
        return build_open_meteo_mcp_descriptors(args)
    return build_open_meteo_custom_descriptors(args)


def record_id_from_arn(record_arn: str) -> str:
    if "/record/" in record_arn:
        return record_arn.rsplit("/record/", 1)[-1]
    return record_arn.rsplit("/", 1)[-1]


def wait_for_record(
    client,
    *,
    registry_id: str,
    record_id: str,
    target_statuses: set[str] | None = None,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        record = client.get_registry_record(registryId=registry_id, recordId=record_id)
        status = record["status"]
        print(f"Record status: {status}")

        if status in FAILED_STATUSES:
            raise RuntimeError(f"Registry record failed: {json.dumps(record, default=str)}")
        if target_statuses and status in target_statuses:
            return record
        if not target_statuses and status in READY_STATUSES:
            return record
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for registry record {record_id}")

        time.sleep(poll_interval_seconds)


def search_until_visible(
    client,
    *,
    registry_id: str,
    query: str,
    record_id: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = client.search_registry_records(
            registryIds=[registry_id],
            searchQuery=query,
            maxResults=10,
        )
        records = response.get("registryRecords", [])
        if any(record.get("recordId") == record_id for record in records):
            return records
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Record {record_id} did not appear in search results")

        print(f"Record not visible in search yet for query {query!r}; waiting...")
        time.sleep(poll_interval_seconds)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register the repo's Open-Meteo tools in AWS Agent Registry."
    )
    parser.add_argument("--registry-id", required=True, help="Agent Registry ID or ARN.")
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-west-1",
    )
    parser.add_argument("--record-name", default="benchma-open-meteo-tools")
    parser.add_argument("--record-version", default="1.0.0")
    parser.add_argument(
        "--descriptor-type",
        choices=["CUSTOM", "MCP"],
        default="CUSTOM",
        help=(
            "Registry descriptor type. CUSTOM is the default because it is stable "
            "for repo-owned Gateway metadata; MCP requires service-accepted MCP descriptor JSON."
        ),
    )
    parser.add_argument("--owner", default="agent-platform-team")
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("AGENTCORE_GATEWAY_URL"),
        help="Optional AgentCore Gateway MCP endpoint URL to include in the descriptor.",
    )
    parser.add_argument(
        "--description",
        default="Read-only Open-Meteo weather tools exposed through AgentCore Gateway.",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Approve the record after submission. Use only with a curator/admin role.",
    )
    parser.add_argument(
        "--skip-submit",
        action="store_true",
        help="Create the record but do not submit it for approval.",
    )
    parser.add_argument(
        "--skip-search",
        action="store_true",
        help="Do not wait for the approved record to appear in search results.",
    )
    parser.add_argument("--search-query", default="weather forecast open meteo")
    parser.add_argument("--wait-seconds", type=int, default=180)
    parser.add_argument("--search-wait-seconds", type=int, default=180)
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the create_registry_record request without calling AWS.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    descriptors = build_descriptors(args)
    create_request = {
        "registryId": args.registry_id,
        "name": args.record_name,
        "description": args.description,
        "descriptorType": args.descriptor_type,
        "descriptors": descriptors,
        "recordVersion": args.record_version,
    }

    if args.dry_run:
        print(json.dumps(create_request, indent=2))
        return 0

    control = boto3.client("bedrock-agentcore-control", region_name=args.region)
    runtime = boto3.client("bedrock-agentcore", region_name=args.region)

    created = control.create_registry_record(**create_request)
    record_id = record_id_from_arn(created["recordArn"])
    print(f"Record created: {record_id}")

    record = wait_for_record(
        control,
        registry_id=args.registry_id,
        record_id=record_id,
        timeout_seconds=args.wait_seconds,
        poll_interval_seconds=args.poll_seconds,
    )
    status = record["status"]

    if not args.skip_submit and status == "DRAFT":
        control.submit_registry_record_for_approval(
            registryId=args.registry_id,
            recordId=record_id,
        )
        print("Submitted for approval")
        record = wait_for_record(
            control,
            registry_id=args.registry_id,
            record_id=record_id,
            target_statuses={"PENDING_APPROVAL", "APPROVED"},
            timeout_seconds=args.wait_seconds,
            poll_interval_seconds=args.poll_seconds,
        )
        status = record["status"]

    if args.approve and status == "PENDING_APPROVAL":
        control.update_registry_record_status(
            registryId=args.registry_id,
            recordId=record_id,
            status="APPROVED",
            statusReason="Approved for the read-only Open-Meteo weather use case.",
        )
        print("Approved")
        record = wait_for_record(
            control,
            registry_id=args.registry_id,
            record_id=record_id,
            target_statuses={"APPROVED"},
            timeout_seconds=args.wait_seconds,
            poll_interval_seconds=args.poll_seconds,
        )
        status = record["status"]

    records = control.list_registry_records(registryId=args.registry_id)
    print("Registry records:")
    print(json.dumps(records.get("registryRecords", []), indent=2, default=str))

    if status == "APPROVED" and not args.skip_search:
        results = search_until_visible(
            runtime,
            registry_id=args.registry_id,
            query=args.search_query,
            record_id=record_id,
            timeout_seconds=args.search_wait_seconds,
            poll_interval_seconds=args.poll_seconds,
        )
        print("Search results:")
        print(json.dumps(results, indent=2, default=str))
    elif not args.skip_search:
        print(f"Skipping search because record status is {status!r}, not 'APPROVED'.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
