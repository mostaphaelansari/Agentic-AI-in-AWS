"""Fetch AgentCore Runtime usage records for one app session."""

from __future__ import annotations

import json
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from config import AGENTCORE_RUNTIME_PRICING

RUNTIME_USAGE_PENDING = "pending"
RUNTIME_USAGE_AVAILABLE = "available"
RUNTIME_USAGE_ERROR = "error"


def _window_ms(
    started_at: float | None, completed_at: float | None
) -> tuple[int | None, int | None]:
    if not started_at:
        return None, None
    start_time = int((started_at - 300) * 1000)
    end_source = completed_at or time.time()
    end_time = int((end_source + 900) * 1000)
    return start_time, end_time


def runtime_usage_cost(vcpu_hours: float, memory_gb_hours: float) -> dict:
    cpu_cost = vcpu_hours * AGENTCORE_RUNTIME_PRICING["vcpu_hour"]
    memory_cost = memory_gb_hours * AGENTCORE_RUNTIME_PRICING["gb_hour"]
    return {
        "cpu_cost": cpu_cost,
        "memory_cost": memory_cost,
        "runtime_cost": cpu_cost + memory_cost,
    }


def _usage_from_events(events: list[dict], session_id: str) -> dict:
    vcpu_hours = 0.0
    memory_gb_hours = 0.0
    elapsed_seconds = 0.0
    usage_records = 0
    last_event_timestamp = None

    for event in events:
        try:
            payload = json.loads(event.get("message", ""))
        except json.JSONDecodeError:
            continue

        attributes = payload.get("attributes", {})
        if attributes.get("session.id") != session_id:
            continue

        metrics = payload.get("metrics", {})
        vcpu = float(metrics.get("agent.runtime.vcpu.hours.used", 0.0) or 0.0)
        memory = float(metrics.get("agent.runtime.memory.gb_hours.used", 0.0) or 0.0)
        if not vcpu and not memory:
            continue

        usage_records += 1
        vcpu_hours += vcpu
        memory_gb_hours += memory
        elapsed_seconds += float(attributes.get("time_elapsed_seconds", 0.0) or 0.0)
        last_event_timestamp = max(
            last_event_timestamp or 0,
            int(payload.get("event_timestamp") or event.get("timestamp") or 0),
        )

    if not usage_records:
        return {"status": RUNTIME_USAGE_PENDING}

    costs = runtime_usage_cost(vcpu_hours, memory_gb_hours)
    return {
        "status": RUNTIME_USAGE_AVAILABLE,
        "usage_records": usage_records,
        "elapsed_seconds": elapsed_seconds,
        "vcpu_hours": vcpu_hours,
        "memory_gb_hours": memory_gb_hours,
        "last_event_timestamp": last_event_timestamp,
        **costs,
    }


def fetch_runtime_usage_for_session(
    region: str,
    log_group: str,
    session_id: str,
    started_at: float | None,
    completed_at: float | None,
) -> dict:
    """Fetch and summarize Runtime USAGE_LOGS for one runtimeSessionId."""
    client = boto3.client("logs", region_name=region)
    start_time, end_time = _window_ms(started_at, completed_at)
    params = {
        "logGroupName": log_group,
        "filterPattern": f'"{session_id}"',
        "limit": 100,
    }
    if start_time is not None:
        params["startTime"] = start_time
    if end_time is not None:
        params["endTime"] = end_time

    events: list[dict] = []
    try:
        while True:
            response = client.filter_log_events(**params)
            events.extend(response.get("events", []))
            next_token = response.get("nextToken")
            if not next_token:
                break
            params["nextToken"] = next_token
        # Vended Harness USAGE_LOGS can be readable through GetLogEvents while
        # FilterLogEvents returns an empty page. Scan only streams overlapping
        # this invocation's bounded time window, then apply the exact session
        # match in _usage_from_events.
        if not events:
            streams = client.describe_log_streams(
                logGroupName=log_group,
                orderBy="LastEventTime",
                descending=True,
                limit=10,
            ).get("logStreams", [])
            for stream in streams:
                first = int(stream.get("firstEventTimestamp", 0) or 0)
                last = int(stream.get("lastEventTimestamp", 0) or 0)
                if start_time is not None and last and last < start_time:
                    continue
                if end_time is not None and first and first > end_time:
                    continue
                stream_params = {
                    "logGroupName": log_group,
                    "logStreamName": stream["logStreamName"],
                    "startFromHead": True,
                    "limit": 10_000,
                }
                if start_time is not None:
                    stream_params["startTime"] = start_time
                if end_time is not None:
                    stream_params["endTime"] = end_time
                previous_token = None
                while True:
                    response = client.get_log_events(**stream_params)
                    events.extend(response.get("events", []))
                    next_token = response.get("nextForwardToken")
                    if not next_token or next_token == previous_token:
                        break
                    previous_token = next_token
                    stream_params["nextToken"] = next_token
    except (BotoCoreError, ClientError) as exc:
        message = (
            exc.response.get("Error", {}).get("Message", str(exc))
            if isinstance(exc, ClientError)
            else str(exc)
        )
        return {"status": RUNTIME_USAGE_ERROR, "error": message}

    return _usage_from_events(events, session_id)


def _harness_usage_log_group(config: dict) -> str:
    """Resolve the vended Runtime log group behind a managed Harness."""
    if configured := (config.get("usage_log_group") or "").strip():
        return configured
    harness_id = (config.get("arn") or "").rsplit("/", 1)[-1]
    if not harness_id:
        raise ValueError("Harness ARN is missing")
    control = boto3.client("bedrock-agentcore-control", region_name=config["region"])
    response = control.get_harness(harnessId=harness_id)
    runtime_id = (
        response.get("harness", {})
        .get("environment", {})
        .get("agentCoreRuntimeEnvironment", {})
        .get("agentRuntimeId")
    )
    if not runtime_id:
        raise ValueError("Harness does not expose its underlying Runtime id")
    return f"/aws/vendedlogs/bedrock-agentcore/runtime/USAGE_LOGS/{runtime_id}"


def fetch_compute_usage_for_session(
    config: dict,
    session_id: str,
    started_at: float | None,
    completed_at: float | None,
) -> dict:
    """Fetch compute usage for either a custom Runtime or managed Harness."""
    try:
        log_group = (
            _harness_usage_log_group(config)
            if config.get("service") == "harness"
            else config.get("usage_log_group")
        )
        if not log_group:
            raise ValueError("Runtime usage log group is not configured")
        return fetch_runtime_usage_for_session(
            config["region"], log_group, session_id, started_at, completed_at
        )
    except (BotoCoreError, ClientError, KeyError, ValueError) as exc:
        if isinstance(exc, ClientError):
            message = exc.response.get("Error", {}).get("Message", str(exc))
        else:
            message = str(exc)
        return {"status": RUNTIME_USAGE_ERROR, "error": message}
