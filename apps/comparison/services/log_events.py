"""CloudWatch Logs helpers for ad-hoc debugging."""

from __future__ import annotations

import argparse
import json
import time

import boto3
from botocore.exceptions import ClientError

RUNTIME_VCPU_HOUR_USD = 0.0895
RUNTIME_GB_HOUR_USD = 0.00945


def get_log_events(
    *,
    region: str,
    log_group: str,
    log_stream: str,
    start_time: int | None = None,
    end_time: int | None = None,
    limit: int = 20,
    start_from_head: bool = True,
) -> dict:
    """Fetch raw CloudWatch log events from one log stream.

    Times are Unix milliseconds, matching the CloudWatch Logs API.
    """
    params = {
        "logGroupName": log_group,
        "logStreamName": log_stream,
        "limit": limit,
        "startFromHead": start_from_head,
    }
    if start_time is not None:
        params["startTime"] = int(start_time)
    if end_time is not None:
        params["endTime"] = int(end_time)

    client = boto3.client("logs", region_name=region)
    try:
        response = client.get_log_events(**params)
    except ClientError as exc:
        message = exc.response.get("Error", {}).get("Message", str(exc))
        return {"events": [], "error": message}

    return {
        "events": response.get("events", []),
        "next_forward_token": response.get("nextForwardToken"),
        "next_backward_token": response.get("nextBackwardToken"),
    }


def format_log_event(event: dict) -> str:
    timestamp = event.get("timestamp", 0) / 1000
    rendered_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    return f"{rendered_time} - {event.get('message', '')}"


def latest_log_stream(region: str, log_group: str) -> str | None:
    client = boto3.client("logs", region_name=region)
    try:
        response = client.describe_log_streams(
            logGroupName=log_group,
            orderBy="LastEventTime",
            descending=True,
            limit=1,
        )
    except ClientError:
        return None
    streams = response.get("logStreams", [])
    return streams[0]["logStreamName"] if streams else None


def runtime_consumption_from_events(events: list[dict]) -> dict:
    vcpu_hours = 0.0
    memory_gb_hours = 0.0
    elapsed_seconds = 0.0
    usage_records = 0

    for event in events:
        try:
            payload = json.loads(event["message"])
        except (KeyError, json.JSONDecodeError):
            continue

        metrics = payload.get("metrics", {})
        vcpu = float(metrics.get("agent.runtime.vcpu.hours.used", 0.0))
        memory = float(metrics.get("agent.runtime.memory.gb_hours.used", 0.0))
        if not vcpu and not memory:
            continue

        usage_records += 1
        vcpu_hours += vcpu
        memory_gb_hours += memory
        elapsed_seconds += float(payload.get("attributes", {}).get("time_elapsed_seconds", 0.0))

    return {
        "usage_records": usage_records,
        "elapsed_seconds": elapsed_seconds,
        "vcpu_hours": vcpu_hours,
        "memory_gb_hours": memory_gb_hours,
        "cpu_cost": vcpu_hours * RUNTIME_VCPU_HOUR_USD,
        "memory_cost": memory_gb_hours * RUNTIME_GB_HOUR_USD,
        "runtime_cost": vcpu_hours * RUNTIME_VCPU_HOUR_USD + memory_gb_hours * RUNTIME_GB_HOUR_USD,
    }


def print_runtime_usage(events: list[dict]) -> None:
    usage = runtime_consumption_from_events(events)
    print("\nRuntime usage summary")
    print(f"usage_records={usage['usage_records']}")
    print(f"elapsed_seconds={usage['elapsed_seconds']:.6f}")
    print(f"vcpu_hours={usage['vcpu_hours']:.12f}")
    print(f"memory_gb_hours={usage['memory_gb_hours']:.12f}")
    print(f"cpu_cost_usd={usage['cpu_cost']:.9f}$")
    print(f"memory_cost_usd={usage['memory_cost']:.9f}$")
    print(f"runtime_cost_usd={usage['runtime_cost']:.9f}$")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch CloudWatch log events.")
    parser.add_argument("--region", required=True)
    parser.add_argument("--log-group", required=True)
    parser.add_argument("--log-stream")
    parser.add_argument("--start-time", type=int, help="Unix milliseconds")
    parser.add_argument("--end-time", type=int, help="Unix milliseconds")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--tail", action="store_true", help="Read from the newest stream tail")
    args = parser.parse_args()

    log_stream = args.log_stream or latest_log_stream(args.region, args.log_group)
    if not log_stream:
        print(f"No log streams found for {args.log_group} in {args.region}")
        return 1

    fetch_limit = max(args.limit, 50) if args.tail else args.limit
    result = get_log_events(
        region=args.region,
        log_group=args.log_group,
        log_stream=log_stream,
        start_time=args.start_time,
        end_time=args.end_time,
        limit=fetch_limit,
        start_from_head=not args.tail,
    )
    if result.get("error"):
        print(f"An error occurred: {result['error']}")
        return 1

    events = result["events"]
    if args.tail:
        events = sorted(
            events,
            key=lambda event: event.get("timestamp", 0),
            reverse=True,
        )[: args.limit]

    print(f"log_stream={log_stream}")
    for event in events:
        print(format_log_event(event))
    print_runtime_usage(events)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
