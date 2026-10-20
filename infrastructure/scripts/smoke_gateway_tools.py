#!/usr/bin/env python3
"""Post-deploy smoke test for AgentCore Gateway tool discovery.

Connects to the Gateway exactly like the custom Runtime agent does
(SigV4 + ``list_tools_sync()``) and asserts the expected tool families are
in the catalog. Gateway prefixes tool names per target (``target___tool``).

Environment:
  GATEWAY_URL             Required. Gateway MCP URL (Pulumi ``gateway_url``).
  AWS_REGION              SigV4 region (default eu-west-1, matching the agent).
  EXPECT_MCP_TOOLS        ``true`` when the mcp-tools Runtime target is attached
                          (default ``false``).
  EXPECT_BOOKING          ``true`` when the booking RapidAPI target is attached.
  EXPECT_AERODATABOX      ``true`` when the aerodatabox target is attached.
  EXPECT_AIR_SCRAPER      ``true`` when the air-scraper target is attached.
  EXPECT_GEOAPIFY         ``true`` when the geoapify target is attached.
  EXPECT_TRAVEL_RAG       ``true`` when the Wikivoyage RAG target is attached.

Exit codes: 0 success, 1 failure.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agents" / "custom"))
from mcp_client.gateway import get_gateway_mcp_client  # noqa: E402

# Weather comes from the always-present Open-Meteo Lambda target.
REQUIRED_SUFFIXES = ("searchLocation", "getForecast")
OPTIONAL_TARGETS = {
    "EXPECT_MCP_TOOLS": ("webSearch", "searchMapPlaces", "findNearbyPlaces"),
    "EXPECT_BOOKING": ("booking___",),
    "EXPECT_AERODATABOX": ("aerodatabox___",),
    "EXPECT_AIR_SCRAPER": ("air-scraper___",),
    "EXPECT_GEOAPIFY": ("geoapify___",),
    "EXPECT_TRAVEL_RAG": ("search_travel_guides",),
}


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes"}


def _matches(names: list[str], needle: str) -> bool:
    """True when *needle* matches a tool name as suffix or prefix (Gateway
    prefixes tools with ``target___``)."""
    if needle.endswith("___"):
        return any(name.startswith(needle) for name in names)
    return any(name == needle or name.endswith(f"___{needle}") for name in names)


def _content_text(result: object) -> str:
    if isinstance(result, dict):
        content = result.get("content") or []
    else:
        content = getattr(result, "content", None) or []
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and "text" in item:
            parts.append(str(item["text"]))
        elif hasattr(item, "text"):
            parts.append(str(item.text))
        else:
            parts.append(str(item))
    return " ".join(parts)


def main() -> int:
    if not os.environ.get("GATEWAY_URL"):
        print("SMOKE FAILED: GATEWAY_URL is required", file=sys.stderr)
        return 1

    calls: dict[str, str] = {}
    call_samples: dict[str, str] = {}
    client = get_gateway_mcp_client()
    with client:
        names = [
            str(getattr(tool, "tool_name", getattr(tool, "name", "")))
            for tool in client.list_tools_sync()
        ]
        if _truthy("EXPECT_MCP_TOOLS"):
            nearby_tool = next(
                (
                    name
                    for name in names
                    if name == "findNearbyPlaces" or name.endswith("___findNearbyPlaces")
                ),
                None,
            )
            if nearby_tool:
                nearby_result = client.call_tool_sync(
                    "smoke-find-nearby",
                    nearby_tool,
                    {
                        "latitude": 38.7077507,
                        "longitude": -9.1365919,
                        "category": "museums",
                        "radius": 3000,
                        "limit": 3,
                    },
                    read_timeout_seconds=timedelta(seconds=120),
                )
                calls[nearby_tool] = _content_text(nearby_result)
                call_samples[nearby_tool] = calls[nearby_tool][:500]

    failures: list[str] = []
    for needle in REQUIRED_SUFFIXES:
        if not _matches(names, needle):
            failures.append(f"missing required weather tool: {needle}")
    for env_flag, needles in OPTIONAL_TARGETS.items():
        if not _truthy(env_flag):
            continue
        for needle in needles:
            if not _matches(names, needle):
                failures.append(f"{env_flag} set but no tool matches: {needle}")
    if _truthy("EXPECT_MCP_TOOLS"):
        nearby_call = next(
            (text for name, text in calls.items() if name.endswith("findNearbyPlaces")),
            "",
        )
        if not nearby_call:
            failures.append("EXPECT_MCP_TOOLS set but findNearbyPlaces was not executed")
        elif not nearby_call.strip().startswith("{") or "results" not in json.loads(nearby_call):
            failures.append(
                f"findNearbyPlaces did not return a results envelope: {nearby_call[:300]!r}"
            )

    print(
        json.dumps(
            {"ok": not failures, "tool_names": sorted(names), "calls": call_samples}, indent=2
        )
    )
    if failures:
        for failure in failures:
            print(f"SMOKE FAILED: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
