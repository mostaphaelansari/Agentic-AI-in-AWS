"""URL helpers local to the mcp-tools Pulumi stack."""

from __future__ import annotations

import urllib.parse


def runtime_mcp_endpoint(runtime_arn: str, aws_region: str) -> str:
    """Build the streamable-HTTP MCP invoke URL for an AgentCore Runtime."""
    encoded = urllib.parse.quote(runtime_arn, safe="")
    return (
        f"https://bedrock-agentcore.{aws_region}.amazonaws.com"
        f"/runtimes/{encoded}/invocations?qualifier=DEFAULT"
    )
