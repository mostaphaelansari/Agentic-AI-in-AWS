"""Shared backend utilities."""

from __future__ import annotations

import json
import logging
from datetime import date

import boto3
from botocore.config import Config as BotocoreConfig

from backends.errors import AWSAuthenticationError, is_authentication_error

logger = logging.getLogger("unified-ui.backends")


def with_current_date(user_msg: str) -> str:
    return f"Current date: {date.today().isoformat()}\n{user_msg}"


def get_client(region: str):
    return boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=BotocoreConfig(
            read_timeout=600,
            connect_timeout=10,
            retries={"max_attempts": 2},
            tcp_keepalive=True,
        ),
    )


def ensure_aws_credentials() -> None:
    """Resolve credentials once before starting parallel backend requests."""
    try:
        credentials = boto3.Session().get_credentials()
        if credentials is None:
            raise AWSAuthenticationError("No AWS credentials found")
        credentials.get_frozen_credentials()
    except Exception as exc:
        if is_authentication_error(exc):
            raise AWSAuthenticationError(str(exc)) from exc
        raise


def parse_sse_events(streaming_body, cancel=None):
    """Yield JSON dictionaries from an SSE response body."""
    for line_bytes in streaming_body.iter_lines(chunk_size=1):
        if cancel is not None and cancel.is_set():
            return
        line = (
            line_bytes.decode("utf-8", errors="replace")
            if isinstance(line_bytes, bytes)
            else line_bytes
        )
        line = line.strip()
        if not line.startswith("data:"):
            continue

        data_str = line[len("data:") :].strip()
        if not data_str:
            continue
        try:
            yield json.loads(data_str)
        except json.JSONDecodeError:
            logger.warning("Discarding malformed SSE data")
