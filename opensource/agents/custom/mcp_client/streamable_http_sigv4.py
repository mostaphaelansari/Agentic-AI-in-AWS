"""StreamableHTTP MCP transport with AWS SigV4 signing.

Used when ``MCP_URL`` points at an AgentCore Runtime endpoint
(``https://bedrock-agentcore.<region>.amazonaws.com/...``), which requires
IAM SigV4 authentication instead of plain HTTP. boto3/botocore are available
transitively through strands-agents.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from mcp.client.streamable_http import (
    GetSessionIdCallback,
    streamable_http_client,
)
from mcp.shared._httpx_utils import McpHttpClientFactory, create_mcp_http_client
from mcp.shared.message import SessionMessage


class SigV4HTTPXAuth(httpx.Auth):
    """HTTPX auth handler that signs requests with AWS SigV4."""

    def __init__(self, credentials: Credentials, service: str, region: str):
        self.signer = SigV4Auth(credentials, service, region)

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        headers = dict(request.headers)
        headers.pop("connection", None)

        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=headers,
        )
        self.signer.add_auth(aws_request)
        request.headers.update(dict(aws_request.headers))
        yield request


@asynccontextmanager
async def streamable_http_client_with_sigv4(
    url: str,
    credentials: Credentials,
    service: str,
    region: str,
    headers: dict[str, str] | None = None,
    timeout: float | timedelta = 30,
    sse_read_timeout: float | timedelta = 60 * 5,
    terminate_on_close: bool = True,
    httpx_client_factory: McpHttpClientFactory = create_mcp_http_client,
) -> AsyncGenerator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
        GetSessionIdCallback,
    ],
    None,
]:
    timeout_seconds = timeout.total_seconds() if isinstance(timeout, timedelta) else timeout
    sse_read_timeout_seconds = (
        sse_read_timeout.total_seconds()
        if isinstance(sse_read_timeout, timedelta)
        else sse_read_timeout
    )
    http_client = httpx_client_factory(
        headers=headers,
        timeout=httpx.Timeout(timeout_seconds, read=sse_read_timeout_seconds),
        auth=SigV4HTTPXAuth(credentials, service, region),
    )

    async with http_client:
        async with streamable_http_client(
            url,
            http_client=http_client,
            terminate_on_close=terminate_on_close,
        ) as streams:
            yield streams
