"""Reconcile Custom Runtime token usage from session-keyed application logs."""

from __future__ import annotations

import json
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

TOKEN_USAGE_MARKER = "comparison_token_usage="
TOKEN_USAGE_PENDING = "pending"
TOKEN_USAGE_AVAILABLE = "available"
TOKEN_USAGE_ERROR = "error"


def _marked_payload(message: str) -> dict | None:
    marker_at = message.find(TOKEN_USAGE_MARKER)
    if marker_at < 0:
        return None
    candidate = message[marker_at + len(TOKEN_USAGE_MARKER) :].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _payload_from_message(message: str) -> dict | None:
    if payload := _marked_payload(message):
        return payload
    try:
        envelope = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict):
        return None
    for key in ("body", "message", "log", "event"):
        nested = envelope.get(key)
        if isinstance(nested, str) and (payload := _marked_payload(nested)):
            return payload
    return None


def _usage_from_events(events: list[dict], session_id: str) -> dict:
    latest: tuple[int, dict] | None = None
    for event in events:
        payload = _payload_from_message(str(event.get("message", "")))
        if payload is None or payload.get("session_id") != session_id:
            continue
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            continue
        timestamp = int(event.get("timestamp", 0) or 0)
        if latest is None or timestamp >= latest[0]:
            latest = (timestamp, usage)
    if latest is None:
        return {"status": TOKEN_USAGE_PENDING}
    return {"status": TOKEN_USAGE_AVAILABLE, "usage": latest[1]}


def fetch_token_usage_for_session(
    region: str,
    log_group: str,
    session_id: str,
    started_at: float | None,
    completed_at: float | None,
) -> dict:
    """Read the latest structured token-usage line for one Runtime session."""
    if not log_group:
        return {"status": TOKEN_USAGE_ERROR, "error": "Token usage log group is not configured"}
    params = {
        "logGroupName": log_group,
        "filterPattern": f'"{session_id}"',
        "limit": 100,
    }
    if started_at:
        params["startTime"] = int((started_at - 300) * 1000)
    params["endTime"] = int(((completed_at or time.time()) + 900) * 1000)
    client = boto3.client("logs", region_name=region)
    events: list[dict] = []
    try:
        while True:
            response = client.filter_log_events(**params)
            events.extend(response.get("events", []))
            token = response.get("nextToken")
            if not token:
                break
            params["nextToken"] = token
    except (BotoCoreError, ClientError) as exc:
        message = (
            exc.response.get("Error", {}).get("Message", str(exc))
            if isinstance(exc, ClientError)
            else str(exc)
        )
        return {"status": TOKEN_USAGE_ERROR, "error": message}
    return _usage_from_events(events, session_id)
