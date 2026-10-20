#!/usr/bin/env python3
"""Post-deploy smoke test for the AgentCore Runtime MCP tools server.

Authenticates with SigV4 (service ``bedrock-agentcore``), initializes an MCP
session, lists tools, and exercises representative calls.

Environment:
  MCP_ENDPOINT   Required. Streamable-HTTP invoke URL from Pulumi ``mcp_endpoint``.
  AWS_REGION     Required for SigV4 (defaults to AWS_DEFAULT_REGION / eu-central-1).
  RUNTIME_ARN    Optional. Logged for diagnostics.
  IMAGE_URI      Optional. When set with EXPECTED_IMAGE_TAG, asserts the tag matches.
  EXPECTED_IMAGE_TAG  Optional. Git SHA expected in IMAGE_URI.
  EXPECT_WEATHER_TOOLS  Optional. ``false`` to allow a web-and-map-only Runtime
                        (default ``true`` — the stack serves the full tool set,
                        and a deployed weather call must succeed).

Exit codes: 0 success, 1 failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.credentials import Credentials
from mcp import ClientSession

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agents" / "custom"))
from mcp_client.streamable_http_sigv4 import streamable_http_client_with_sigv4  # noqa: E402

EXPECTED_CORE_TOOLS = frozenset({"webSearch", "searchMapPlaces", "findNearbyPlaces"})
WEATHER_TOOLS = frozenset({"searchLocation", "getForecast"})


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes"}


def check_image_tag(image_uri: str, expected_tag: str) -> None:
    """Raise AssertionError when IMAGE_URI tag does not match EXPECTED_IMAGE_TAG."""
    if not image_uri or not expected_tag:
        return
    tag = image_uri.rsplit(":", 1)[-1]
    if tag != expected_tag:
        raise AssertionError(f"image tag mismatch: got {tag!r}, expected {expected_tag!r}")


def assert_tool_catalog(names: set[str], *, expect_weather: bool) -> None:
    missing = EXPECTED_CORE_TOOLS - names
    if missing:
        raise AssertionError(f"missing core tools: {sorted(missing)}")
    if expect_weather:
        missing_weather = WEATHER_TOOLS - names
        if missing_weather:
            raise AssertionError(f"weather tools required but missing: {sorted(missing_weather)}")
    else:
        unexpected = WEATHER_TOOLS & names
        if unexpected:
            raise AssertionError(
                f"EXPECT_WEATHER_TOOLS=false but weather tools are present: {sorted(unexpected)}"
            )


async def _run() -> dict:
    endpoint = os.environ.get("MCP_ENDPOINT", "").strip()
    if not endpoint:
        raise SystemExit("MCP_ENDPOINT is required")

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-central-1"
    check_image_tag(
        os.environ.get("IMAGE_URI", "").strip(),
        os.environ.get("EXPECTED_IMAGE_TAG", "").strip(),
    )

    session = boto3.Session(region_name=region)
    credentials = session.get_credentials()
    if credentials is None:
        raise SystemExit("No AWS credentials available for SigV4")
    frozen = credentials.get_frozen_credentials()
    signed = Credentials(frozen.access_key, frozen.secret_key, frozen.token)

    expect_weather = _truthy("EXPECT_WEATHER_TOOLS", default=True)
    report: dict = {
        "mcp_endpoint": endpoint,
        "runtime_arn": os.environ.get("RUNTIME_ARN"),
        "image_uri": os.environ.get("IMAGE_URI"),
        "region": region,
        "tool_names": [],
        "calls": {},
    }

    async with streamable_http_client_with_sigv4(
        url=endpoint,
        credentials=signed,
        service="bedrock-agentcore",
        region=region,
        timeout=120,
        terminate_on_close=True,
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as client:
            await client.initialize()
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            report["tool_names"] = sorted(names)
            assert_tool_catalog(names, expect_weather=expect_weather)

            map_result = await client.call_tool(
                "searchMapPlaces",
                {"query": "Lisbon", "limit": 1, "language": "en"},
            )
            map_text = " ".join(part.text for part in map_result.content if hasattr(part, "text"))
            report["calls"]["searchMapPlaces"] = map_text[:500]
            map_payload = json.loads(map_text) if map_text.strip().startswith("{") else {}
            map_results = map_payload.get("results") or []
            if not map_results and "Lisbon" not in map_text and "lisbon" not in map_text.lower():
                raise AssertionError(
                    f"searchMapPlaces returned no usable results: {map_text[:300]!r}"
                )
            if map_results:
                first_map_result = map_results[0]
                nearby_result = await client.call_tool(
                    "findNearbyPlaces",
                    {
                        "latitude": first_map_result["latitude"],
                        "longitude": first_map_result["longitude"],
                        "category": "museums",
                        "radius": 3000,
                        "limit": 3,
                    },
                )
                nearby_text = " ".join(
                    part.text for part in nearby_result.content if hasattr(part, "text")
                )
                report["calls"]["findNearbyPlaces"] = nearby_text[:500]
                nearby_payload = (
                    json.loads(nearby_text) if nearby_text.strip().startswith("{") else {}
                )
                if "results" not in nearby_payload:
                    raise AssertionError(
                        f"findNearbyPlaces did not return a results envelope: {nearby_text[:300]!r}"
                    )

            # Web search can be rate-limited; require a structured envelope only.
            search_result = await client.call_tool(
                "webSearch",
                {"query": "OpenStreetMap", "max_results": 1},
            )
            search_text = " ".join(
                part.text for part in search_result.content if hasattr(part, "text")
            )
            report["calls"]["webSearch"] = search_text[:500]
            search_payload = json.loads(search_text) if search_text.strip().startswith("{") else {}
            if "results" not in search_payload:
                raise AssertionError(
                    f"webSearch did not return a results envelope: {search_text[:300]!r}"
                )

            if expect_weather:
                # DoD: at least one deployed weather call must succeed.
                location_result = await client.call_tool(
                    "searchLocation", {"name": "Lisbon", "count": 1}
                )
                location_text = " ".join(
                    part.text for part in location_result.content if hasattr(part, "text")
                )
                report["calls"]["searchLocation"] = location_text[:500]
                location_payload = json.loads(location_text)
                results = location_payload.get("results") or []
                if not results:
                    raise AssertionError(
                        f"searchLocation returned no results: {location_text[:300]!r}"
                    )
                first = results[0]

                forecast_result = await client.call_tool(
                    "getForecast",
                    {
                        "latitude": first["latitude"],
                        "longitude": first["longitude"],
                        "current_weather": True,
                        "forecast_days": 1,
                    },
                )
                forecast_text = " ".join(
                    part.text for part in forecast_result.content if hasattr(part, "text")
                )
                report["calls"]["getForecast"] = forecast_text[:500]
                forecast_payload = json.loads(forecast_text)
                if "current_weather" not in forecast_payload:
                    raise AssertionError(
                        f"getForecast missing current_weather: {forecast_text[:300]!r}"
                    )

    return report


def main() -> int:
    try:
        report = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — CLI surface
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
