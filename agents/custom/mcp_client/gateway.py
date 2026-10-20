"""AgentCore Gateway MCP client for the custom agent."""

from __future__ import annotations

import os
import re
import urllib.parse
from datetime import timedelta

import boto3
from botocore.credentials import Credentials
from strands.tools.mcp.mcp_client import MCPClient

from .streamable_http_sigv4 import streamable_http_client_with_sigv4

GATEWAY_SERVICE = "bedrock-agentcore"
_AGENTCORE_GATEWAY_HOST_RE = re.compile(
    r"^(?:.+\.)?gateway\.bedrock-agentcore\.(?P<region>[a-z0-9-]+)\.amazonaws\.com$"
)


def _gateway_region(gateway_url: str) -> str:
    """Return the AgentCore Gateway region from the URL, or fall back to runtime region."""
    host = urllib.parse.urlparse(gateway_url).hostname or ""
    match = _AGENTCORE_GATEWAY_HOST_RE.match(host)
    return match.group("region") if match else os.environ.get("AWS_REGION", "eu-west-1")


def _load_credentials(region: str) -> Credentials:
    session = boto3.Session(region_name=region)
    frozen = session.get_credentials().get_frozen_credentials()
    return Credentials(
        access_key=frozen.access_key,
        secret_key=frozen.secret_key,
        token=frozen.token,
    )


def get_gateway_mcp_client(gateway_url: str | None = None) -> MCPClient:
    """Return an MCP client authenticated to an AWS_IAM AgentCore Gateway."""
    url = gateway_url or os.environ.get("GATEWAY_URL")
    if not url:
        raise RuntimeError("GATEWAY_URL is not configured")

    region = _gateway_region(url)
    credentials = _load_credentials(region)

    return MCPClient(
        lambda: streamable_http_client_with_sigv4(
            url=url,
            credentials=credentials,
            service=GATEWAY_SERVICE,
            region=region,
            timeout=timedelta(seconds=120),
            sse_read_timeout=timedelta(seconds=300),
        ),
        startup_timeout=60,
    )
