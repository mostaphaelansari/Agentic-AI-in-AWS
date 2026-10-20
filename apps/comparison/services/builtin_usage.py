"""Fetch measured AgentCore Browser and Code Interpreter compute usage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from config import AGENTCORE_BUILTIN_TOOL_PRICING

BUILTIN_USAGE_PENDING = "pending"
BUILTIN_USAGE_AVAILABLE = "available"
BUILTIN_USAGE_ERROR = "error"

TOOL_SPECS = {
    "browser": {
        "default_identifier": "aws.browser.v1",
        "harness_key": "agentCoreBrowser",
        "harness_arn_key": "browserArn",
        "list_method": "list_browser_sessions",
        "identifier_arg": "browserIdentifier",
        "stream": "BedrockAgentCoreBrowser_UsageLogs",
    },
    "code_interpreter": {
        "default_identifier": "aws.codeinterpreter.v1",
        "harness_key": "agentCoreCodeInterpreter",
        "harness_arn_key": "codeInterpreterArn",
        "list_method": "list_code_interpreter_sessions",
        "identifier_arg": "codeInterpreterIdentifier",
        "stream": "BedrockAgentCoreCodeInterpreter_UsageLogs",
    },
}


def builtin_usage_cost(vcpu_hours: float, memory_gb_hours: float) -> dict[str, float]:
    cpu_cost = vcpu_hours * AGENTCORE_BUILTIN_TOOL_PRICING["vcpu_hour"]
    memory_cost = memory_gb_hours * AGENTCORE_BUILTIN_TOOL_PRICING["gb_hour"]
    return {
        "cpu_cost": cpu_cost,
        "memory_cost": memory_cost,
        "builtin_cost": cpu_cost + memory_cost,
    }


def _epoch_seconds(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return float(value or 0.0)


def _harness_tool_identifiers(config: dict) -> dict[str, str]:
    harness_id = (config.get("arn") or "").rsplit("/", 1)[-1]
    if not harness_id:
        return {}
    control = boto3.client("bedrock-agentcore-control", region_name=config["region"])
    harness = control.get_harness(harnessId=harness_id).get("harness", {})
    identifiers: dict[str, str] = {}
    for tool in harness.get("tools", []):
        tool_config = tool.get("config", {})
        for kind, spec in TOOL_SPECS.items():
            details = tool_config.get(spec["harness_key"], {})
            if arn := details.get(spec["harness_arn_key"]):
                identifiers[kind] = arn.rsplit("/", 1)[-1]
    return identifiers


def _tool_identifiers(config: dict, kinds: set[str]) -> dict[str, str]:
    harness = _harness_tool_identifiers(config) if config.get("service") == "harness" else {}
    return {kind: harness.get(kind, TOOL_SPECS[kind]["default_identifier"]) for kind in kinds}


def _matching_tool_session(
    client,
    kind: str,
    identifier: str,
    app_session_id: str,
    started_at: float | None,
    completed_at: float | None,
) -> dict | None:
    spec = TOOL_SPECS[kind]
    method = getattr(client, spec["list_method"])
    params = {spec["identifier_arg"]: identifier, "maxResults": 100}
    candidates: list[dict] = []
    window_start = float(started_at or 0.0) - 300
    window_end = float(completed_at or datetime.now(timezone.utc).timestamp()) + 300

    while True:
        response = method(**params)
        for item in response.get("items", []):
            if item.get("name") == app_session_id:
                return item
            created_at = _epoch_seconds(item.get("createdAt"))
            if window_start <= created_at <= window_end:
                candidates.append(item)
        next_token = response.get("nextToken")
        if not next_token:
            break
        params["nextToken"] = next_token

    # Harness creates opaque built-in-tool session names. A unique session
    # created during this run is safe to attribute; concurrent candidates are
    # deliberately left unresolved rather than charged to the wrong run.
    return candidates[0] if len(candidates) == 1 else None


def _discover_log_group(logs, resource_arn: str) -> str | None:
    sources = logs.describe_delivery_sources().get("deliverySources", [])
    names = {
        source["name"]
        for source in sources
        if source.get("logType") == "USAGE_LOGS" and resource_arn in source.get("resourceArns", [])
    }
    if not names:
        return None
    deliveries = logs.describe_deliveries().get("deliveries", [])
    destination_arns = {
        delivery.get("deliveryDestinationArn")
        for delivery in deliveries
        if delivery.get("deliverySourceName") in names
        and delivery.get("deliveryDestinationType") == "CWL"
    }
    destinations = logs.describe_delivery_destinations().get("deliveryDestinations", [])
    for destination in destinations:
        if destination.get("arn") not in destination_arns:
            continue
        arn = destination.get("deliveryDestinationConfiguration", {}).get(
            "destinationResourceArn", ""
        )
        if ":log-group:" in arn:
            return arn.split(":log-group:", 1)[1].removesuffix(":*")
    return None


def _events_for_tool_session(
    logs,
    log_group: str,
    stream_name: str,
    tool_session_id: str,
    started_at: float | None,
    completed_at: float | None,
) -> list[dict]:
    params: dict[str, Any] = {
        "logGroupName": log_group,
        "logStreamName": stream_name,
        "startFromHead": True,
        "limit": 10_000,
    }
    if started_at:
        params["startTime"] = int((float(started_at) - 300) * 1000)
    if completed_at:
        params["endTime"] = int((float(completed_at) + 900) * 1000)
    events: list[dict] = []
    previous_token = None
    while True:
        response = logs.get_log_events(**params)
        events.extend(response.get("events", []))
        next_token = response.get("nextForwardToken")
        if not next_token or next_token == previous_token:
            break
        previous_token = next_token
        params["nextToken"] = next_token
    return [event for event in events if _event_session_id(event) == tool_session_id]


def _event_session_id(event: dict) -> str:
    try:
        return str(json.loads(event.get("message", "")).get("attributes", {}).get("session.id", ""))
    except json.JSONDecodeError:
        return ""


def _usage_from_events(events: list[dict]) -> dict:
    vcpu_hours = 0.0
    memory_gb_hours = 0.0
    elapsed_seconds = 0.0
    usage_records = 0
    for event in events:
        try:
            payload = json.loads(event.get("message", ""))
        except json.JSONDecodeError:
            continue
        metrics = payload.get("metrics", {})
        vcpu = sum(
            float(value or 0.0)
            for key, value in metrics.items()
            if key.endswith(".vcpu.hours.used")
        )
        memory = sum(
            float(value or 0.0)
            for key, value in metrics.items()
            if key.endswith(".memory.gb_hours.used")
        )
        if not vcpu and not memory:
            continue
        usage_records += 1
        vcpu_hours += vcpu
        memory_gb_hours += memory
        elapsed_seconds += float(
            payload.get("attributes", {}).get("time_elapsed_seconds", 0.0) or 0.0
        )
    if not usage_records:
        return {"status": BUILTIN_USAGE_PENDING}
    return {
        "status": BUILTIN_USAGE_AVAILABLE,
        "usage_records": usage_records,
        "elapsed_seconds": elapsed_seconds,
        "vcpu_hours": vcpu_hours,
        "memory_gb_hours": memory_gb_hours,
        **builtin_usage_cost(vcpu_hours, memory_gb_hours),
    }


def fetch_builtin_usage_for_session(
    config: dict,
    app_session_id: str,
    tool_kinds: list[str],
    started_at: float | None,
    completed_at: float | None,
) -> dict:
    """Join app session -> built-in-tool session -> vended usage records."""
    kinds = set(tool_kinds) & set(TOOL_SPECS)
    if not kinds:
        return {
            "status": BUILTIN_USAGE_AVAILABLE,
            "tools": {},
            "vcpu_hours": 0.0,
            "memory_gb_hours": 0.0,
            **builtin_usage_cost(0.0, 0.0),
        }
    try:
        region = config["region"]
        client = boto3.client("bedrock-agentcore", region_name=region)
        logs = boto3.client("logs", region_name=region)
        identifiers = _tool_identifiers(config, kinds)
        tool_results: dict[str, dict] = {}
        for kind in sorted(kinds):
            identifier = identifiers[kind]
            tool_session = _matching_tool_session(
                client,
                kind,
                identifier,
                app_session_id,
                started_at,
                completed_at,
            )
            if not tool_session:
                tool_results[kind] = {"status": BUILTIN_USAGE_PENDING}
                continue
            resource_type = "browser" if kind == "browser" else "code-interpreter"
            if not identifier.startswith("aws."):
                resource_type += "-custom"
            arn_parts = str(config.get("arn", "")).split(":")
            account = (
                "aws"
                if identifier.startswith("aws.")
                else (arn_parts[4] if len(arn_parts) > 4 else "")
            )
            resource_arn = (
                f"arn:aws:bedrock-agentcore:{region}:{account}:{resource_type}/{identifier}"
            )
            log_group = config.get("builtin_usage_log_group") or _discover_log_group(
                logs, resource_arn
            )
            if not log_group:
                tool_results[kind] = {
                    "status": BUILTIN_USAGE_ERROR,
                    "error": "AgentCore built-in-tool USAGE_LOGS delivery is not configured",
                }
                continue
            events = _events_for_tool_session(
                logs,
                log_group,
                TOOL_SPECS[kind]["stream"],
                tool_session["sessionId"],
                started_at,
                completed_at,
            )
            tool_results[kind] = {
                **_usage_from_events(events),
                "session_id": tool_session["sessionId"],
            }

        vcpu_hours = sum(float(item.get("vcpu_hours", 0.0)) for item in tool_results.values())
        memory_gb_hours = sum(
            float(item.get("memory_gb_hours", 0.0)) for item in tool_results.values()
        )
        statuses = {item.get("status") for item in tool_results.values()}
        status = (
            BUILTIN_USAGE_AVAILABLE
            if statuses == {BUILTIN_USAGE_AVAILABLE}
            else BUILTIN_USAGE_ERROR
            if BUILTIN_USAGE_ERROR in statuses
            else BUILTIN_USAGE_PENDING
        )
        result = {
            "status": status,
            "tools": tool_results,
            "vcpu_hours": vcpu_hours,
            "memory_gb_hours": memory_gb_hours,
            **builtin_usage_cost(vcpu_hours, memory_gb_hours),
        }
        errors = [str(item["error"]) for item in tool_results.values() if item.get("error")]
        if errors:
            result["error"] = "; ".join(dict.fromkeys(errors))
        return result
    except (BotoCoreError, ClientError, KeyError, ValueError) as exc:
        if isinstance(exc, ClientError):
            message = exc.response.get("Error", {}).get("Message", str(exc))
        else:
            message = str(exc)
        return {"status": BUILTIN_USAGE_ERROR, "error": message}
